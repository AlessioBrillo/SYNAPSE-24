"""The two ECG bench numbers of BRINGUP_SOP §4a, computed from the ECG stream the host receives.

The band does not send ADC counts: it sends electrode-referred microvolts, computed as
``(adc_mV - midrail) / SYNAPSE_ECG_GAIN`` (``firmware/common/sensors/ecg_ad8232.cpp``). So a wrong
``SYNAPSE_ECG_GAIN`` scales the whole ECG, and the stream itself is what measures it:

1. noise floor: electrodes shorted (RA/LA/RL together), 60 s, standard deviation in µV;
2. gain: a known sine into the electrodes (e.g. 1 mVpp at 10 Hz). The stream shows ``r`` times the applied
   amplitude, with ``r = true_gain / configured_gain``, hence ``gain_measured = r * configured_gain`` and
   the Kconfig value ``CONFIG_SYNAPSE_ECG_GAIN = round(gain_measured * 100)``.

The tool reports numbers; it does not invent a verdict. BRINGUP_SOP §4a does define a
*provisional* noise bound (<= 30 uV true noise) and gain repeatability (+/-5 % over
three runs) — apply those in the report, not in this fit. The nominal gain and the
ECG stream name come from config/hardware.yaml, never from a second literal here.

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
import yaml

MIN_SNR_DB = 10.0  # below this the "sine" is not in the data and a gain would be a fiction
S16_LIMIT = 32767  # the wire value saturates here: a clipped sine measures a gain that is too low
FLAT_TOP_RUN = 4  # consecutive identical samples at an extreme: an ADC rail, not a sine
FLAT_TOP_FRACTION = 0.5  # the repeating level must sit at least this far from the signal centre
ROOT = Path(__file__).resolve().parents[1]


def _contract_ecg() -> dict[str, object]:
    manifest = yaml.safe_load((ROOT / "config" / "hardware.yaml").read_text(encoding="utf-8"))
    return next(
        pod["metadata"]["ecg_config"]
        for pod in manifest["pods"]
        if "ecg_config" in pod.get("metadata", {})
    )


_ECG = _contract_ecg()
NOMINAL_GAIN = float(_ECG["gain"])  # the Kconfig default, from the manifest
ECG_STREAM = str(_ECG["lsl_stream"])


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


def configured_gain_for(serial: str | None) -> float | None:
    """The gain the band is actually using, from its manufacturing record."""
    if not serial:
        return None
    path = ROOT / "data" / "calibration" / f"{serial}.json"
    if not path.exists():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    value = record.get("ecg_gain_x100")
    return int(value) / 100.0 if value else None


def noise_floor_uv(
    wire_uv: npt.ArrayLike, configured_gain: float | None = None
) -> dict[str, float]:
    """Std of a shorted-input recording, in µV at the configured gain (SOP §4a.1).

    ``std_uv`` is the number on the wire. The firmware already divides the
    ADC reading by the configured (calibrated) gain and the raw encoder scales
    mV to uV, so the wire number is electrode-referred uV when a gain was
    applied: ``true_noise_uv`` is the same figure, not a second correction.
    Scaling by nominal/actual here would double-correct a calibrated band.
    """
    x = np.asarray(wire_uv, dtype=np.float64)
    if x.size < 2:
        raise ValueError("need at least two samples")
    std = float(np.std(x, ddof=1))
    result = {"std_uv": std, "mean_uv": float(np.mean(x)), "true_noise_uv": std}
    if configured_gain is not None:
        result["configured_gain"] = float(configured_gain)
    return result


def _longest_run(mask: npt.NDArray[np.bool_]) -> int:
    """Longest stretch of consecutive True values in a 1-D boolean mask."""
    if mask.size == 0:
        return 0
    edges = np.flatnonzero(mask[1:] != mask[:-1]) + 1
    starts = np.r_[0, edges]
    ends = np.r_[edges, mask.size]
    true_starts = mask[starts]
    if not np.any(true_starts):
        return 0
    return int(np.max(ends[true_starts] - starts[true_starts]))


def _is_clipped(x: npt.NDArray[np.float64]) -> bool:
    """The s16 limit or a flat top at an extreme invalidates a gain fit.

    The validator now writes the ECG in microvolts (the contract's display
    unit), so a real capture never reaches +/-32767 uV: the ADC rails clip the
    front end well below that. A clipped sine therefore shows up as repeated
    identical values at the top or bottom of the trace, and fitting it would
    report a gain that is too low. Both shapes are refused.
    """
    if np.any(np.abs(x) >= S16_LIMIT):
        return True
    hi, lo = float(np.max(x)), float(np.min(x))
    span = hi - lo
    if span <= 0:
        return False  # a constant trace has no sine to fit
    centre = float(np.median(x))
    for level in (hi, lo):
        if abs(level - centre) < FLAT_TOP_FRACTION * span:
            continue
        if _longest_run(x == level) >= FLAT_TOP_RUN:
            return True
    return False


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
    if _is_clipped(x):
        raise ValueError(
            "the stream is clipped (s16 limit or a flat top at the extreme): "
            "reduce the applied amplitude (or the gain)"
        )
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
    ap.add_argument("--configured-gain", type=float, help="gain the band is currently using")
    ap.add_argument("--serial", help="band serial, to read its calibration record")
    args = ap.parse_args()
    wire, fs = load_capture(args.capture, args.fs)
    configured = args.configured_gain
    if configured is None:
        configured = configured_gain_for(args.serial)
    if configured is None:
        # One warning, one assumption, for both subcommands: a noise result
        # without `true_noise_uv` would silently invite the wrong comparison.
        configured = NOMINAL_GAIN
        origin = (
            f"no calibration record for {args.serial}" if args.serial else "no --configured-gain"
        )
        print(
            f"warning: {origin}: assuming the nominal {NOMINAL_GAIN:g}x",
            file=sys.stderr,
        )
    if args.what == "noise":
        result = noise_floor_uv(wire, configured_gain=configured)
    else:
        result = gain_from_sine(
            wire,
            fs,
            args.freq,
            args.applied_mvpp,
            configured_gain=configured,
        )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
