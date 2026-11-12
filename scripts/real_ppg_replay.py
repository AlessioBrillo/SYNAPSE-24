"""A real finger PPG through the band's own beat-to-beat detector (MVP-1.8).

``firmware/esp32_band/signal_quality/synapse_rr.c`` says in its header that it "has never seen a real
finger". BIDMC (PhysioNet, ICU patients, PPG + ECG sampled together at 125 Hz) is the nearest thing
available without a sensor, so:

    PLETH 125 Hz -> 50 Hz -> IR counts as a reflective MAX30102 reports them (the count FALLS on a pulse)
        -> synapse_rr_push (the firmware's C code, compiled on the host) -> intervals
    ECG lead II  -> R peaks (NeuroKit) -> reference intervals
    per 30 s window: heart rate and RMSSD from the band vs from the ECG

What this does not prove: a MAX30102 (reflective, wrist or finger, its own LED current, ambient light,
motion). BIDMC is a transmissive oximeter on bed-ridden patients. ``BASE`` and ``PULSE_COUNTS`` are
stand-ins for what the real sensor will give.

    uv run python scripts/real_ppg_replay.py --record 01
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from synapse24.signal_quality.ecg import detect_r_peaks_neurokit  # noqa: E402
from tests.firmware_host import build, try_load  # noqa: E402

BAND = ROOT / "firmware" / "esp32_band"
PPG_FS = 50
BIDMC_FS = 125
BASE = 120_000.0  # IR counts at rest (same stand-in as tests/test_firmware_s5_rr.py)
PULSE_COUNTS = 4_000.0  # counts of the systolic pulse; real value depends on the LED current
T0_US = 10_000_000
WINDOW_S = 30
MIN_INTERVALS = 5  # fewer than this in a window: the band gives no estimate, and we say so


def load_bidmc(
    data_dir: Path, record: str
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """(PLETH, ECG lead II) of one record at 125 Hz, from the PhysioNet CSV."""
    cols = np.loadtxt(data_dir / f"bidmc_{record}_Signals.csv", delimiter=",", skiprows=1)
    return cols[:, 2], cols[:, 5]


def pleth_to_ir_counts(pleth: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    """50 Hz reflective IR counts: baseline BASE, a pulse of PULSE_COUNTS, falling on each beat."""
    x = resample_poly(pleth, PPG_FS, BIDMC_FS)
    lo, hi = np.percentile(x, [5, 95])
    unit = (x - lo) / max(hi - lo, 1e-9)  # 0..1, higher = systole in a transmissive oximeter
    return BASE - PULSE_COUNTS * unit


def load_rr(out_dir: Path) -> ctypes.CDLL | None:
    """Build synapse_rr.c for the host; None if the OS refuses to load it."""
    lib = try_load(
        build(
            out_dir,
            [
                ROOT / "tests" / "firmware_host" / "host_glue_rr.c",
                BAND / "signal_quality" / "synapse_rr.c",
            ],
            extra_includes=[str(BAND / "signal_quality")],
        )
    )
    if lib is None:
        return None
    lib.host_rr_size.restype = ctypes.c_uint32
    lib.host_rr_init.argtypes = [ctypes.c_void_p]
    lib.host_rr_push.argtypes = [ctypes.c_void_p, ctypes.c_float, ctypes.c_int64]
    lib.host_rr_push.restype = ctypes.c_int
    lib.host_rr_take.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint16), ctypes.c_uint8]
    lib.host_rr_take.restype = ctypes.c_uint32
    lib.host_rr_accepted.argtypes = lib.host_rr_rejected.argtypes = [ctypes.c_void_p]
    lib.host_rr_accepted.restype = lib.host_rr_rejected.restype = ctypes.c_uint32
    return lib


def firmware_intervals(
    lib: ctypes.CDLL, ir: npt.NDArray[np.float64]
) -> tuple[list[tuple[float, float]], dict[str, int]]:
    """Feed the detector one sample at a time. Returns [(time_s, interval_ms)] and its counters."""
    state = ctypes.create_string_buffer(int(lib.host_rr_size()))
    lib.host_rr_init(state)
    out = (ctypes.c_uint16 * 8)()
    found: list[tuple[float, float]] = []
    for i, value in enumerate(ir):
        lib.host_rr_push(state, float(value), T0_US + i * 1_000_000 // PPG_FS)
        total = lib.host_rr_take(state, out, 8)
        found += [(i / PPG_FS, out[k] * 1000.0 / 1024.0) for k in range(min(total, 8))]
    counters = {
        "accepted": int(lib.host_rr_accepted(state)),
        "rejected": int(lib.host_rr_rejected(state)),
    }
    return found, counters


def _rmssd(
    times_s: npt.NDArray[np.float64], rr_ms: npt.NDArray[np.float64], *, tolerance: float = 0.2
) -> float:
    """RMSSD over *adjacent* clean intervals only, the same rule for the band and for the ECG.

    An interval is clean if it is within ``tolerance`` of the window median; two intervals are
    adjacent if the second one starts where the first ended (so no beat lies between them).
    """
    if len(rr_ms) < 3:
        return float("nan")
    clean = np.abs(rr_ms - np.median(rr_ms)) <= tolerance * np.median(rr_ms)
    adjacent = np.abs(np.diff(times_s) * 1000.0 - rr_ms[1:]) < 60.0
    keep = clean[1:] & clean[:-1] & adjacent
    d = np.diff(rr_ms)[keep]
    return float(np.sqrt(np.mean(d**2))) if len(d) >= 3 else float("nan")


def compare_windows(
    band: list[tuple[float, float]], ecg_peaks_s: npt.NDArray[np.float64], seconds: float
) -> dict[str, Any]:
    """Per 30 s window: heart rate (median interval) and RMSSD, band vs ECG."""
    rows: list[dict[str, float]] = []
    silent = 0
    for w0 in np.arange(0.0, seconds - WINDOW_S + 1e-9, WINDOW_S):
        in_band = [(t, rr) for t, rr in band if w0 <= t < w0 + WINDOW_S]
        mine_t = np.array([t for t, _ in in_band])
        mine = np.array([rr for _, rr in in_band])
        in_w = ecg_peaks_s[(ecg_peaks_s >= w0) & (ecg_peaks_s < w0 + WINDOW_S)]
        truth = np.diff(in_w) * 1000.0
        truth_t = in_w[1:]
        if len(truth) < MIN_INTERVALS:
            continue  # no reference either: nothing to compare
        if len(mine) < MIN_INTERVALS:
            silent += 1
            continue
        rows.append(
            {
                "hr_error_bpm": 60000.0 / float(np.median(mine))
                - 60000.0 / float(np.median(truth)),
                "rmssd_band_ms": _rmssd(mine_t, mine),
                "rmssd_ecg_ms": _rmssd(truth_t, truth),
            }
        )
    hr_err = np.array([r["hr_error_bpm"] for r in rows])
    return {
        "windows_compared": len(rows),
        "windows_band_silent": silent,
        "hr_median_abs_error_bpm": round(float(np.median(np.abs(hr_err))), 2) if rows else None,
        "hr_within_5bpm": round(float(np.mean(np.abs(hr_err) <= 5.0)), 3) if rows else None,
        "rmssd_band_ms_median": round(float(np.nanmedian([r["rmssd_band_ms"] for r in rows])), 1)
        if rows
        else None,
        "rmssd_ecg_ms_median": round(float(np.nanmedian([r["rmssd_ecg_ms"] for r in rows])), 1)
        if rows
        else None,
    }


def replay_ppg(
    pleth: npt.NDArray[np.float64], ecg: npt.NDArray[np.float64], lib: ctypes.CDLL
) -> dict[str, Any]:
    ir = pleth_to_ir_counts(pleth)
    band, counters = firmware_intervals(lib, ir)
    peaks_s = detect_r_peaks_neurokit(ecg, BIDMC_FS).astype(np.float64) / BIDMC_FS
    result = compare_windows(band, peaks_s, len(ir) / PPG_FS)
    result.update(
        {
            "seconds": round(len(ir) / PPG_FS, 1),
            "band_intervals": len(band),
            "ecg_beats": int(len(peaks_s)),
            **counters,
        }
    )
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--record", default="01")
    ap.add_argument("--seconds", type=float, default=480.0)
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data" / "bidmc")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    pleth, ecg = load_bidmc(args.data_dir, args.record)
    n = int(args.seconds * BIDMC_FS)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        lib = load_rr(Path(tmp))
        if lib is None:
            print(
                "host policy blocked the freshly built library; rerun in the gate's Linux container"
            )
            return 3
        result = replay_ppg(pleth[:n], ecg[:n], lib)
    result["record"] = args.record
    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
