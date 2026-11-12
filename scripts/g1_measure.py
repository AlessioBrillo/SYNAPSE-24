"""The two ECG bench numbers of BRINGUP_SOP §4a, computed from raw ADC counts.

1. noise floor: electrodes shorted, 60 s, standard deviation of the stream in counts;
2. gain: a known sine into the electrodes (e.g. 1 mVpp at 10 Hz) -> counts per mV *at the input*.

The second number is exactly the ``--counts-per-mv`` knob of ``real_signal_replay.py``. Converting it to
the SOP's ``gain_measured`` (V out / V in) needs the ADC's mV per count, which depends on the attenuation
the firmware sets and is not guessed here: pass ``--adc-mv-per-count`` when it is known.

No pass/fail thresholds: neither READINESS_GATE.md nor the SOP defines any, and a made-up one would read
as a requirement. The numbers are what G1 asks for.

    uv run python scripts/g1_measure.py noise capture.npy
    uv run python scripts/g1_measure.py gain  capture.npy --freq 10 --applied-mvpp 1 [--adc-mv-per-count X]

``capture.npy`` is a 1-D array of ECG ADC counts at ``--fs`` (default 500 Hz).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

MIN_SNR_DB = 10.0  # below this the "sine" is not in the data and a gain would be a fiction


def noise_floor_counts(counts: npt.ArrayLike) -> dict[str, float]:
    """Std of a shorted-input recording, in counts (SOP §4a.1)."""
    x = np.asarray(counts, dtype=np.float64)
    if x.size < 2:
        raise ValueError("need at least two samples")
    return {"std_counts": float(np.std(x, ddof=1)), "mean_counts": float(np.mean(x))}


def gain_from_sine(
    counts: npt.ArrayLike,
    fs: float,
    freq_hz: float,
    applied_mvpp: float,
    *,
    adc_mv_per_count: float | None = None,
) -> dict[str, Any]:
    """Counts per mV at the input, from a known sine (SOP §4a.2).

    Least-squares fit of ``a*sin + b*cos + c`` at the known frequency: it ignores the DC offset and
    the noise, and does not need a whole number of periods.
    """
    x = np.asarray(counts, dtype=np.float64)
    t = np.arange(x.size) / fs
    basis = np.column_stack(
        [np.sin(2 * np.pi * freq_hz * t), np.cos(2 * np.pi * freq_hz * t), np.ones_like(t)]
    )
    coef, *_ = np.linalg.lstsq(basis, x, rcond=None)
    amplitude = float(np.hypot(coef[0], coef[1]))
    residual = x - basis @ coef
    resid_rms = float(np.sqrt(np.mean(residual**2)))
    snr_db = float(20 * np.log10(amplitude / resid_rms)) if resid_rms > 0 else float("inf")
    if snr_db < MIN_SNR_DB:
        raise ValueError(
            f"no {freq_hz} Hz sine in the capture (SNR {snr_db:.1f} dB < {MIN_SNR_DB} dB): "
            "wrong frequency, input not connected, or the ADC is saturated"
        )
    counts_per_mv = 2 * amplitude / applied_mvpp
    out: dict[str, Any] = {
        "counts_per_mv": counts_per_mv,
        "amplitude_counts": amplitude,
        "snr_db": snr_db,
        "offset_counts": float(coef[2]),
    }
    if adc_mv_per_count is not None:
        out["gain_measured"] = counts_per_mv * adc_mv_per_count  # mV out per mV in
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("what", choices=["noise", "gain"])
    ap.add_argument("capture", type=Path)
    ap.add_argument("--fs", type=float, default=500.0)
    ap.add_argument("--freq", type=float, default=10.0)
    ap.add_argument("--applied-mvpp", type=float, default=1.0)
    ap.add_argument("--adc-mv-per-count", type=float)
    args = ap.parse_args()
    counts = np.load(args.capture)
    if args.what == "noise":
        result = noise_floor_counts(counts)
    else:
        result = gain_from_sine(
            counts,
            args.fs,
            args.freq,
            args.applied_mvpp,
            adc_mv_per_count=args.adc_mv_per_count,
        )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
