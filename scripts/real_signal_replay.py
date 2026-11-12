"""Recorded physiological signal through the band's own code (MVP-1.8).

The firmware does no ECG DSP: the AD8232 trace leaves the band at 500 Hz in frames built by
``firmware/common/ble/synapse_raw_frame.c``. The value in the frame is *already electrode-referred
microvolts*, computed by ``firmware/common/sensors/ecg_ad8232.cpp`` as
``(adc_mV - midrail_mV) / SYNAPSE_ECG_GAIN`` and sent as ``round(mV * 1000)`` in an s16. This script puts a
real MIT-BIH ECG through that path and asks the only question the bench would ask: *after the ADC and the
radio framing, are the beats still there?*

    MIT-BIH mV @360 Hz -> 500 Hz -> front end (gain, mid-rail, 12-bit ADC, clipping) -> firmware's
        gain and s16 uV -> C encoder (firmware) -> frames -> SDK decoder (phone side)
        -> R-peak detector -> Se/PPV against the cardiologist annotations

The same detector is also run on the untouched recording, so a loss caused by the chain shows up as a
difference between the two rows, not as a property of the detector.

Two gains, on purpose: ``true_gain`` is what the analog front end really does, ``configured_gain`` is what
the firmware divides by. They are equal only if ``SYNAPSE_ECG_GAIN`` was measured on the bench; until then
the amplitude on the wire is off by their ratio (and a nominal 1100x leaves only about +-1.3 mV before the
ADC rails, which clips a large QRS).

What this does NOT prove: the AD8232 itself, its real offset/noise, the lead-off behaviour, or the radio.
The ADC range (``ADC_FULL_SCALE_MV``) and mid-rail (``MIDRAIL_MV``) are nominal values.

    uv run python scripts/real_signal_replay.py --record 100 --seconds 60
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
from scipy.signal import lfilter, resample_poly

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "sdk" / "python"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from synapse_device.codec import (  # noqa: E402
    FLAG_TS_EPOCH_WRAPPED,
    RawFormat,
    RawStream,
    decode_frame,
)

from synapse24.ingestion.mitbih import load_mitbih_record  # noqa: E402
from synapse24.signal_quality.ecg import (  # noqa: E402
    detect_r_peaks_neurokit,
    r_peak_detection_quality,
)
from tests.firmware_host import build, try_load  # noqa: E402

ECG_FS = 500
ADC_MAX = 4095  # ESP32-S3 ADC1, 12 bit
# ponytail: nominal. ADC_ATTEN_DB_12 reads about 0..3.1 V; the real range and the curve-fitting calibration
# are per chip. The AD8232 output rests on about Vcc/2. Measure both on the bench (BRINGUP_SOP 4a).
ADC_FULL_SCALE_MV = 3100.0
MIDRAIL_MV = 1650.0
CALIBRATION_SAMPLES = (
    500  # the driver averages the first second to find the mid-rail, and emits none of it
)
MIDRAIL_TRACK_ALPHA = 1.0 / 5000.0  # then follows it with a ~10 s time constant (ecg_ad8232.cpp)
MIN_SECONDS = 5.0  # of ECG after the calibration second: less is not a recording to score
MAX_BEAT_LOSS = 0.03  # Se/PPV the chain may cost against the untouched recording (a beat or two)
# AD8232 breakout nominal, the same number as config/hardware.yaml ecg_config.gain and the Kconfig default.
NOMINAL_GAIN = 1100.0
FRAME_SAMPLES = 100  # 0.2 s of ECG per notification; the frame limit for s16 is 116
RAW_SRC = ROOT / "firmware" / "common" / "ble" / "synapse_raw_frame.c"


def ecg_to_wire_uv(
    mv: npt.NDArray[np.float64],
    fs_in: int,
    *,
    true_gain: float = NOMINAL_GAIN,
    configured_gain: float = NOMINAL_GAIN,
) -> tuple[npt.NDArray[np.int16], float]:
    """What the band puts in the ECG frames for ``mv`` at the electrodes: s16 microvolts.

    Mirrors ``ecg_ad8232.cpp``: front end -> 12-bit ADC (rails at 0 and full scale) -> the first second
    is only used to find the mid-rail and is NOT emitted -> from then on the mid-rail follows the signal
    slowly (a ~0.016 Hz high-pass) -> (adc - mid-rail) / *configured* gain -> x1000, rounded, clipped to
    s16. So the output starts ``CALIBRATION_SAMPLES`` samples after the input.
    Returns (microvolts, share of emitted samples that hit the ADC rails or the s16 limit).
    """
    ratio = Fraction(ECG_FS, fs_in)
    x = resample_poly(mv, ratio.numerator, ratio.denominator)
    adc_mv = MIDRAIL_MV + true_gain * x
    railed = (adc_mv < 0) | (adc_mv > ADC_FULL_SCALE_MV)
    lsb = ADC_FULL_SCALE_MV / ADC_MAX
    adc_mv = np.rint(np.clip(adc_mv, 0, ADC_FULL_SCALE_MV) / lsb) * lsb
    if len(adc_mv) < CALIBRATION_SAMPLES + MIN_SECONDS * ECG_FS:
        raise ValueError(
            f"{len(adc_mv) / ECG_FS:.1f} s of ECG: need the 1 s calibration plus {MIN_SECONDS:.0f} s"
        )
    midrail0 = float(np.mean(adc_mv[:CALIBRATION_SAMPLES]))
    adc_mv, railed = adc_mv[CALIBRATION_SAMPLES:], railed[CALIBRATION_SAMPLES:]
    # mid[n] = mid[n-1] + alpha * (adc[n] - mid[n-1]), updated before the sample is used, as in the driver
    midrail = lfilter(
        [MIDRAIL_TRACK_ALPHA],
        [1.0, -(1.0 - MIDRAIL_TRACK_ALPHA)],
        adc_mv,
        zi=[(1.0 - MIDRAIL_TRACK_ALPHA) * midrail0],
    )[0]
    uv = np.rint((adc_mv - midrail) / configured_gain * 1000.0)
    saturated = railed | (np.abs(uv) > 32767)
    return np.clip(uv, -32768, 32767).astype(np.int16), float(np.mean(saturated))


def load_codec(out_dir: Path) -> ctypes.CDLL | None:
    """Build the firmware raw-frame codec for the host; None if the OS refuses to load it."""
    lib = try_load(build(out_dir, [RAW_SRC]))
    if lib is None:
        return None
    lib.synapse_raw_frame_encode.restype = ctypes.c_int
    lib.synapse_raw_frame_encode.argtypes = [
        ctypes.c_uint8,
        ctypes.c_uint16,
        ctypes.c_uint32,
        ctypes.c_uint8,
        ctypes.c_uint16,
        ctypes.c_uint8,
        ctypes.c_void_p,
        ctypes.c_uint8,
        ctypes.c_char_p,
        ctypes.c_size_t,
    ]
    return lib


def firmware_frames(
    lib: ctypes.CDLL,
    samples: npt.NDArray[np.int16],
    *,
    per_frame: int = FRAME_SAMPLES,
) -> list[bytes]:
    """Frame s16 ``samples`` with the firmware's C encoder, as the band would notify them."""
    frames: list[bytes] = []
    for seq, start in enumerate(range(0, len(samples), per_frame)):
        chunk = np.ascontiguousarray(samples[start : start + per_frame], dtype="<i2")
        ts_us = start * 1_000_000 // ECG_FS
        wrapped = FLAG_TS_EPOCH_WRAPPED if ts_us >> 32 else 0
        out = ctypes.create_string_buffer(256)
        status = lib.synapse_raw_frame_encode(
            int(RawStream.ECG),
            seq & 0xFFFF,
            ts_us & 0xFFFFFFFF,
            wrapped,
            0,
            int(RawFormat.S16),
            chunk.ctypes.data,
            len(chunk),
            out,
            len(out),
        )
        if status < 0:
            raise RuntimeError(f"firmware encoder refused frame {seq}: status {status}")
        frames.append(out.raw[: 12 + 2 * len(chunk)])
    return frames


def phone_decode(frames: list[bytes]) -> npt.NDArray[np.int64]:
    """Decode with the SDK (the phone-side reader) and refuse a gap in the sequence."""
    parts: list[npt.NDArray[np.int64]] = []
    for expected, frame in enumerate(frames):
        header, values = decode_frame(frame)
        if header.sequence != expected & 0xFFFF or header.dropped:
            raise ValueError(
                f"frame {expected}: sequence {header.sequence}, dropped {header.dropped}"
            )
        parts.append(values.astype(np.int64))
    return np.concatenate(parts)


def _score(
    detected: npt.NDArray[np.int64], reference: npt.NDArray[np.int64], fs: int
) -> dict[str, float]:
    se, ppv = r_peak_detection_quality(detected, reference, fs)
    return {"sensitivity": round(se, 4), "ppv": round(ppv, 4), "detected": int(len(detected))}


def replay_ecg(
    mv: npt.NDArray[np.float64],
    reference_peaks: npt.NDArray[np.int64],
    fs_in: int,
    lib: ctypes.CDLL,
    *,
    true_gain: float = NOMINAL_GAIN,
    configured_gain: float = NOMINAL_GAIN,
) -> dict[str, Any]:
    """Run one recording through the chain and score beats before and after it."""
    wire, clipped = ecg_to_wire_uv(mv, fs_in, true_gain=true_gain, configured_gain=configured_gain)
    frames = firmware_frames(lib, wire)
    received = phone_decode(frames)
    # the band emits nothing during the mid-rail calibration second: its clock starts that much later
    ref_out = np.rint(reference_peaks * ECG_FS / fs_in).astype(np.int64) - CALIBRATION_SAMPLES
    ref_out = ref_out[(ref_out >= 0) & (ref_out < len(wire))]
    before = detect_r_peaks_neurokit(mv, fs_in)
    after = detect_r_peaks_neurokit(received.astype(np.float64), ECG_FS)
    return {
        "reference_beats": int(len(reference_peaks)),
        "frames": len(frames),
        "bit_exact": bool(np.array_equal(received, wire)),
        "saturated_fraction": round(clipped, 6),
        "true_gain": true_gain,
        "configured_gain": configured_gain,
        "untouched_recording": _score(before, reference_peaks, fs_in),
        "after_the_chain": _score(after, ref_out, ECG_FS),
    }


def beats_lost(result: dict[str, Any]) -> bool:
    """True if the chain cost more beat sensitivity or PPV than ``MAX_BEAT_LOSS``."""
    before, after = result["untouched_recording"], result["after_the_chain"]
    return (
        after["sensitivity"] < before["sensitivity"] - MAX_BEAT_LOSS
        or after["ppv"] < before["ppv"] - MAX_BEAT_LOSS
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--record", default="100")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data" / "mitbih")
    ap.add_argument("--true-gain", type=float, default=NOMINAL_GAIN)
    ap.add_argument("--configured-gain", type=float, default=NOMINAL_GAIN)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    mv, ref, meta = load_mitbih_record(args.record, args.data_dir)
    n = int(args.seconds * meta["fs"])
    ref = ref[ref < n]
    # Windows cannot delete a DLL that is still loaded: ignore the cleanup error.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        lib = load_codec(Path(tmp))
        if lib is None:
            print(
                "host policy blocked the freshly built codec; rerun in the gate's Linux container"
            )
            return 3
        try:
            result = replay_ecg(
                mv[:n],
                ref,
                meta["fs"],
                lib,
                true_gain=args.true_gain,
                configured_gain=args.configured_gain,
            )
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 2
    result["record"] = args.record
    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0 if result["bit_exact"] and not beats_lost(result) else 1


if __name__ == "__main__":
    sys.exit(main())
