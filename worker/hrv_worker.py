"""Synapse stateless inference worker — HRV trends job (first job type).

Reads a stored FIT or XDF file referenced by an Averyn job, computes RMSSD /
SDNN / mean HR per window, and returns a `JobResult` keyed to the job id.

The worker is intentionally dependency-light: pyxdf (XDF), a hand-rolled FIT
reader for the Synapse developer fields, and NeuroKit2 for R-peak detection.
No user identifier ever enters this process — Averyn attaches the result to
the user's activity as imported data (source: "synapse-worker").

Run standalone for lab testing:
    uv run python worker/hrv_worker.py --job '{"job_id":"...","computation_type":"hrv_trends",...}'
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

# project imports
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from synapse24.signal_quality.ecg import detect_r_peaks_neurokit, compute_hrv_metrics

# Allow running both as `python worker/hrv_worker.py` and as a module.
try:
    from worker.job_contract import Job, JobResult, JobType  # package import
except ImportError:  # pragma: no cover - executed as a script inside worker/
    from job_contract import Job, JobResult, JobType

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


def read_fit_developer_records(path: Path) -> list[dict]:
    """Yield per-record developer field values from a FIT file.

    Only understands the flat Synapse developer layout written by
    scripts/export_session_fit.py: message 209 (developer data) with a
    timestamp + developer_data_index + fields in fixed order.
    """
    data = path.read_bytes()
    if len(data) < 14 or data[8:12] != b".FIT":
        return []

    records: list[dict] = []
    pos = 14  # skip header (+ header CRC already counted)
    end = len(data)
    # local msg type -> global mesg num (from definition messages)
    defs: dict[int, int] = {}
    # local msg type -> total data-bytes per record (computed from def fields)
    def_data_len: dict[int, int] = {}
    # local msg types used for developer data (global mesg num 209)
    dev_local_types: set[int] = set()

    def _field_bytes(base_type: int) -> int:
        return {0: 1, 2: 1, 131: 1, 132: 2, 133: 4, 134: 4, 136: 4, 137: 8}.get(base_type, 1)

    while pos < end:
        b = data[pos]
        pos += 1
        is_def = bool(b & 0x80)
        lt = b & 0x3F
        if is_def:
            # definition: reserved(1), arch(1), global mesg num(2), num fields(1)
            if pos + 5 > end:
                break
            pos += 2  # reserved + arch
            gnum = struct.unpack_from("<H", data, pos)[0]
            pos += 2
            nfields = data[pos]
            pos += 1
            defs[lt] = gnum
            if gnum == 209:
                dev_local_types.add(lt)
            total = 0
            for _ in range(nfields):
                if pos + 3 > end:
                    break
                _fnum = data[pos]
                fsize = data[pos + 1]
                ftype = data[pos + 2]
                pos += 3
                # Field size is declared explicitly in the definition; do not
                # derive it from base_type (STRING is 17 bytes, base_type=7).
                total += fsize
            def_data_len[lt] = total
            # definition messages also carry a 2-byte CRC after the fields
            if pos + 2 <= end:
                pos += 2
            continue

        # Data message: 1 byte header already consumed. If we know its size,
        # skip it unless it is the developer data block.
        if lt in dev_local_types:
            # timestamp(u32) + developer_data_index(u8) + values
            if pos + 5 > end:
                break
            ts = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            dev_idx = data[pos]
            pos += 1
            vals: dict[str, int] = {}
            for field_id in (
                SYNAPSE_DEV_FIELD_STRESS,
                SYNAPSE_DEV_FIELD_SQI,
                SYNAPSE_DEV_FIELD_MAP,
                SYNAPSE_DEV_FIELD_ECG_QUALITY,
                SYNAPSE_DEV_FIELD_SLEEP_STAGE,
                SYNAPSE_DEV_FIELD_RMSSD,
                SYNAPSE_DEV_FIELD_SDNN,
                SYNAPSE_DEV_FIELD_LF_HF,
            ):
                if pos + 1 > end:
                    break
                v = data[pos]
                pos += 1
                if field_id in (SYNAPSE_DEV_FIELD_RMSSD, SYNAPSE_DEV_FIELD_SDNN, SYNAPSE_DEV_FIELD_LF_HF):
                    if pos + 1 > end:
                        break
                    v |= data[pos] << 8
                    pos += 1
                vals[field_id] = v
            records.append({"timestamp": ts, "dev": vals})
            # developer data also ends with a 2-byte CRC
            if pos + 2 <= end:
                pos += 2
        elif lt in def_data_len:
            # standard record: skip whole payload + CRC
            skip = def_data_len[lt]
            if pos + skip + 2 > end:
                break
            pos += skip + 2
        else:
            # Unknown data message without a definition: stop rather than corrupt.
            break

    return records


def read_xdf_hrv(path: Path, window_s: float = 30.0) -> list[dict]:
    """Compute RMSSD/SDNN per window from an XDF's ECG/PPG stream."""
    import pyxdf  # imported lazily so the module imports without it

    streams, _ = pyxdf.load_xdf(str(path))
    best = None
    for s in streams:
        ts = s.get("time_stamps")
        series = s.get("time_series")
        if ts is None or series is None:
            continue
        if best is None or len(ts) > len(best["timestamps"]):
            best = {"timestamps": __import__("numpy").asarray(ts, dtype=float).ravel(),
                    "data": __import__("numpy").asarray(series)}
    if best is None:
        return []

    import numpy as np

    sig = best["data"]
    if sig.ndim > 1:
        sig = sig[:, 0]
    sig = sig.astype(np.float64)
    ts = best["timestamps"]
    fs = 1.0 / np.median(np.diff(ts[ts > 0])) if len(ts) > 1 else 500.0

    try:
        r_peaks = detect_r_peaks_neurokit(sig, int(fs))
    except Exception:
        r_peaks = np.array([], dtype=np.int64)

    rr_times = r_peaks / fs if len(r_peaks) else np.array([])
    t0, t1 = float(ts[0]), float(ts[-1])
    out = []
    n_windows = max(1, int((t1 - t0) // window_s))
    for wi in range(n_windows):
        w0 = t0 + wi * window_s
        w1 = min(t0 + (wi + 1) * window_s, t1)
        mask = (rr_times >= w0) & (rr_times < w1)
        win = r_peaks[mask]
        hrv = compute_hrv_metrics(win, int(fs)) if len(win) >= 2 else {}
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


def run_hrv_trends(job: Job, file_path: Path) -> JobResult:
    """Compute HRV trends from a stored file."""
    if file_path.suffix.lower() == ".fit":
        records = read_fit_developer_records(file_path)
        # FIT records already carry rmssd/sdnn per window: pass them through.
        payload = {
            "windows": [
                {
                    "window_start": r["timestamp"],
                    "rmssd_ms": r["dev"].get(SYNAPSE_DEV_FIELD_RMSSD),
                    "sdnn_ms": r["dev"].get(SYNAPSE_DEV_FIELD_SDNN),
                    "stress_level": r["dev"].get(SYNAPSE_DEV_FIELD_STRESS),
                    "sqi": r["dev"].get(SYNAPSE_DEV_FIELD_SQI),
                    "map": r["dev"].get(SYNAPSE_DEV_FIELD_MAP),
                }
                for r in records
            ],
            "source": "fit-file",
        }
    else:
        windows = read_xdf_hrv(file_path)
        payload = {"windows": windows, "source": "xdf-file"}

    return JobResult(
        job_id=job.job_id,
        ok=True,
        payload=payload,
        algorithm_version="hrv-trends-py-v1",
    )


def dispatch(job_json: str, file_path: Path) -> str:
    """Entry point for standalone / container testing."""
    job = Job.from_json(job_json)
    if job.computation_type != JobType.HRV_TRENDS:
        result = JobResult(
            job_id=job.job_id,
            ok=False,
            error=f"unsupported computation type {job.computation_type.value}",
            algorithm_version="hrv-trends-py-v1",
        )
    else:
        result = run_hrv_trends(job, file_path)
    return result.to_json()


def main() -> int:
    parser = argparse.ArgumentParser(description="Synapse stateless worker (HRV trends job)")
    parser.add_argument("--job", required=True, help="Job JSON (see worker/job_contract.py)")
    parser.add_argument("--file", required=True, type=Path, help="Stored file (FIT or XDF) to process")
    args = parser.parse_args()

    print(dispatch(args.job, args.file))
    return 0


if __name__ == "__main__":
    sys.exit(main())
