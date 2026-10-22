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
import math
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

__all__ = [
    "ExportError",
    "ExportSummary",
    "export_session_fit",
    "load_xdf_streams",
    "main",
    "select_primary_stream",
]

# Stream types that can carry an HRV analysis, in order of preference.
PRIMARY_STREAM_TYPES = ("ECG", "PPG")


class ExportError(RuntimeError):
    """Raised when a session cannot be exported."""


@dataclass(frozen=True)
class StreamView:
    """One stream of an XDF session."""

    name: str
    type: str
    timestamps: np.ndarray
    data: np.ndarray
    nominal_srate: float

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

    streams, _header = pyxdf.load_xdf(str(p))
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


def export_session_fit(
    xdf_path: Path | str,
    out_path: Path | str,
    serial: str = "SYN-000000",
    window_s: float = 30.0,
) -> ExportSummary:
    """Export an XDF session to a FIT file with Synapse developer data.

    Args:
        xdf_path: Input XDF file.
        out_path: Output FIT file.
        serial: Device serial written in the FIT file id.
        window_s: Analysis window length, in seconds.

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

    warnings: list[str] = []
    try:
        r_peaks = detect_r_peaks_neurokit(signal, int(round(fs)))
    except Exception as exc:  # pragma: no cover - depends on the signal
        warnings.append(f"R-peak detection failed: {exc}")
        r_peaks = np.array([], dtype=np.int64)

    # R-peak times in the same clock domain as the stream timestamps: the
    # peak index is converted with the stream period, never compared with
    # absolute window boundaries.
    peak_times = t0 + (r_peaks / fs) if len(r_peaks) else np.array([], dtype=np.float64)

    writer = FitWriter()
    writer.write_file_header()
    writer.write_file_id(
        serial_number=_serial_to_int(serial),
        time_created=fit_timestamp(datetime.fromtimestamp(t0, tz=UTC)),
    )
    writer.write_developer_data_id()
    writer.write_field_descriptions()

    n_windows = max(1, math.ceil(duration_s / window_s))
    n_records = 0
    for index in range(n_windows):
        win_t0 = t0 + index * window_s
        win_t1 = min(t0 + (index + 1) * window_s, t1)
        mask = (peak_times >= win_t0) & (peak_times < win_t1)
        window_peaks = r_peaks[mask]
        metrics = (
            compute_hrv_metrics(window_peaks, int(round(fs)), window_duration_s=window_s)
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
                timestamp=fit_timestamp(datetime.fromtimestamp(win_t0, tz=UTC)),
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
    args = parser.parse_args(argv)

    if not args.xdf.is_file():
        parser.error(f"XDF not found: {args.xdf}")

    try:
        summary = export_session_fit(args.xdf, args.out, args.device_id, args.window_s)
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
