"""Session export: XDF → FIT, the product format consumed by Averyn.

The FIT bytes are produced by the SDK's canonical writer
(:mod:`synapse_device.fit`); this module owns the *analysis* part (which
stream to analyse, R-peak detection, per-window HRV) and the mapping of the
metrics onto the developer fields of the contract
(``contracts/fit_developer_fields.json``).

Two rules make the export trustworthy:

* the analysed stream is selected **by type** (ECG first, then PPG), never by
  sample count: "the biggest stream" is a heuristic that silently analyses the
  wrong signal;
* every timestamp lives in the same clock domain: the R-peak times are
  derived from the stream timestamps, so a window boundary is an absolute
  instant and not an index.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from synapse_device.fit import (
    DEV_FIELD_BY_NAME,
    FitRecord,
    FitWriter,
    fit_timestamp,
)

from synapse24.signal_quality.ecg import compute_hrv_metrics, detect_r_peaks_neurokit
from synapse24.utils.windows import window_bounds

__all__ = [
    "ExportError",
    "ExportSummary",
    "TimeAnchor",
    "export_session_fit",
    "load_xdf_streams",
    "main",
    "select_primary_stream",
]

# Stream types that can carry an HRV analysis, in order of preference.
PRIMARY_STREAM_TYPES = ("ECG", "PPG")


class ExportError(RuntimeError):
    """Raised when a session cannot be exported."""


def _find_float(node: Any, keys: tuple[str, ...], depth: int = 0) -> float | None:
    """Find the first numeric value under ``keys`` in a nested dict/list tree."""
    if depth > 8:
        return None
    if isinstance(node, dict):
        for key in keys:
            if key in node:
                value = node[key]
                if isinstance(value, list):
                    value = value[0] if value else None
                if isinstance(value, int | float | str):
                    try:
                        return float(value)
                    except (TypeError, ValueError):
                        pass
        for value in node.values():
            found = _find_float(value, keys, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_float(value, keys, depth + 1)
            if found is not None:
                return found
    return None


_UTC_KEYS = ("utc0_s", "utc0", "utc_epoch_s")
_LSL_KEYS = ("lsl_t0_s", "lsl_start_s", "lsl_time_at_start_s")


@dataclass(frozen=True)
class TimeAnchor:
    """The Unix epoch that corresponds to a known LSL-domain instant.

    ``utc0_s`` is the wall-clock time at LSL time ``lsl_t0_s``. A session gate
    report (or the XDF stream metadata) carries both; relying on manual CLI
    flags for real sessions is what made exports undateable in practice.
    """

    utc0_s: float
    lsl_t0_s: float | None = None

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> TimeAnchor | None:
        """Read an anchor from a gate report / session metadata dict."""
        utc0 = _find_float(data, _UTC_KEYS)
        if utc0 is None:
            return None
        return cls(utc0_s=utc0, lsl_t0_s=_find_float(data, _LSL_KEYS))


def _anchor_from_xdf_info(info: dict[str, Any]) -> TimeAnchor | None:
    """Read an anchor declared in the stream's own XDF ``<desc>`` metadata."""
    desc = info.get("desc", [])
    utc0 = _find_float(desc, _UTC_KEYS)
    if utc0 is None:
        return None
    return TimeAnchor(utc0_s=utc0, lsl_t0_s=_find_float(desc, _LSL_KEYS))


@dataclass(frozen=True)
class StreamView:
    """One stream of an XDF session."""

    name: str
    type: str
    timestamps: np.ndarray
    data: np.ndarray
    nominal_srate: float
    #: anchor declared by the file itself, if any (self-describing session).
    time_anchor: TimeAnchor | None = None

    @property
    def n_samples(self) -> int:
        """Number of samples in the stream."""
        return int(self.data.shape[0])

    @property
    def duration_s(self) -> float:
        """Time span covered by the stream."""
        if self.n_samples < 2:
            return 0.0
        return float(self.timestamps[-1] - self.timestamps[0])

    @property
    def sample_rate_hz(self) -> float:
        """Sampling rate estimated from the timestamps."""
        if self.n_samples < 2:
            return float(self.nominal_srate or 0.0)
        diffs = np.diff(self.timestamps)
        diffs = diffs[diffs > 0]
        if len(diffs) == 0:
            return float(self.nominal_srate or 0.0)
        return float(1.0 / np.median(diffs))


@dataclass
class ExportSummary:
    """What an export produced."""

    out_path: Path
    n_records: int
    n_windows: int
    primary_stream: str
    sample_rate_hz: float
    duration_s: float
    warnings: list[str] = field(default_factory=list)


def load_xdf_streams(path: Path | str) -> list[StreamView]:
    """Read the streams of an XDF file."""
    import pyxdf  # imported lazily so the module imports without pyxdf

    p = Path(path)
    if not p.is_file():
        raise ExportError(f"XDF not found: {p}")

    # dejitter_timestamps=False: pyxdf's default re-times every segment with a linear fit and only
    # breaks it at a gap over 1 s, so a lost frame (0.13 s of ECG at 500 Hz) is smoothed away and
    # the beats after it keep contiguous times. The beat times here are the real ones.
    streams, _header = pyxdf.load_xdf(str(p), dejitter_timestamps=False)
    out: list[StreamView] = []
    for stream in streams:
        time_stamps = stream.get("time_stamps")
        time_series = stream.get("time_series")
        if time_stamps is None or time_series is None:
            continue
        info = stream.get("info", {})

        def _first(key: str, default: Any = "") -> Any:
            value = info.get(key, default)
            if isinstance(value, list):
                return value[0] if value else default
            return value if value is not None else default

        data = np.asarray(time_series)
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        out.append(
            StreamView(
                name=str(_first("name")),
                type=str(_first("type")),
                timestamps=np.asarray(time_stamps, dtype=np.float64).ravel(),
                data=data,
                nominal_srate=float(_first("nominal_srate", 0.0) or 0.0),
                time_anchor=_anchor_from_xdf_info(info),
            )
        )
    return out


def select_primary_stream(streams: list[StreamView]) -> StreamView:
    """Select the stream to analyse, by type.

    ECG first, PPG second. A stream that is neither is never selected
    silently: analysing the wrong signal produces plausible but meaningless
    HRV numbers.
    """
    for wanted in PRIMARY_STREAM_TYPES:
        for stream in streams:
            haystack = f"{stream.name} {stream.type}".upper()
            if wanted in haystack:
                return stream
    available = ", ".join(sorted(f"{s.name}({s.type})" for s in streams)) or "none"
    raise ExportError(
        f"no analysable stream in the session (looked for {PRIMARY_STREAM_TYPES}, found: {available})"
    )


#: Timestamps from 2017 on are wall-clock seconds; a local LSL clock never gets there.
_UNIX_EPOCH_PLAUSIBLE_S = 1_500_000_000.0


def export_session_fit(  # noqa: PLR0915 - complex export pipeline (decode, anchor, HRV, write)
    xdf_path: Path | str,
    out_path: Path | str,
    serial: str = "SYN-000000",
    window_s: float = 30.0,
    utc0_s: float | None = None,
    *,
    session_metadata: dict[str, Any] | Path | str | None = None,
    lsl_t0_s: float | None = None,
) -> ExportSummary:
    """Export an XDF session to a FIT file with Synapse developer data.

    XDF timestamps live in the LSL clock domain (``pylsl.local_clock``),
    which is *not* Unix epoch. To write correct FIT wall-clock times we need
    an anchor: the Unix epoch that corresponds to LSL time ``t0`` (the first
    sample). Without it, timestamps that already look like Unix time
    (wall-clock recordings, as ``synapse record`` writes) are taken as such,
    with a warning; a local LSL clock raises :class:`ExportError`, because
    the date is unknown and a made-up one does not belong in a health record.

    Args:
        xdf_path: Input XDF file.
        out_path: Output FIT file.
        serial: Device serial written in the FIT file id.
        window_s: Analysis window length, in seconds.
        utc0_s: Unix epoch seconds corresponding to LSL time ``t0`` (the
            first sample timestamp). Required for a local LSL clock. Explicit
            CLI values always override a recorded anchor.
        session_metadata: Gate report / session metadata dict (or path to a
            JSON file) carrying ``utc0_s`` and optionally ``lsl_t0_s``.
        lsl_t0_s: LSL-domain instant corresponding to ``utc0_s``; overrides the
            recorded value. Defaults to the first sample timestamp.

    Returns:
        An :class:`ExportSummary` describing what was written.
    """
    if window_s <= 0:
        raise ExportError(f"window must be positive, got {window_s}")

    streams = load_xdf_streams(xdf_path)
    if not streams:
        raise ExportError(f"no usable stream found in {xdf_path}")
    primary = select_primary_stream(streams)

    signal = primary.data
    if signal.ndim > 1:
        # The first channel of the stream: a single-channel stream is still
        # 2-D here, and a 2-D array makes the R-peak detector fail.
        signal = signal[:, 0]
    signal = signal.astype(np.float64)
    timestamps = primary.timestamps
    fs = primary.sample_rate_hz
    if fs <= 0:
        raise ExportError(f"cannot estimate the sampling rate of stream {primary.name!r}")

    t0 = float(timestamps[0])
    t1 = float(timestamps[-1])
    duration_s = t1 - t0

    # Anchor the timestamps to Unix epoch. LSL local_clock() is NOT Unix epoch (it counts from
    # the machine's start), so a FIT written from it would be dated 1970. The anchor is taken,
    # in order: (1) the explicit utc0_s argument (CLI override), (2) a gate report / session
    # metadata dict passed in, (3) metadata declared in the XDF stream itself, (4) the value
    # itself when it is already wall-clock time (`synapse record`). A local clock with none of
    # those is refused: inventing a date puts a wrong one in a health record.
    warnings: list[str] = []

    anchor: TimeAnchor | None = None
    if session_metadata is not None:
        if isinstance(session_metadata, Path | str):
            meta_path = Path(session_metadata)
            if not meta_path.is_file():
                raise ExportError(f"session metadata file not found: {meta_path}")
            try:
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ExportError(f"cannot read session metadata {meta_path}: {exc}") from exc
        else:
            metadata = session_metadata
        if not isinstance(metadata, dict):
            raise ExportError("session metadata must be a JSON object or a dict")
        anchor = TimeAnchor.from_mapping(metadata)

    if anchor is None and utc0_s is None:
        # A self-describing XDF carries its own anchor; do not require a flag.
        anchor = next((s.time_anchor for s in streams if s.time_anchor is not None), None)
        if anchor is not None:
            warnings.append("UTC anchor read from the XDF stream metadata")

    lsl_anchor = lsl_t0_s
    if anchor is not None:
        if utc0_s is None:
            utc0_s = anchor.utc0_s
            warnings.append("UTC anchor read from session metadata")
        if lsl_anchor is None:
            lsl_anchor = anchor.lsl_t0_s
    if lsl_anchor is None:
        lsl_anchor = t0

    if utc0_s is None:
        if t0 < _UNIX_EPOCH_PLAUSIBLE_S:
            raise ExportError(
                f"the XDF timestamps (t0={t0:.3f}) are a local LSL clock, not dates: "
                "pass the Unix time of the first sample (--utc0-s), or a gate report "
                "with utc0_s/lsl_t0_s via session_metadata, to date the FIT file"
            )
        utc0_s = t0
        lsl_anchor = t0
        warnings.append(
            f"no UTC anchor given: the XDF timestamps (t0={t0:.3f}) are taken as Unix epoch "
            "(wall-clock timestamps, as `synapse record` writes them)"
        )

    def _utc(lsl_ts: float) -> float:
        """Map one LSL-domain timestamp to Unix epoch seconds."""
        return utc0_s + (lsl_ts - lsl_anchor)

    try:
        r_peaks = detect_r_peaks_neurokit(signal, int(round(fs)))
    except Exception as exc:  # pragma: no cover - depends on the signal
        warnings.append(f"R-peak detection failed: {exc}")
        r_peaks = np.array([], dtype=np.int64)

    # R-peak times in the same clock domain as the stream timestamps, READ from the stream's own
    # timestamps. `t0 + index / fs` assumed no sample was ever lost: after a lost frame every later
    # beat was placed too early, the interval across the hole came out too short, and one lost
    # frame in a window produced a fake RMSSD and a wrong heart rate in the exported file.
    peak_times = (
        np.asarray(timestamps, dtype=np.float64)[r_peaks]
        if len(r_peaks)
        else np.array([], dtype=np.float64)
    )
    fs_int = int(round(fs))

    writer = FitWriter()
    writer.write_file_header()
    writer.write_file_id(
        serial_number=_serial_to_int(serial),
        time_created=fit_timestamp(datetime.fromtimestamp(_utc(t0), tz=UTC)),
    )
    writer.write_developer_data_id()
    writer.write_field_descriptions()

    bounds = window_bounds(t0, t1, window_s)
    n_windows = len(bounds)
    n_records = 0
    for win_t0, win_t1 in bounds:
        mask = (peak_times >= win_t0) & (peak_times < win_t1)
        window_times = peak_times[mask]
        # The HRV code works on sample indices at an integer rate: hand it the beats as the sample
        # positions they would have had at the real times, so a hole in the recording is a long
        # interval (which the code can reject) and never a stretch that the sample count hides.
        window_peaks = (
            np.rint((window_times - window_times[0]) * fs_int).astype(np.int64)
            if len(window_times)
            else window_times.astype(np.int64)
        )
        metrics = (
            compute_hrv_metrics(window_peaks, fs_int, window_duration_s=win_t1 - win_t0)
            if len(window_peaks) >= 2
            else {}
        )

        dev: dict[str, int] = {
            "synapse_stress": 3,  # unknown: the triage labels live on the device
            "synapse_sqi": DEV_FIELD_BY_NAME["synapse_sqi"].invalid,
            "synapse_map": DEV_FIELD_BY_NAME["synapse_map"].invalid,
            "synapse_ecg_quality": DEV_FIELD_BY_NAME["synapse_ecg_quality"].invalid,
            "synapse_sleep_stage": DEV_FIELD_BY_NAME["synapse_sleep_stage"].invalid,
            "synapse_rmssd": DEV_FIELD_BY_NAME["synapse_rmssd"].invalid,
            "synapse_sdnn": DEV_FIELD_BY_NAME["synapse_sdnn"].invalid,
            "synapse_lf_hf": DEV_FIELD_BY_NAME["synapse_lf_hf"].invalid,
        }
        if "rmssd_ms" in metrics:
            dev["synapse_rmssd"] = int(round(metrics["rmssd_ms"]))
        if "sdnn_ms" in metrics:
            dev["synapse_sdnn"] = int(round(metrics["sdnn_ms"]))
        if "lf_hf_ratio" in metrics:
            dev["synapse_lf_hf"] = int(round(metrics["lf_hf_ratio"] * 100))
        heart_rate = int(round(metrics.get("hr_mean_bpm", 0))) if metrics else 0

        writer.write_record(
            FitRecord(
                timestamp=fit_timestamp(datetime.fromtimestamp(_utc(win_t0), tz=UTC)),
                heart_rate=heart_rate if 0 < heart_rate <= 255 else 255,
                dev=dev,
            )
        )
        n_records += 1

    out = Path(out_path)
    writer.finalize(out)
    return ExportSummary(
        out_path=out,
        n_records=n_records,
        n_windows=n_windows,
        primary_stream=f"{primary.name} ({primary.type})",
        sample_rate_hz=fs,
        duration_s=duration_s,
        warnings=warnings,
    )


def _serial_to_int(serial: str) -> int:
    """Convert a ``SYN-XXXXXX`` serial into the FIT uint32z serial number."""
    digits = "".join(ch for ch in serial if ch.isdigit())
    if not digits:
        return 0xFFFFFFFF
    return int(digits) & 0xFFFFFFFF


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (``synapse-export``)."""
    parser = argparse.ArgumentParser(
        prog="synapse-export",
        description="Export a SYNAPSE-24 XDF session to a FIT file with Synapse developer data.",
    )
    parser.add_argument("xdf", type=Path, help="Input XDF file")
    parser.add_argument("out", type=Path, help="Output FIT file")
    parser.add_argument("--device-id", default="SYN-000000", help="Device serial (SYN-XXXXXX)")
    parser.add_argument("--window-s", type=float, default=30.0, help="HRV window in seconds")
    parser.add_argument(
        "--utc0-s",
        type=float,
        default=None,
        help="Unix epoch seconds at LSL time t0 (first sample). Overrides any recorded anchor; required only when the XDF timestamps are a local LSL clock with no recorded anchor or session metadata.",
    )
    parser.add_argument(
        "--session-metadata",
        type=Path,
        default=None,
        help="Gate report / session metadata JSON carrying utc0_s and optionally lsl_t0_s.",
    )
    parser.add_argument(
        "--lsl-t0-s",
        type=float,
        default=None,
        help="LSL time corresponding to utc0_s (overrides the recorded value).",
    )
    args = parser.parse_args(argv)

    if not args.xdf.is_file():
        parser.error(f"XDF not found: {args.xdf}")

    try:
        summary = export_session_fit(
            args.xdf,
            args.out,
            args.device_id,
            args.window_s,
            args.utc0_s,
            session_metadata=args.session_metadata,
            lsl_t0_s=args.lsl_t0_s,
        )
    except ExportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for warning in summary.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(
        f"Exported {summary.n_records} record(s) to {summary.out_path} "
        f"from {summary.primary_stream} @ {summary.sample_rate_hz:.1f} Hz "
        f"({summary.duration_s:.1f} s)."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
