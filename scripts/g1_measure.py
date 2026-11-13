"""The two ECG bench numbers of BRINGUP_SOP §4a, computed from the ECG stream the host receives.

The band does not send ADC counts: it sends electrode-referred microvolts, computed as
``(adc_mV - midrail) / SYNAPSE_ECG_GAIN`` (``firmware/common/sensors/ecg_ad8232.cpp``). So a wrong
``SYNAPSE_ECG_GAIN`` scales the whole ECG, and the stream itself is what measures it:

1. noise floor: electrodes shorted (RA/LA/RL together), 60 s, standard deviation in µV;
2. gain: a known sine into the electrodes (e.g. 1 mVpp at 10 Hz). The stream shows ``r`` times the applied
   amplitude, with ``r = true_gain / configured_gain``, hence ``gain_measured = r * configured_gain`` and
   the Kconfig value ``CONFIG_SYNAPSE_ECG_GAIN = round(gain_measured * 100)``.

No pass/fail thresholds: neither READINESS_GATE.md nor the SOP defines any, and a made-up one would read
as a requirement. The numbers are what G1 asks for.

    uv run python scripts/g1_measure.py noise capture.npy
    uv run python scripts/g1_measure.py gain  capture.npy --freq 10 --applied-mvpp 1 [--configured-gain 1100]

``capture`` is either a ``.npy`` (1-D array of the ECG stream values, µV, at ``--fs``, default 500 Hz) or
the ``.xdf`` the live validator / LabRecorder wrote: the ``SYNAPSE_ECG_T0`` stream, first channel, at its
nominal rate.
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
S16_LIMIT = 32767  # the wire value saturates here: a clipped sine measures a gain that is too low
NOMINAL_GAIN = 1100.0  # config/hardware.yaml ecg_config.gain, the Kconfig default
ECG_STREAM = "SYNAPSE_ECG_T0"  # config/hardware.yaml ecg_config.lsl_stream


def load_capture(path: Path, fs: float) -> tuple[npt.NDArray[np.float64], float]:
    """(ECG values in µV, sampling rate) from a ``.npy`` or an ``.xdf``.

    Refuses what the fits below cannot handle: more than one channel, or an XDF with dropped samples
    (they assume uniform sampling, and a hole would give a wrong frequency or amplitude).
    """
    if path.suffix.lower() != ".xdf":
        x = np.load(path).astype(np.float64)
        if x.ndim == 2 and x.shape[1] == 1:
            x = x[:, 0]
        if x.ndim != 1:
            raise ValueError(f"{path}: expected one channel of ECG values, got shape {x.shape}")
        return x, fs
    from synapse24.export import load_xdf_streams  # noqa: PLC0415 - needs pyxdf, only for XDF

    for stream in load_xdf_streams(path):
        if stream.name != ECG_STREAM:
            continue
        rate = float(stream.nominal_srate or stream.sample_rate_hz or fs)
        dt = np.diff(stream.timestamps)
        if dt.size and dt.max() > 2.5 * np.median(dt):
            raise ValueError(
                f"{ECG_STREAM} has gaps (longest {dt.max() * 1000:.0f} ms, typical "
                f"{np.median(dt) * 1000:.1f} ms): the capture is not usable for a gain or a noise figure"
            )
        return stream.data[:, 0].astype(np.float64), rate
    raise ValueError(f"no {ECG_STREAM} stream in {path}")


def noise_floor_uv(wire_uv: npt.ArrayLike) -> dict[str, float]:
    """Std of a shorted-input recording, in µV at the configured gain (SOP §4a.1)."""
    x = np.asarray(wire_uv, dtype=np.float64)
    if x.size < 2:
        raise ValueError("need at least two samples")
    return {"std_uv": float(np.std(x, ddof=1)), "mean_uv": float(np.mean(x))}


def gain_from_sine(
    wire_uv: npt.ArrayLike,
    fs: float,
    freq_hz: float,
    applied_mvpp: float,
    *,
    configured_gain: float = NOMINAL_GAIN,
) -> dict[str, Any]:
    """Real front-end gain from a known sine (SOP §4a.2).

    Least-squares fit of ``a*sin + b*cos + c`` at the known frequency: it ignores the DC offset and
    the noise, and does not need a whole number of periods.
    """
    x = np.asarray(wire_uv, dtype=np.float64)
    if np.any(np.abs(x) >= S16_LIMIT):
        raise ValueError("the stream hit the s16 limit: reduce the applied amplitude (or the gain)")
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
    ratio = 2 * amplitude / (applied_mvpp * 1000.0)  # stream pp (µV) / applied pp (µV)
    gain_measured = ratio * configured_gain
    return {
        "gain_measured": gain_measured,
        "sdkconfig_ecg_gain": round(gain_measured * 100),
        "stream_over_applied": ratio,
        "amplitude_uv": amplitude,
        "snr_db": snr_db,
        "offset_uv": float(coef[2]),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("what", choices=["noise", "gain"])
    ap.add_argument("capture", type=Path)
    ap.add_argument("--fs", type=float, default=500.0)
    ap.add_argument("--freq", type=float, default=10.0)
    ap.add_argument("--applied-mvpp", type=float, default=1.0)
    ap.add_argument("--configured-gain", type=float, default=NOMINAL_GAIN)
    args = ap.parse_args()
    wire, fs = load_capture(args.capture, args.fs)
    if args.what == "noise":
        result = noise_floor_uv(wire)
    else:
        result = gain_from_sine(
            wire,
            fs,
            args.freq,
            args.applied_mvpp,
            configured_gain=args.configured_gain,
        )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
