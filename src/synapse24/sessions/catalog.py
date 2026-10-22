"""SYNAPSE-24 session catalog.

Scans a directory of stored XDF / FIT sessions and builds an idempotent,
JSON-serializable index:

* a stable, content-derived `session_id` (sha256 over path + size + mtime),
* device serial, time window, duration,
* per-stream metadata (name, type, sampling rate, sample count),
* XDF validation outcome (`synapse24.utils.validate_xdf`),
* a light FIT structural check.

The catalog has no hardware dependency and works entirely on files already on
disk, so it is safe to run in CI with synthetic fixtures. It is the read-side
of the Averyn data plane: Averyn (and the local reference service in PR5)
consume this index to surface sessions, reports and exports.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any

from synapse_device.fit import FIT_CRC_SIZE, fit_crc16

# project imports
from synapse24.utils import validate_xdf

FIT_HEADER_MAGIC = b".FIT"
FIT_HEADER_SIZE = 14


@dataclass(frozen=True)
class StreamInfo:
    """Metadata for a single stream inside a session file."""

    name: str
    type: str
    sampling_rate_hz: float
    n_samples: int


@dataclass(frozen=True)
class SessionRecord:
    """One indexed session."""

    session_id: str
    device_serial: str
    path: str
    format: str  # "xdf" | "fit"
    size_bytes: int
    sha256: str
    started_at: str | None  # ISO-8601 UTC, or None when not determinable
    ended_at: str | None
    duration_s: float | None
    streams: list[StreamInfo] = field(default_factory=list)
    n_samples_total: int = 0
    xdf_valid: bool | None = None  # True/False for XDF, None for FIT
    fit_valid: bool | None = None  # True/False for FIT, None for XDF
    indexed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass(frozen=True)
class SessionCatalog:
    """Immutable index of all sessions found under a root directory."""

    root: str
    generated_at: str
    sessions: dict[str, SessionRecord] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "generated_at": self.generated_at,
            "n_sessions": len(self.sessions),
            "sessions": {
                sid: {**asdict(rec), "streams": [asdict(s) for s in rec.streams]}
                for sid, rec in self.sessions.items()
            },
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _stable_session_id(path: Path) -> str:
    """Deterministic session id from path + size + mtime.

    Stable across re-scans of an unchanged file (idempotent index), cheap to
    compute (no full-content hash of potentially large XDF files).
    """
    stat = path.stat()
    payload = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    return "syn-" + hashlib.sha256(payload.encode()).hexdigest()[:24]


def _iso_from_timestamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


def _validate_fit_light(path: Path) -> bool:
    """Validate a FIT file: header magic, header CRC and file CRC.

    The check is real, not cosmetic: the CRC of the header and the CRC of the
    file body are recomputed with the FIT algorithm, so a file that merely
    *looks* like FIT (the ones the project used to write) is reported as
    invalid instead of being catalogued as a usable session.

    Consequence: the files written by the previous writer (a different CRC
    algorithm and a per-message CRC) are listed with ``fit_valid = False``.
    They are still catalogued, so nothing disappears silently, but they have
    to be re-exported to be used.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if len(data) < FIT_HEADER_SIZE + FIT_CRC_SIZE:
        return False
    if data[8:12] != FIT_HEADER_MAGIC:
        return False
    header_crc = int.from_bytes(data[12:14], "little")
    if fit_crc16(data[:12]) != header_crc:
        return False
    file_crc = int.from_bytes(data[-2:], "little")
    return fit_crc16(data[:-2]) == file_crc


def _extract_device_serial_from_fit(path: Path) -> str:
    """Best-effort device serial from FIT file_id / developer data; falls back to filename stem."""
    try:
        data = path.read_bytes()
    except OSError:
        return path.stem
    # The file_id message (global mesg num 0) carries a serial number field.
    # A full FIT parse is out of scope here; use the filename stem as the
    # stable device identifier when no serial is embedded.
    return path.stem.upper()


def _clean_str(v: Any) -> str:
    """Normalizza un valore letto da validate_xdf (stringhe serializzate)."""
    if v is None:
        return ""
    s = str(v).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1]
    return s


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(_clean_str(v))
    except (TypeError, ValueError):
        return default


def _xdf_stream_infos(streams: list[dict[str, Any]]) -> list[StreamInfo]:
    """validate_xdf già appiattisce ogni stream a name/type/n_samples/nominal_srate/duration_s."""
    infos: list[StreamInfo] = []
    for s in streams:
        name = _clean_str(s.get("name")) or "unnamed"
        stype = _clean_str(s.get("type")) or "unknown"
        fs = _to_float(s.get("nominal_srate"))
        if fs == 0.0:
            # nominal_srate round-trippa a 0 attraverso l'XML header di XDF;
            # actual_srate è misurato e più affidabile.
            fs = _to_float(s.get("actual_srate"))
        infos.append(
            StreamInfo(
                name=name,
                type=stype,
                sampling_rate_hz=fs,
                n_samples=int(_to_float(s.get("n_samples"))),
            )
        )
    return infos


def index_xdf(path: Path) -> SessionRecord:
    """Index a single XDF file."""
    summary = validate_xdf(path)
    streams_raw = summary.get("streams", []) or []
    streams = _xdf_stream_infos(streams_raw)
    n_total = sum(s.n_samples for s in streams)
    # validate_xdf reports per-stream duration_s but not absolute wall-clock
    # start/end times; leave those unset rather than inventing them.
    duration_s = max((s.get("duration_s") or 0.0) for s in streams_raw) if streams_raw else 0.0
    return SessionRecord(
        session_id=_stable_session_id(path),
        device_serial=_extract_device_serial_from_fit(path),
        path=str(path.resolve()),
        format="xdf",
        size_bytes=path.stat().st_size,
        sha256=_sha256_file(path),
        started_at=None,
        ended_at=None,
        duration_s=round(float(duration_s), 6),
        streams=streams,
        n_samples_total=n_total,
        xdf_valid=bool(summary.get("validation", {}).get("all_streams_valid", False)),
        fit_valid=None,
    )


def index_fit(path: Path) -> SessionRecord:
    """Index a single FIT file (light structural check only)."""
    valid = _validate_fit_light(path)
    try:
        data = path.read_bytes()
    except OSError:
        data = b""
    # Timestamp of file_id message is at a fixed-ish offset; not parsed here.
    started_at = ended_at = None
    duration_s = 0.0
    return SessionRecord(
        session_id=_stable_session_id(path),
        device_serial=_extract_device_serial_from_fit(path),
        path=str(path.resolve()),
        format="fit",
        size_bytes=path.stat().st_size,
        sha256=_sha256_file(path),
        started_at=started_at,
        ended_at=ended_at,
        duration_s=duration_s,
        streams=[],
        n_samples_total=0,
        xdf_valid=None,
        fit_valid=bool(valid),
    )


def index_directory(root: Path | str) -> SessionCatalog:
    """Scan `root` recursively for *.xdf and *.fit files and build the catalog.

    Idempotent: re-scanning an unchanged directory yields identical session
    ids and records (modulo `indexed_at` / `generated_at` timestamps).
    """
    root_path = Path(root).resolve()
    sessions: dict[str, SessionRecord] = {}
    for pattern in ("*.xdf", "*.fit"):
        for path in sorted(root_path.rglob(pattern)):
            try:
                if path.suffix.lower() == ".xdf":
                    rec = index_xdf(path)
                else:
                    rec = index_fit(path)
            except Exception:
                # A single unreadable file must not abort the whole catalog.
                continue
            sessions[rec.session_id] = rec
    return SessionCatalog(
        root=str(root_path),
        generated_at=datetime.now(UTC).isoformat(),
        sessions=sessions,
    )


def save_catalog(catalog: SessionCatalog, output: Path | str) -> None:
    out = Path(output)
    out.write_text(catalog.to_json(), encoding="utf-8")


def load_catalog(output: Path | str) -> SessionCatalog:
    data = json.loads(Path(output).read_text(encoding="utf-8"))
    sessions = {
        sid: SessionRecord(**{**rec, "streams": [StreamInfo(**s) for s in rec.get("streams", [])]})
        for sid, rec in data.get("sessions", {}).items()
    }
    return SessionCatalog(
        root=data["root"],
        generated_at=data["generated_at"],
        sessions=sessions,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_index(args: argparse.Namespace) -> int:
    catalog = index_directory(Path(args.directory))
    if args.out:
        save_catalog(catalog, Path(args.out))
    print(catalog.to_json())
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    catalog = load_catalog(args.index)
    for sid, rec in sorted(catalog.sessions.items()):
        print(
            f"{sid}  {rec.format:4s}  {rec.device_serial:16s}  "
            f"{rec.started_at or '-':24s}  {rec.duration_s or 0:7.2f}s  "
            f"{rec.n_samples_total:8d}  streams={len(rec.streams)}"
        )
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    catalog = load_catalog(args.index)
    rec = catalog.sessions.get(args.session_id)
    if rec is None:
        print(f"session {args.session_id} not found", file=sys.stderr)
        return 1
    print(json.dumps(asdict(rec), indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="synapse-sessions", description="SYNAPSE-24 session catalog"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="Index a directory of XDF/FIT sessions")
    p_index.add_argument("directory", help="Directory to scan")
    p_index.add_argument("--out", help="Write catalog JSON to this file")
    p_index.set_defaults(func=_cmd_index)

    p_list = sub.add_parser("list", help="List sessions in a catalog JSON")
    p_list.add_argument("index", help="Catalog JSON file")
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show", help="Show one session record")
    p_show.add_argument("index", help="Catalog JSON file")
    p_show.add_argument("session_id", help="Session id")
    p_show.set_defaults(func=_cmd_show)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
