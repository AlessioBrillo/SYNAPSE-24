"""Recorded physiological signal through the band's own code (MVP-1.8).

The firmware does no ECG DSP: the AD8232 trace leaves the band raw (500 Hz, 12-bit ADC counts as
s16) in frames built by ``firmware/common/ble/synapse_raw_frame.c``. This script puts a real MIT-BIH
ECG through exactly that path and asks the only question the bench would ask: *after the ADC and the
radio framing, are the beats still there?*

    MIT-BIH mV @360 Hz -> 500 Hz -> 12-bit ADC counts -> C encoder (firmware) -> frames
        -> SDK decoder (phone side) -> R-peak detector -> Se/PPV against the cardiologist annotations

The same detector is also run on the untouched recording, so a loss caused by the chain shows up as a
difference between the two rows, not as a property of the detector.

What this does NOT prove: the AD8232 analog front end, its real gain/offset/noise, the lead-off
behaviour, or the radio. ``COUNTS_PER_MV`` is a stand-in until ``ecg_config.gain_measured`` exists.

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
from scipy.signal import resample_poly

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
ADC_MIDSCALE = 2048
FRAME_SAMPLES = 100  # 0.2 s of ECG per notification; the frame limit for s16 is 116
# ponytail: nominal stand-in (~breakout gain 100 into a 3.1 V range). Replace with the bench value in
# config/hardware.yaml ecg_config.gain_measured once the kit exists; the knob is the point, not 132.
COUNTS_PER_MV = 132.0
RAW_SRC = ROOT / "firmware" / "common" / "ble" / "synapse_raw_frame.c"


def ecg_to_adc_counts(
    mv: npt.NDArray[np.float64],
    fs_in: int,
    *,
    counts_per_mv: float = COUNTS_PER_MV,
) -> tuple[npt.NDArray[np.int16], float]:
    """Resample to 500 Hz and quantize like a 12-bit ADC. Returns (counts, clipped fraction)."""
    ratio = Fraction(ECG_FS, fs_in)
    resampled = resample_poly(mv, ratio.numerator, ratio.denominator)
    raw = np.rint(resampled * counts_per_mv + ADC_MIDSCALE)
    clipped = float(np.mean((raw < 0) | (raw > ADC_MAX)))
    return np.clip(raw, 0, ADC_MAX).astype(np.int16), clipped


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
    counts: npt.NDArray[np.int16],
    *,
    per_frame: int = FRAME_SAMPLES,
) -> list[bytes]:
    """Frame ``counts`` with the firmware's C encoder, as the band would notify them."""
    frames: list[bytes] = []
    for seq, start in enumerate(range(0, len(counts), per_frame)):
        chunk = np.ascontiguousarray(counts[start : start + per_frame], dtype="<i2")
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
    counts_per_mv: float = COUNTS_PER_MV,
) -> dict[str, Any]:
    """Run one recording through the chain and score beats before and after it."""
    counts, clipped = ecg_to_adc_counts(mv, fs_in, counts_per_mv=counts_per_mv)
    frames = firmware_frames(lib, counts)
    received = phone_decode(frames)
    ref_out = np.rint(reference_peaks * ECG_FS / fs_in).astype(np.int64)
    before = detect_r_peaks_neurokit(mv, fs_in)
    after = detect_r_peaks_neurokit(received.astype(np.float64), ECG_FS)
    return {
        "reference_beats": int(len(reference_peaks)),
        "frames": len(frames),
        "bit_exact": bool(np.array_equal(received, counts)),
        "clipped_fraction": round(clipped, 6),
        "counts_per_mv": counts_per_mv,
        "untouched_recording": _score(before, reference_peaks, fs_in),
        "after_the_chain": _score(after, ref_out, ECG_FS),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--record", default="100")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data" / "mitbih")
    ap.add_argument("--counts-per-mv", type=float, default=COUNTS_PER_MV)
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
        result = replay_ecg(mv[:n], ref, meta["fs"], lib, counts_per_mv=args.counts_per_mv)
    result["record"] = args.record
    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0 if result["bit_exact"] else 1


if __name__ == "__main__":
    sys.exit(main())
