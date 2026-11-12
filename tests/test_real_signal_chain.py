"""MVP-1.8: a real recorded ECG through the band's own framing code.

``tests/fixtures/mitbih_ecg_30s.npz`` holds 30 s of MIT-BIH records 100 and 105 (PhysioNet, Open Data
Commons Attribution licence; cut by hand from the public records, no processing). The firmware has no ECG
DSP, so the thing worth proving on a real trace is that the path *sensor counts -> C frame encoder ->
SDK decoder* neither loses nor reorders samples and leaves the beats findable.

This proves the chain, not the AD8232: gain, offset, noise and lead-off are the bench's job
(``COUNTS_PER_MV`` is a stand-in). A skip here is not a pass.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "sdk" / "python"))

import real_signal_replay as rsr  # noqa: E402
from synapse_device.codec import decode_frame  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "mitbih_ecg_30s.npz"
RECORDS = ["100", "105"]


@pytest.fixture(scope="module")
def codec(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    lib = rsr.load_codec(tmp_path_factory.mktemp("realsignal"))
    if lib is None:
        pytest.skip("OS policy blocked loading the freshly built library; rerun in the Linux gate")
    return lib


@pytest.fixture(scope="module")
def recordings() -> dict[str, tuple[np.ndarray, np.ndarray, int]]:
    z = np.load(FIXTURE)
    fs = int(z["fs"])
    return {
        r: (z[f"mv_{r}"].astype(np.float64), z[f"ref_{r}"].astype(np.int64), fs) for r in RECORDS
    }


@pytest.mark.parametrize("record", RECORDS)
def test_the_framing_is_lossless_and_ordered(codec, recordings, record) -> None:
    mv, ref, fs = recordings[record]
    result = rsr.replay_ecg(mv, ref, fs, codec)
    assert result["bit_exact"], "the phone must read back exactly the counts the band sent"
    assert result["clipped_fraction"] == 0.0, "the nominal gain must keep a real ECG inside the ADC"
    assert result["frames"] == -(-int(30 * rsr.ECG_FS) // rsr.FRAME_SAMPLES)


@pytest.mark.parametrize("record", RECORDS)
def test_the_beats_survive_the_chain(codec, recordings, record) -> None:
    mv, ref, fs = recordings[record]
    result = rsr.replay_ecg(mv, ref, fs, codec)
    before, after = result["untouched_recording"], result["after_the_chain"]
    assert before["sensitivity"] >= 0.95, "fixture must be detectable"
    assert before["ppv"] >= 0.95, "fixture must be detectable"
    # Resampling to 500 Hz and 12-bit counts may cost the odd beat, never a visible share of them.
    assert after["sensitivity"] >= before["sensitivity"] - 0.03, result
    assert after["ppv"] >= before["ppv"] - 0.03, result


def test_frame_timestamps_are_the_sample_clock(codec, recordings) -> None:
    mv, _, fs = recordings["100"]
    counts, _ = rsr.ecg_to_adc_counts(mv, fs)
    frames = rsr.firmware_frames(codec, counts)
    for k, frame in enumerate(frames):
        header, _ = decode_frame(frame)
        assert header.timestamp_us == (k * rsr.FRAME_SAMPLES * 1_000_000 // rsr.ECG_FS) & 0xFFFFFFFF


def test_a_hole_in_the_sequence_is_refused(codec, recordings) -> None:
    mv, _, fs = recordings["100"]
    counts, _ = rsr.ecg_to_adc_counts(mv, fs)
    frames = rsr.firmware_frames(codec, counts)
    del frames[3]
    with pytest.raises(ValueError, match="sequence"):
        rsr.phone_decode(frames)


def test_the_adc_clips_instead_of_wrapping() -> None:
    counts, clipped = rsr.ecg_to_adc_counts(np.array([0.0, 50.0, -50.0] * 400), 360)
    assert clipped > 0
    assert counts.min() == 0
    assert counts.max() == rsr.ADC_MAX
