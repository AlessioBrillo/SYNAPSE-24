"""SYNAPSE-24 session catalog.

Scans a directory of stored XDF / FIT sessions and builds an idempotent,
JSON-serializable index:

* a stable, content-derived `session_id` (the first 24 hex digits of the file's sha256: the same
  bytes are the same session wherever they sit, in a container or after a copy),
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
import re
import struct
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from synapse_device.fit import (
    FIT_CRC_SIZE,
    FIT_EPOCH,
    FIT_FIELD_FILE_ID_SERIAL,
    FIT_FIELD_FILE_ID_TIME_CREATED,
    FIT_HEADER_DEFINITION,
    fit_crc16,
    read_fit,
)

# project imports
from synapse24.utils import validate_xdf

FIT_HEADER_MAGIC = b".FIT"
FIT_HEADER_SIZE = 14

# Seconds from the Unix epoch (1970-01-01) to the FIT epoch (1989-12-31).
_FIT_EPOCH_OFFSET_S = int((FIT_EPOCH - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds())


@dataclass(frozen=True)
class StreamInfo:
    """Metadata for a single stream inside a session file."""

    name: str
    type: str
    sampling_rate_hz: float
    n_samples: int
    #: unit of each channel as the file declares it ([] = the file says nothing; never guessed)
    channel_units: list[str] = field(default_factory=list)
    #: channel labels as declared in the XDF header ([] = the file says nothing)
    channel_names: list[str] = field(default_factory=list)
    #: acquisition tier declared by the writer (0 when absent)
    tier: int = 0
    #: LSL source_id, which carries the device serial on real recordings
    source_id: str = ""


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
    #: files found but NOT catalogued, path -> why (unreadable, or the same bytes as another file).
    skipped: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "generated_at": self.generated_at,
            "n_sessions": len(self.sessions),
            "skipped": dict(self.skipped),
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


def _session_id(sha256: str) -> str:
    """Session id from the file's content digest.

    It used to hash the absolute path and the mtime, so the same file got another id in a
    container, after a copy, or after a ``touch``; the digest was computed anyway.
    """
    return "syn-" + sha256[:24]


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


def _parse_fit_file_id(path: Path) -> tuple[int | None, float | None]:  # noqa: PLR0915 - FIT framing walker
    """Best-effort read of the FIT file_id message (global mesg 0).

    Returns ``(serial_number, unix_time_created)`` or ``(None, None)`` when the file is not a FIT
    file or the file_id message cannot be found. The walker follows the FIT framing so it can step
    over every message that precedes file_id: definitions (with the developer-data flag and the
    architecture byte, big- or little-endian), normal data messages, and compressed-timestamp
    data messages. It used to take the bytes of any other data message for headers, so a file
    whose first message was not file_id lost its serial; it does not depend on the SDK reader.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None, None
    if len(data) < FIT_HEADER_SIZE + FIT_CRC_SIZE or data[8:12] != FIT_HEADER_MAGIC:
        return None, None

    # The header says how long it is (12 or 14 bytes): do not assume.
    pos = data[0] if data[0] in (12, 14) else FIT_HEADER_SIZE
    end = len(data) - FIT_CRC_SIZE
    # local_type -> (global_num, struct endianness, [(field_num, size, base_type)], developer bytes)
    definitions: dict[int, tuple[int, str, list[tuple[int, int, int]], int]] = {}

    while pos < end:
        header = data[pos]
        pos += 1
        if header & 0x80:  # compressed-timestamp header: a data message, local type in bits 5-6
            local_type, is_definition = (header >> 5) & 0x03, False
        else:
            local_type, is_definition = header & 0x0F, bool(header & FIT_HEADER_DEFINITION)

        if is_definition:
            if pos + 5 > end:
                break
            endian = ">" if data[pos + 1] == 1 else "<"
            global_num = struct.unpack_from(endian + "H", data, pos + 2)[0]
            n_fields = data[pos + 4]
            pos += 5
            if pos + 3 * n_fields > end:
                break
            fields = [struct.unpack_from("<BBB", data, pos + 3 * i) for i in range(n_fields)]
            pos += 3 * n_fields
            developer_bytes = 0
            if header & 0x20:  # developer data flag: a second list of fields follows
                if pos + 1 > end:
                    break
                n_dev = data[pos]
                pos += 1
                if pos + 3 * n_dev > end:
                    break
                developer_bytes = sum(data[pos + 3 * i + 1] for i in range(n_dev))
                pos += 3 * n_dev
            definitions[local_type] = (global_num, endian, fields, developer_bytes)
            continue

        known = definitions.get(local_type)
        if known is None:
            break  # a data message with no definition: the rest cannot be walked
        global_num, endian, fields, developer_bytes = known
        size_total = sum(size for _num, size, _base in fields) + developer_bytes
        if pos + size_total > end:
            break
        if global_num == 0:  # file_id
            serial: int | None = None
            time_created_fit: int | None = None
            offset = pos
            for field_num, size, base_type in fields:
                if base_type in (0x86, 0x8C) and size == 4:  # UINT32 / UINT32Z
                    value = struct.unpack_from(endian + "I", data, offset)[0]
                    if field_num == FIT_FIELD_FILE_ID_SERIAL:
                        serial = value
                    elif field_num == FIT_FIELD_FILE_ID_TIME_CREATED:
                        time_created_fit = value
                offset += size
            if serial is None:
                break  # file_id found but with no serial
            unix_ts = None
            if time_created_fit is not None:
                unix_ts = float(time_created_fit + _FIT_EPOCH_OFFSET_S)
            return serial, unix_ts
        pos += size_total
    return None, None


def _extract_device_serial_from_fit(path: Path) -> str:
    """Device serial from FIT file_id message; falls back to filename stem."""
    serial, _ = _parse_fit_file_id(path)
    if serial is not None:
        return f"SYN-{serial:06d}"
    return path.stem.upper()


_SERIAL_RE = re.compile(r"SYN-(\d{6})", re.IGNORECASE)


def _serial_from_source_ids(source_ids: list[str]) -> str | None:
    """The serial a serial-bearing LSL source_id declares (e.g. ``..._SYN-000123``)."""
    for source_id in source_ids:
        match = _SERIAL_RE.search(source_id)
        if match:
            return f"SYN-{match.group(1)}"
    return None


def _extract_device_serial_from_xdf(path: Path, source_ids: list[str] | None = None) -> str:
    """Prefer the serial in the XDF stream's source_id over the filename.

    A copied/renamed file keeps its device identity in the source_id; the old
    filename-based guess silently attributed a session to the wrong device.
    """
    serial = _serial_from_source_ids(source_ids or [])
    if serial is not None:
        return serial
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


def _xdf_stream_extras(path: Path) -> list[dict[str, Any]]:
    """Channel labels/tier from the XDF <desc>, which validate_xdf does not expose."""
    import pyxdf  # imported here: indexing stays importable without pyxdf

    try:
        streams, _header = pyxdf.load_xdf(str(path), dejitter_timestamps=False)
    except Exception:  # a broken file is reported by validate_xdf, not here
        return []

    extras: list[dict[str, Any]] = []
    for stream in streams:
        info = stream.get("info", {}) or {}

        def first(key: str, default: str = "") -> str:
            value = info.get(key, default)
            if isinstance(value, list):
                return str(value[0]) if value else default
            return str(value) if value is not None else default

        names: list[str] = []
        try:
            for channel in info["desc"][0]["channels"][0]["channel"]:
                names.append(str((channel.get("label") or [""])[0] or ""))
        except (KeyError, IndexError, TypeError, AttributeError):
            pass
        try:
            tier = int(info["desc"][0]["acquisition"][0]["tier"][0])
        except (KeyError, IndexError, TypeError, ValueError):
            tier = 0
        extras.append(
            {
                "name": first("name"),
                "source_id": first("source_id"),
                "channel_names": names,
                "tier": tier,
            }
        )
    return extras


def _xdf_stream_infos(
    streams: list[dict[str, Any]], extras: list[dict[str, Any]] | None = None
) -> list[StreamInfo]:
    """Build StreamInfo from validate_xdf's flat summary + the header <desc> extras."""
    extras = extras or []
    by_name_source = {(e["name"], e["source_id"]): e for e in extras}
    by_name = {e["name"]: e for e in extras}
    infos: list[StreamInfo] = []
    for index, s in enumerate(streams):
        name = _clean_str(s.get("name")) or "unnamed"
        stype = _clean_str(s.get("type")) or "unknown"
        source_id = _clean_str(s.get("source_id"))
        extra = (
            by_name_source.get((name, source_id))
            or by_name.get(name)
            or (extras[index] if index < len(extras) else {})
        )
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
                channel_units=[str(u) for u in (s.get("channel_units") or [])],
                channel_names=[str(n) for n in (extra.get("channel_names") or [])],
                tier=int(extra.get("tier", 0)),
                source_id=source_id or str(extra.get("source_id", "")),
            )
        )
    return infos


#: Timestamps from 2017 on are wall-clock seconds; a local LSL clock never gets there (the same
#: rule the FIT export uses to tell the two apart).
_UNIX_EPOCH_PLAUSIBLE_S = 1_500_000_000.0


def _wall_clock_window(streams_raw: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """ISO start/end of the session when the XDF timestamps are wall-clock time, else (None, None).

    A local LSL clock is not a date (it counts from the machine's start), and a made-up date in a
    health record is worse than none.
    """
    # Only the streams on the wall clock date the session: a marker or aux stream on a small
    # local clock must not take the date away from an ECG that has one.
    dated = [
        s
        for s in streams_raw
        if s.get("first_timestamp") is not None
        and s.get("last_timestamp") is not None
        and s["first_timestamp"] >= _UNIX_EPOCH_PLAUSIBLE_S
    ]
    if not dated:
        return None, None
    return (
        datetime.fromtimestamp(min(s["first_timestamp"] for s in dated), tz=UTC).isoformat(),
        datetime.fromtimestamp(max(s["last_timestamp"] for s in dated), tz=UTC).isoformat(),
    )


def index_xdf(path: Path) -> SessionRecord:
    """Index a single XDF file."""
    digest = _sha256_file(path)
    summary = validate_xdf(path)
    streams_raw = summary.get("streams", []) or []
    extras = _xdf_stream_extras(path)
    streams = _xdf_stream_infos(streams_raw, extras)
    n_total = sum(s.n_samples for s in streams)
    # validate_xdf reports per-stream duration_s but not absolute wall-clock
    # start/end times; leave those unset rather than inventing them.
    duration_s = max((s.get("duration_s") or 0.0) for s in streams_raw) if streams_raw else 0.0
    started_at, ended_at = _wall_clock_window(streams_raw)
    return SessionRecord(
        session_id=_session_id(digest),
        device_serial=_extract_device_serial_from_xdf(
            path, [s.source_id for s in streams if s.source_id]
        ),
        path=str(path.resolve()),
        format="xdf",
        size_bytes=path.stat().st_size,
        sha256=digest,
        started_at=started_at,
        ended_at=ended_at,
        duration_s=round(float(duration_s), 6),
        streams=streams,
        n_samples_total=n_total,
        xdf_valid=bool(summary.get("validation", {}).get("all_streams_valid", False)),
        fit_valid=None,
    )


def index_fit(path: Path) -> SessionRecord:
    """Index a single FIT file: file_id serial/time_created plus record count."""
    digest = _sha256_file(path)
    valid = _validate_fit_light(path)
    serial, time_created_unix = _parse_fit_file_id(path)
    device_serial = f"SYN-{serial:06d}" if serial is not None else path.stem.upper()

    started_at = ended_at = None
    duration_s: float | None = None
    streams: list[StreamInfo] = []
    n_samples_total = 0

    try:
        fit_file = read_fit(path)
        records = fit_file.records
        n_samples_total = len(records)
        if records:
            ts_list = [int(r.get("timestamp", 0)) for r in records if r.get("timestamp")]
            if ts_list:
                first_fit = min(ts_list)
                last_fit = max(ts_list)
                first_unix = float(first_fit + _FIT_EPOCH_OFFSET_S)
                last_unix = float(last_fit + _FIT_EPOCH_OFFSET_S)
                started_at = datetime.fromtimestamp(first_unix, tz=UTC).isoformat()
                ended_at = datetime.fromtimestamp(last_unix, tz=UTC).isoformat()
                duration_s = round(last_unix - first_unix, 6)
        streams = [
            StreamInfo(
                name="record",
                type="FIT",
                sampling_rate_hz=0.0,
                n_samples=n_samples_total,
            )
        ]
    except Exception:
        # read_fit is strict: a structurally valid but semantically empty FIT
        # still yields zero records rather than an error here.
        pass

    if time_created_unix is not None and started_at is None:
        started_at = datetime.fromtimestamp(time_created_unix, tz=UTC).isoformat()

    return SessionRecord(
        session_id=_session_id(digest),
        device_serial=device_serial,
        path=str(path.resolve()),
        format="fit",
        size_bytes=path.stat().st_size,
        sha256=digest,
        started_at=started_at,
        ended_at=ended_at,
        duration_s=duration_s,
        streams=streams,
        n_samples_total=n_samples_total,
        xdf_valid=None,
        fit_valid=bool(valid),
    )


def _relative(path: Path, root: Path) -> str:
    """``path`` relative to ``root`` with forward slashes (the absolute path if it is outside)."""
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def index_directory(root: Path | str) -> SessionCatalog:
    """Scan `root` recursively for *.xdf and *.fit files and build the catalog.

    Idempotent: re-scanning an unchanged directory yields identical session
    ids and records (modulo `indexed_at` / `generated_at` timestamps).
    """
    root_path = Path(root).resolve()
    sessions: dict[str, SessionRecord] = {}
    skipped: dict[str, str] = {}
    for pattern in ("*.xdf", "*.fit"):
        for path in sorted(root_path.rglob(pattern)):
            try:
                if path.suffix.lower() == ".xdf":
                    rec = index_xdf(path)
                else:
                    rec = index_fit(path)
            except Exception as exc:  # noqa: BLE001 - one unreadable file must not abort the catalog
                # ...but it must not vanish either: it is listed with the reason. Keys are relative
                # to the root and the message is the exception type plus a short, single-line text:
                # this goes into catalog.json, and a full host path or raw file content does not.
                rel = _relative(path, root_path)
                text = str(exc).replace(str(path.resolve()), rel).replace(str(root_path), ".")
                reason = " ".join(text.split())[:160]
                skipped[rel] = f"{type(exc).__name__}: {reason}"
                continue
            existing = sessions.get(rec.session_id)
            if existing is not None and existing.path != rec.path:
                # The id is the content digest: two files with the same bytes are one session, and
                # the second used to REPLACE the first without a word.
                skipped[_relative(path, root_path)] = (
                    f"same content as {_relative(Path(existing.path), root_path)}"
                )
                continue
            sessions[rec.session_id] = rec
    return SessionCatalog(
        root=str(root_path),
        generated_at=datetime.now(UTC).isoformat(),
        sessions=sessions,
        skipped=skipped,
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
        skipped={str(k): str(v) for k, v in (data.get("skipped") or {}).items()},
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
