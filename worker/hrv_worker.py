"""Synapse stateless inference worker.

Reads a stored FIT or XDF file referenced by an Averyn job and returns a
`JobResult` keyed to the job id. Supported computation types:

* ``hrv_trends``      — RMSSD/SDNN/mean HR per window (FIT dev fields or XDF ECG).
* ``stress_daily``    — daily stress summary from the edge triage labels already
                        stored in the FIT developer field ``synapse_stress``.
* ``sqi_report``      — signal quality report per window from FIT dev fields
                        (``synapse_sqi``/``synapse_map``/``synapse_ecg_quality``).
* ``sleep_staging``   — YASA sleep staging on an EEG stream from an XDF file.
                        EEG input only; never a PPG/IMU proxy.

The worker is intentionally dependency-light: pyxdf (XDF), a hand-rolled FIT
reader for the Synapse developer fields, and NeuroKit2 for R-peak detection.
No user identifier ever enters this process — Averyn attaches the result to the
user's activity as imported data (source: "synapse-worker").

Run standalone for lab testing:
    uv run python worker/hrv_worker.py --job '{"job_id":"...","computation_type":"hrv_trends",...}' --file session.fit
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

# project imports
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python"))
from synapse_device.fit import DEV_FIELDS, FIT_EPOCH, read_fit

from synapse24.signal_quality.ecg import compute_hrv_metrics, detect_r_peaks_neurokit
from synapse24.signal_quality.ppg import detect_ppg_peaks
from synapse24.utils.windows import window_bounds

# Allow running both as `python worker/hrv_worker.py` and as a module.
try:
    from worker.job_contract import (  # package import
        Job,
        JobResult,
        JobType,
    )
except ImportError:  # pragma: no cover - executed as a script inside worker/
    from job_contract import (  # type: ignore[no-redef,import-not-found]
        Job,
        JobResult,
        JobType,
    )

# Payload JSON Schema (canonical, in contracts/). Validated lazily so the worker
# imports on hosts without jsonschema installed.
try:  # pragma: no cover - jsonschema is a dev/CI dependency
    import jsonschema

    _HAS_JSONSCHEMA = True
except ImportError:  # pragma: no cover
    jsonschema = None  # type: ignore[assignment]
    _HAS_JSONSCHEMA = False

_WORKER_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "contracts" / "schemas" / "worker_job_result.json"
)


@lru_cache(maxsize=1)
def _load_worker_schema() -> dict | None:
    if not _WORKER_SCHEMA_PATH.is_file():
        return None
    schema = json.loads(_WORKER_SCHEMA_PATH.read_text(encoding="utf-8"))
    return schema if isinstance(schema, dict) else None


def _validate_payload(computation_type: JobType, payload: dict | None) -> list[str]:
    """Return validation errors (empty when payload conforms to the schema)."""
    if not _HAS_JSONSCHEMA or payload is None:
        return []
    schema = _load_worker_schema()
    if schema is None:
        return []
    subschema = schema.get("payloads", {}).get(computation_type.value)
    if subschema is None:
        return []
    try:
        jsonschema.validate(instance=payload, schema=subschema)
    except jsonschema.ValidationError as exc:  # type: ignore[attr-defined]
        return [f"{computation_type.value}: {exc.message}"]
    return []


# FIT constants (mirror scripts/export_session_fit.py)
FIT_MESG_RECORD = 20
FIT_MESG_DEVELOPER_DATA = 209
FIT_BASE_TYPE_UINT8 = 2
FIT_BASE_TYPE_UINT16 = 132
FIT_BASE_TYPE_UINT32 = 134

SYNAPSE_DEV_FIELD_STRESS = 0
SYNAPSE_DEV_FIELD_SQI = 1
SYNAPSE_DEV_FIELD_MAP = 2
SYNAPSE_DEV_FIELD_ECG_QUALITY = 3
SYNAPSE_DEV_FIELD_SLEEP_STAGE = 4
SYNAPSE_DEV_FIELD_RMSSD = 5
SYNAPSE_DEV_FIELD_SDNN = 6
SYNAPSE_DEV_FIELD_LF_HF = 7

# Invalid sentinels (uint8 255 / uint16 0xFFFF per the FIT descriptor)
DEV_FIELD_INVALID_UINT8 = 255
DEV_FIELD_INVALID_UINT16 = 0xFFFF

# Seconds from the Unix epoch to the FIT epoch, taken from the SDK's own constant (one
# definition). Every window_start in a job payload is Unix seconds, whichever file it came from.
FIT_TO_UNIX_OFFSET_S = int((FIT_EPOCH - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds())

# synapse_stress = 3 means "the triage did not run": a missing label, not a baseline.
STRESS_UNKNOWN = 3

# ADR-0014 sleep classes are 0=W 1=N1 2=N2 3=N3 4=REM; anything else (YASA's "unscored")
# is reported as the field's invalid value, never as a stage.
SLEEP_STAGE_NA = 255


def _fit_to_unix(fit_ts: object) -> float | None:
    """FIT timestamp (seconds since 1989-12-31) -> Unix seconds; ``None`` stays ``None``."""
    if isinstance(fit_ts, (int, float)):
        return float(fit_ts) + FIT_TO_UNIX_OFFSET_S
    return None


def _timed(records: list[dict]) -> list[dict]:
    """Keep the records that can be placed in time (a window_start must be a number)."""
    return [r for r in records if _fit_to_unix(r["timestamp"]) is not None]


def _stage_code(stage: int) -> int:
    """A classifier's stage code -> the contract's code (0..4, else 255)."""
    return int(stage) if 0 <= int(stage) <= 4 else SLEEP_STAGE_NA


# ---------------------------------------------------------------------------
# FIT reader (canonical implementation: synapse_device.fit)
# ---------------------------------------------------------------------------

# Contract field id -> name, so the reader's output can be keyed the way the
# job payloads expect.
_DEV_FIELD_ID_BY_NAME = {field.name: field.field_id for field in DEV_FIELDS}


def read_fit_developer_records(path: Path) -> list[dict]:
    """Return per-record developer field values from a FIT file.

    The FIT decoding is the SDK's canonical reader (one implementation for
    the whole project); this adapter only re-keys the developer values with
    the contract field ids.
    """
    records: list[dict] = []
    for record in read_fit(path).records:
        dev = {
            _DEV_FIELD_ID_BY_NAME.get(name, name): int(value)
            for name, value in (record.get("dev") or {}).items()
        }
        records.append({"timestamp": record["timestamp"], "dev": dev})
    return records


# ---------------------------------------------------------------------------
# XDF readers
# ---------------------------------------------------------------------------


def read_xdf_hrv(path: Path, window_s: float = 30.0) -> list[dict]:
    """Compute RMSSD/SDNN per window from an XDF's ECG/PPG stream.

    The analysed stream is selected **by type** (ECG first, then PPG): the
    stream with the most samples may be an IMU stream, and analysing it
    would produce plausible but meaningless numbers.

    Every instant lives in the same clock domain: the R-peak times are
    derived from the stream timestamps (``t0 + index / fs``), so a window
    boundary is an absolute instant.
    """
    import numpy as np
    import pyxdf  # imported lazily so the module imports without it

    streams, _ = pyxdf.load_xdf(str(path))
    candidates: list[tuple[str, str, np.ndarray, np.ndarray]] = []
    for s in streams:
        ts = s.get("time_stamps")
        series = s.get("time_series")
        if ts is None or series is None:
            continue
        info = s.get("info", {}) or {}

        def _first(key: str) -> str:
            value = info.get(key, "")
            if isinstance(value, list):
                value = value[0] if value else ""
            return str(value or "")

        candidates.append(
            (
                _first("name"),
                _first("type"),
                np.asarray(ts, dtype=float).ravel(),
                np.asarray(series),
            )
        )

    selected = None
    kind = ""
    for wanted in ("ECG", "PPG"):
        for name, stype, ts, series in candidates:
            if wanted in f"{name} {stype}".upper():
                selected = (name, stype, ts, series)
                kind = wanted
                break
        if selected is not None:
            break
    if selected is None:
        return []

    _name, _type, ts, series = selected
    sig = series
    if sig.ndim > 1:
        sig = sig[:, 0]
    sig = sig.astype(np.float64)
    if len(ts) < 2:
        return []

    diffs = np.diff(ts)
    diffs = diffs[diffs > 0]
    fs = 1.0 / np.median(diffs) if len(diffs) else 500.0

    # int(fs) truncated 49.99 Hz to 49 (a 2 % error on every interval); round instead.
    fs_int = int(round(fs))
    if fs_int < 8:
        # The cardiac band tops out at 4-8 Hz: below this the filter itself is invalid, and
        # an empty list would read as "no beats found" rather than "cannot analyse".
        raise ValueError(f"sampling rate {fs:.2f} Hz is too low for peak detection")
    # The R-peak detector belongs to the ECG: on a PPG trace it returns plausible but
    # meaningless beats, so a PPG stream gets its own systolic-peak detector.
    detect = detect_r_peaks_neurokit if kind == "ECG" else detect_ppg_peaks
    try:
        r_peaks = detect(sig, fs_int)
    except Exception as exc:  # noqa: BLE001 - the detectors raise many types on short or flat input
        logging.getLogger(__name__).warning("%s peak detection failed: %s", kind, exc)
        r_peaks = np.array([], dtype=np.int64)

    t0, t1 = float(ts[0]), float(ts[-1])
    # Absolute time of every R-peak (the peak index is a position in the
    # signal, not an instant: mixing the two produced empty windows).
    peak_times = t0 + (r_peaks / fs) if len(r_peaks) else np.array([], dtype=np.float64)

    out = []
    for w0, w1 in window_bounds(t0, t1, window_s):
        mask = (peak_times >= w0) & (peak_times < w1)
        win = r_peaks[mask]
        hrv = compute_hrv_metrics(win, fs_int, window_duration_s=w1 - w0) if len(win) >= 2 else {}
        out.append(
            {
                "window_start": w0,
                "window_end": w1,
                "rmssd_ms": hrv.get("rmssd_ms"),
                "sdnn_ms": hrv.get("sdnn_ms"),
                "mean_rr_ms": hrv.get("mean_rr_ms"),
                "hr_mean_bpm": hrv.get("hr_mean_bpm"),
            }
        )
    return out


def _iter_xdf_streams(path: Path) -> Iterator[tuple[dict[str, Any], Any, Any]]:
    """Yield (stream_info_dict, timestamps, series) for every XDF stream."""
    import numpy as np
    import pyxdf

    streams, _ = pyxdf.load_xdf(str(path))
    for s in streams:
        info = s.get("info", {}) or {}
        name = (
            (info.get("name") or [""])[0]
            if isinstance(info.get("name"), list)
            else str(info.get("name", ""))
        )
        stype = (
            (info.get("type") or [""])[0]
            if isinstance(info.get("type"), list)
            else str(info.get("type", ""))
        )
        ts = s.get("time_stamps")
        series = s.get("time_series")
        if ts is None or series is None:
            continue
        nominal = (
            info.get("nominal_srate", [0])[0]
            if isinstance(info.get("nominal_srate"), list)
            else info.get("nominal_srate", 0)
        )
        try:
            fs = float(nominal)
        except (TypeError, ValueError):
            fs = 0.0
        yield (
            {
                "name": name,
                "type": stype,
                "sampling_rate_hz": fs,
            },
            np.asarray(ts, dtype=float).ravel(),
            np.asarray(series),
        )


def read_xdf_eeg_streams(path: Path) -> list[dict]:
    """Return EEG streams from an XDF as [{signal, sampling_rate, name}]."""
    out: list[dict] = []
    for info, ts, series in _iter_xdf_streams(path):
        if "EEG" not in info["type"].upper():
            continue
        sig = series
        if sig.ndim > 1:
            sig = sig[:, 0]
        out.append(
            {
                "name": info["name"] or "EEG",
                "signal": sig.astype(np.float64).ravel(),
                "sampling_rate": info["sampling_rate_hz"] or 250.0,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Job runners
# ---------------------------------------------------------------------------


def _valid_uint8(v: int | None) -> int | None:
    """Return None for the uint8 invalid sentinel, else the value."""
    if v is None:
        return None
    return None if v == DEV_FIELD_INVALID_UINT8 else int(v)


def _valid_uint16(v: int | None) -> int | None:
    """Return None for the uint16 invalid sentinel, else the value."""
    if v is None:
        return None
    return None if v == DEV_FIELD_INVALID_UINT16 else int(v)


def run_hrv_trends(job: Job, file_path: Path) -> JobResult:
    """Compute HRV trends from a stored file."""
    if file_path.suffix.lower() == ".fit":
        records = _timed(read_fit_developer_records(file_path))
        # FIT records already carry rmssd/sdnn per window: pass them through.
        payload = {
            "windows": [
                {
                    "window_start": _fit_to_unix(r["timestamp"]),
                    "rmssd_ms": _valid_uint16(r["dev"].get(SYNAPSE_DEV_FIELD_RMSSD)),
                    "sdnn_ms": _valid_uint16(r["dev"].get(SYNAPSE_DEV_FIELD_SDNN)),
                    "mean_rr_ms": None,
                    "hr_mean_bpm": None,
                    "stress_level": _valid_uint8(r["dev"].get(SYNAPSE_DEV_FIELD_STRESS)),
                    "sqi": _valid_uint8(r["dev"].get(SYNAPSE_DEV_FIELD_SQI)),
                    "map": _valid_uint8(r["dev"].get(SYNAPSE_DEV_FIELD_MAP)),
                }
                for r in records
            ],
            "source": "fit-file",
        }
    else:
        try:
            windows = read_xdf_hrv(file_path)
        except ValueError as exc:
            return JobResult(
                job_id=job.job_id,
                ok=False,
                error=f"hrv_trends: {exc}",
                algorithm_version="hrv-trends-py-v1",
            )
        payload = {"windows": windows, "source": "xdf-file"}

    return JobResult(
        job_id=job.job_id,
        ok=True,
        payload=payload,
        algorithm_version="hrv-trends-py-v1",
    )


def run_stress_daily(job: Job, file_path: Path) -> JobResult:
    """Daily stress summary from the edge triage labels in a FIT file.

    Aggregates the ``synapse_stress`` developer field (0=baseline, 1=stress,
    2=artifact, 3=unknown) written by the edge TFLM triage. No server-side
    model is required — the labels already live on the device file.
    """
    if file_path.suffix.lower() != ".fit":
        return JobResult(
            job_id=job.job_id,
            ok=False,
            error="stress_daily requires a FIT file with synapse_stress developer fields",
            algorithm_version="stress-daily-py-v1",
        )

    records = read_fit_developer_records(file_path)
    raw_labels = [_valid_uint8(r["dev"].get(SYNAPSE_DEV_FIELD_STRESS)) for r in records]
    # 3 = "unknown": the triage did not run for that window. Counting it as a label
    # reported stress_level 0 (baseline) for a file with no labels at all. It keeps its
    # position (None) so it breaks a run of baseline/stress windows instead of bridging it.
    # Only 0 baseline / 1 stress / 2 artifact are labels: 3 (unknown) and any value a newer
    # firmware might add are not, so the three probabilities always sum to 1.
    labels = [l if l in (0, 1, 2) else None for l in raw_labels]
    n = sum(1 for l in labels if l is not None)
    if n == 0:
        reason = (
            "insufficient_data: no window carries a stress label (unknown or unrecognised)"
            if any(l is not None for l in raw_labels)
            else "no synapse_stress labels found in FIT file"
        )
        return JobResult(
            job_id=job.job_id,
            ok=False,
            error=reason,
            algorithm_version="stress-daily-py-v1",
        )

    n_stress = sum(1 for l in labels if l == 1)
    n_baseline = sum(1 for l in labels if l == 0)
    n_artifact = sum(1 for l in labels if l == 2)
    daily_average = (n_stress / n) * 100.0 if n else 0.0
    peak_stress = 1.0 if n_stress else 0.0

    # The covered window is the real duration spanned by the records, not a
    # record count: the payload field is a duration in seconds.
    timestamps = [r["timestamp"] for r in records if isinstance(r.get("timestamp"), (int, float))]
    window_s = float(max(timestamps) - min(timestamps)) if len(timestamps) > 1 else 0.0

    # Recovery periods: windows of >=3 consecutive baseline labels.
    recovery_periods = 0
    run = 0
    for l in labels:
        if l == 0:
            run += 1
        else:
            if run >= 3:
                recovery_periods += 1
            run = 0
    if run >= 3:
        recovery_periods += 1

    # Stress episodes: maximal runs of consecutive stress labels.
    stress_episodes = []
    run = 0
    start_idx = 0
    for i, l in enumerate(labels):
        if l == 1:
            if run == 0:
                start_idx = i
            run += 1
        elif run:
            stress_episodes.append({"start": start_idx, "end": i - 1, "len": run})
            run = 0
    if run:
        stress_episodes.append({"start": start_idx, "end": len(labels) - 1, "len": run})

    payload = {
        "stress_level": 1 if n_stress > n_baseline else 0,
        "stress_prob": round(n_stress / n, 4),
        "baseline_prob": round(n_baseline / n, 4),
        "artifact_prob": round(n_artifact / n, 4),
        "window_s": window_s,
        "daily_average": round(daily_average, 2),
        "peak_stress": peak_stress,
        "recovery_periods": recovery_periods,
        "stress_episodes": stress_episodes,
        "n_records": n,
        "source": "fit-file",
    }
    return JobResult(
        job_id=job.job_id,
        ok=True,
        payload=payload,
        algorithm_version="stress-daily-py-v1",
    )


def run_sqi_report(job: Job, file_path: Path) -> JobResult:
    """Signal quality report per window from FIT developer fields.

    Reads ``synapse_sqi`` (PPG SQI), ``synapse_map`` (motion artifact prob.)
    and ``synapse_ecg_quality`` per record.
    """
    if file_path.suffix.lower() != ".fit":
        return JobResult(
            job_id=job.job_id,
            ok=False,
            error="sqi_report requires a FIT file with synapse_sqi developer fields",
            algorithm_version="sqi-report-py-v1",
        )

    records = _timed(read_fit_developer_records(file_path))
    windows = []
    for r in records:
        windows.append(
            {
                "window_start": _fit_to_unix(r["timestamp"]),
                "sqi": _valid_uint8(r["dev"].get(SYNAPSE_DEV_FIELD_SQI)),
                "map": _valid_uint8(r["dev"].get(SYNAPSE_DEV_FIELD_MAP)),
                "ecg_quality": _valid_uint8(r["dev"].get(SYNAPSE_DEV_FIELD_ECG_QUALITY)),
            }
        )

    valid_sqi = [w["sqi"] for w in windows if w["sqi"] is not None]
    valid_map = [w["map"] for w in windows if w["map"] is not None]
    valid_ecq = [w["ecg_quality"] for w in windows if w["ecg_quality"] is not None]

    # window_s is the real duration covered by the records (seconds), not a
    # record count. Fall back to 0 when there is a single record / no timestamps.
    timestamps = [w["window_start"] for w in windows if isinstance(w["window_start"], (int, float))]
    window_s = float(max(timestamps) - min(timestamps)) if len(timestamps) > 1 else 0.0

    def _mean_int(values: Sequence[float]) -> int | None:
        return int(round(sum(values) / len(values))) if values else None

    payload = {
        "sqi": _mean_int(valid_sqi),
        "map": _mean_int(valid_map),
        "ecg_quality": _mean_int(valid_ecq),
        "window_s": window_s,
        "per_channel": windows,
        "source": "fit-file",
    }
    return JobResult(
        job_id=job.job_id,
        ok=True,
        payload=payload,
        algorithm_version="sqi-report-py-v1",
    )


def run_sleep_staging(job: Job, file_path: Path) -> JobResult:
    """Sleep staging on an EEG stream from an XDF file (YASA).

    EEG input only — a PPG/IMU-only file returns an error and never claims
    "YASA sleep staging", per worker/job_contract.py.
    """
    if file_path.suffix.lower() != ".xdf":
        return JobResult(
            job_id=job.job_id,
            ok=False,
            error="sleep_staging requires an XDF file with an EEG stream",
            algorithm_version="sleep-staging-yasa-py-v1",
        )

    eeg_streams = read_xdf_eeg_streams(file_path)
    if not eeg_streams:
        return JobResult(
            job_id=job.job_id,
            ok=False,
            error="no EEG stream found in XDF file",
            algorithm_version="sleep-staging-yasa-py-v1",
        )

    from synapse24.signal_quality.eeg import compute_yasa_sleep_staging  # noqa: PLC0415

    primary = eeg_streams[0]
    try:
        staging = compute_yasa_sleep_staging(
            eeg_signal=primary["signal"],
            sampling_rate=int(primary["sampling_rate"]),
            eeg_ch_name=primary["name"],
        )
    except Exception as exc:  # pragma: no cover - YASA/runtime failure
        return JobResult(
            job_id=job.job_id,
            ok=False,
            error=f"sleep staging failed: {exc}",
            algorithm_version="sleep-staging-yasa-py-v1",
        )

    hypnogram = staging["hypnogram"]
    confidence = staging["confidence"]
    payload = {
        "sleep_stage": _stage_code(hypnogram[-1]) if len(hypnogram) else None,
        # No confidence for an epoch with no stage: "80 % sure it is not available" means nothing.
        "confidence": (
            float(confidence[-1])
            if len(confidence) and len(hypnogram) and _stage_code(hypnogram[-1]) != SLEEP_STAGE_NA
            else None
        ),
        "epoch_s": 30.0,
        "hypnogram": [_stage_code(s) for s in hypnogram],
        "input_modality": "eeg",
        "aux_channels_used": staging.get("aux_channels_used", []),
        "source": "xdf-file",
        "n_epochs": int(len(hypnogram)),
    }
    return JobResult(
        job_id=job.job_id,
        ok=True,
        payload=payload,
        algorithm_version="sleep-staging-yasa-py-v1",
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_RUNNERS = {
    JobType.HRV_TRENDS: run_hrv_trends,
    JobType.STRESS_DAILY: run_stress_daily,
    JobType.SQI_REPORT: run_sqi_report,
    JobType.SLEEP_STAGING: run_sleep_staging,
}


def dispatch(job_json: str, file_path: Path) -> str:
    """Entry point for standalone / container testing.

    Never raises on malformed input: an unparseable or unknown-typed job
    returns an ok=False JobResult so Averyn can surface a clean error.
    """
    try:
        job = Job.from_json(job_json)
    except (ValueError, KeyError) as exc:
        result = JobResult(
            job_id="unknown",
            ok=False,
            error=f"invalid job payload: {exc}",
            algorithm_version="synapse-worker-v1",
        )
        return result.to_json()

    # file_sha256 integrity check: refuse to process a tampered file. Only
    # enforced when the caller actually supplied a sha256 (placeholder values
    # such as "" or "x" are skipped so lab/dispatch tests are not coupled to
    # file hashing).
    if (
        job.file_sha256
        and len(job.file_sha256) == 64
        and all(c in "0123456789abcdefABCDEF" for c in job.file_sha256)
    ):
        try:
            digest = hashlib.sha256(Path(file_path).read_bytes()).hexdigest()
        except OSError:
            digest = ""
        if digest != job.file_sha256.lower():
            result = JobResult(
                job_id=job.job_id,
                ok=False,
                error="file_sha256 mismatch: file may have been tampered",
                algorithm_version="synapse-worker-v1",
            )
            return result.to_json()

    runner = _RUNNERS.get(job.computation_type)
    if runner is None:
        result = JobResult(
            job_id=job.job_id,
            ok=False,
            error=f"unsupported computation type {job.computation_type.value}",
            algorithm_version="synapse-worker-v1",
        )
    else:
        result = runner(job, Path(file_path))

    # Contract enforcement: the payload must conform to its JSON Schema.
    if result.ok and result.payload is not None:
        errors = _validate_payload(job.computation_type, result.payload)
        if errors:
            result = JobResult(
                job_id=result.job_id,
                ok=False,
                error="schema violation: " + "; ".join(errors),
                algorithm_version=result.algorithm_version,
            )
    return result.to_json()


def main() -> int:
    parser = argparse.ArgumentParser(description="Synapse stateless inference worker")
    parser.add_argument("--job", required=True, help="Job JSON (see worker/job_contract.py)")
    parser.add_argument(
        "--file", required=True, type=Path, help="Stored file (FIT or XDF) to process"
    )
    args = parser.parse_args()

    print(dispatch(args.job, args.file))
    return 0


if __name__ == "__main__":
    sys.exit(main())
