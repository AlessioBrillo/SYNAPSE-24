"""MVP-1.8: a real recorded ECG through the band's own framing code.

``tests/fixtures/mitbih_ecg_30s.npz`` holds 30 s of MIT-BIH records 100 and 105 (PhysioNet, Open Data
Commons Attribution licence; cut by hand from the public records, no processing). The firmware has no ECG
DSP, and the frames carry electrode-referred microvolts, so the thing worth proving on a real trace is that
the path *front end -> firmware's gain and s16 uV -> C frame encoder -> SDK decoder* neither loses nor
reorders samples and leaves the beats findable, also when the ADC rails or SYNAPSE_ECG_GAIN is wrong.

This proves the chain, not the AD8232: gain, offset, noise and lead-off are the bench's job (the ADC range
and the mid-rail are nominal). A skip here is not a pass.
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
    assert result["bit_exact"], "the phone must read back exactly the values the band sent"
    # A nominal 1100x leaves +-1.3 mV before the ADC rails: record 105 clips 0.2 % of its samples.
    assert result["saturated_fraction"] < 0.01, result
    assert result["frames"] == -(-int(30 * rsr.ECG_FS) // rsr.FRAME_SAMPLES)


@pytest.mark.parametrize("record", RECORDS)
def test_the_beats_survive_the_chain(codec, recordings, record) -> None:
    mv, ref, fs = recordings[record]
    result = rsr.replay_ecg(mv, ref, fs, codec)
    before, after = result["untouched_recording"], result["after_the_chain"]
    assert before["sensitivity"] >= 0.95, "fixture must be detectable"
    assert before["ppv"] >= 0.95, "fixture must be detectable"
    # Resampling to 500 Hz and the 12-bit ADC may cost the odd beat, never a visible share of them.
    assert after["sensitivity"] >= before["sensitivity"] - 0.03, result
    assert after["ppv"] >= before["ppv"] - 0.03, result


def test_frame_timestamps_are_the_sample_clock(codec, recordings) -> None:
    mv, _, fs = recordings["100"]
    wire, _ = rsr.ecg_to_wire_uv(mv, fs)
    frames = rsr.firmware_frames(codec, wire)
    for k, frame in enumerate(frames):
        header, _ = decode_frame(frame)
        assert header.timestamp_us == (k * rsr.FRAME_SAMPLES * 1_000_000 // rsr.ECG_FS) & 0xFFFFFFFF


def test_a_hole_in_the_sequence_is_refused(codec, recordings) -> None:
    mv, _, fs = recordings["100"]
    wire, _ = rsr.ecg_to_wire_uv(mv, fs)
    frames = rsr.firmware_frames(codec, wire)
    del frames[3]
    with pytest.raises(ValueError, match="sequence"):
        rsr.phone_decode(frames)


def test_the_adc_clips_instead_of_wrapping() -> None:
    wire, saturated = rsr.ecg_to_wire_uv(np.array([0.0, 50.0, -50.0] * 400), 360)
    assert saturated > 0
    assert np.abs(wire).max() < 2000, (
        "bounded by the ADC rails (about +1.3 / -1.5 mV), never wrapped"
    )


@pytest.mark.parametrize("record", RECORDS)
def test_a_wrong_configured_gain_scales_the_amplitude_not_the_beats(
    codec, recordings, record
) -> None:
    """SYNAPSE_ECG_GAIN is a scale on the wire (the whole ECG), so it spoils every uV claim, not detection."""
    mv, ref, fs = recordings[record]
    nominal, _ = rsr.ecg_to_wire_uv(mv, fs)
    wrong, _ = rsr.ecg_to_wire_uv(mv, fs, configured_gain=600.0)
    assert np.std(wrong) / np.std(nominal) == pytest.approx(1100.0 / 600.0, rel=0.03)
    good = rsr.replay_ecg(mv, ref, fs, codec)["after_the_chain"]
    bad = rsr.replay_ecg(mv, ref, fs, codec, configured_gain=600.0)["after_the_chain"]
    assert bad["sensitivity"] == good["sensitivity"]
    assert bad["ppv"] == good["ppv"]
