"""The synthetic band sessions (``contracts/fixtures/band_scenarios_v1.json``) and their answer key.

The key is computed by a reference implementation in the generator, so a test that only compared it
with itself would prove nothing. Each scenario below is checked against facts that do not come from
the oracle: the frames that were removed, the restart that was injected, the signal that was drawn.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT / "scripts"))

import make_band_fixtures  # noqa: E402
from synapse_device import codec, consumer  # noqa: E402

FIXTURES = ROOT / "contracts" / "fixtures" / "band_scenarios_v1.json"
DOC: dict[str, Any] = json.loads(FIXTURES.read_text(encoding="utf-8"))
RAW = {s["name"]: s for s in DOC["raw"]}


def _decode(name: str) -> list[tuple[codec.RawHeader, np.ndarray]]:
    out = []
    for f in RAW[name]["frames"]:
        try:
            out.append(codec.decode_frame(bytes.fromhex(f["hex"])))
        except codec.RawCodecError:
            continue
    return out


def _stream(name: str, stream: codec.RawStream) -> list[tuple[codec.RawHeader, np.ndarray]]:
    return [(h, v) for h, v in _decode(name) if h.stream == stream]


def test_the_file_is_what_the_generator_writes() -> None:
    assert make_band_fixtures.main(["--check"]) == 0


def test_every_scenario_says_it_is_synthetic_and_the_file_stays_small() -> None:
    assert all(s["synthetic"] for s in [*DOC["raw"], DOC["l1"]])
    assert FIXTURES.stat().st_size < 500_000, "a fixture file a repository carries must stay small"
    assert {"clean_20s", "ecg_frames_lost", "band_restart", "timestamp_wrap"} <= set(RAW)


def test_the_answer_key_matches_a_second_decoding_of_every_scenario() -> None:
    for scenario in DOC["raw"]:
        frames = [(f["recv_ms"], bytes.fromhex(f["hex"])) for f in scenario["frames"]]
        assert make_band_fixtures.oracle(frames) == scenario["expected"], scenario["name"]


def test_reception_times_never_go_backwards() -> None:
    for scenario in DOC["raw"]:
        times = [f["recv_ms"] for f in scenario["frames"]]
        assert times == sorted(times), scenario["name"]


# --- independent facts -------------------------------------------------------------------------------------


def test_the_clean_session_has_no_loss_and_twenty_1mv_r_waves() -> None:
    expected = RAW["clean_20s"]["expected"]
    for name, summary in expected["streams"].items():
        assert summary["lost_frames"] == 0, name
        assert summary["restarts"] == summary["out_of_order"] == summary["device_dropped"] == 0, (
            name
        )
    ecg = np.concatenate([v for _h, v in _stream("clean_20s", codec.RawStream.ECG)])
    assert ecg.size == 156 * 64  # whole frames only
    # one 1 mV R wave per second: the wire unit of the ECG is microvolts
    above = np.flatnonzero(ecg > 800)
    beats = 1 + int(np.count_nonzero(np.diff(above) > 100))
    assert beats == 20, beats
    assert 950 <= int(ecg.max()) <= 1050
    # PPG is two channels of raw counts, ACC has gravity on Z in milli-g
    ppg = np.concatenate([v for _h, v in _stream("clean_20s", codec.RawStream.PPG)]).reshape(-1, 2)
    assert ppg[:, 1].mean() > ppg[:, 0].mean() > 90_000
    acc = np.concatenate([v for _h, v in _stream("clean_20s", codec.RawStream.ACC)]).reshape(-1, 3)
    assert 990 <= np.median(acc[:, 2]) <= 1010


def test_three_removed_frames_are_three_lost_frames() -> None:
    sequences = sorted(h.sequence for h, _v in _stream("ecg_frames_lost", codec.RawStream.ECG))
    missing = sorted(set(range(sequences[0], sequences[-1] + 1)) - set(sequences))
    assert missing == [4, 5, 6]
    assert RAW["ecg_frames_lost"]["expected"]["streams"]["ecg"]["lost_frames"] == 3
    # the other streams were untouched
    assert RAW["ecg_frames_lost"]["expected"]["streams"]["ppg"]["lost_frames"] == 0


def test_a_reboot_is_one_restart_and_not_a_flood_of_lost_frames() -> None:
    ecg = RAW["band_restart"]["expected"]["streams"]["ecg"]
    sequences = [h.sequence for h, _v in _stream("band_restart", codec.RawStream.ECG)]
    drops = [i for i in range(1, len(sequences)) if sequences[i] < sequences[i - 1]]
    assert len(drops) == 1
    assert sequences[drops[0] - 1] == 79
    assert sequences[drops[0]] == 0
    assert ecg["restarts"] == 1
    assert ecg["lost_frames"] == 0
    assert ecg["events"] == [{"sequence": 0, "event": "restart"}]


def test_the_32_bit_wrap_becomes_a_continuous_64_bit_timeline() -> None:
    expected = RAW["timestamp_wrap"]["expected"]["streams"]
    for name in ("ecg", "ppg"):
        timeline = expected[name]["timeline_us"]
        assert expected[name]["wraps"] == 1, name
        assert timeline == sorted(timeline), f"{name}: the wrap broke the order"
        assert max(np.diff(timeline)) < 1_000_000, f"{name}: a hole where the counter wrapped"
        assert max(timeline) > 1 << 32 > min(timeline)
    # without the epoch the low 32 bits go backwards exactly once per stream
    lows = [h.timestamp_us for h, _v in _stream("timestamp_wrap", codec.RawStream.ECG)]
    assert sum(1 for a, b in zip(lows, lows[1:], strict=False) if b < a) == 1


def test_a_device_loss_report_adds_up_and_saturates_at_255() -> None:
    headers = [h for h, _v in _stream("device_reported_loss", codec.RawStream.PPG)]
    assert sorted(h.dropped for h in headers if h.dropped) == [7, 255]
    assert RAW["device_reported_loss"]["expected"]["streams"]["ppg"]["device_dropped"] == 262
    assert RAW["device_reported_loss"]["expected"]["streams"]["ppg"]["lost_frames"] == 0


def test_six_broken_frames_are_six_refusals_and_none_is_interpreted() -> None:
    expected = RAW["invalid_frames"]["expected"]
    assert expected["invalid_frames"] == {
        "unknown_version": 1,
        "unknown_stream": 1,
        "unknown_format": 1,
        "truncated": 1,
        "too_short": 1,
        "zero_samples": 1,
    }
    good = len(RAW["invalid_frames"]["frames"]) - 6
    assert expected["streams"]["ecg"]["frames"] == good


def test_a_repeat_and_a_late_frame_are_not_new_samples() -> None:
    ecg = RAW["repeat_and_late_frame"]["expected"]["streams"]["ecg"]
    assert len(RAW["repeat_and_late_frame"]["frames"]) == 32
    assert ecg["frames"] == 30  # the two extra arrivals add nothing to the timeline
    assert ecg["out_of_order"] == 2
    assert ecg["lost_frames"] == 1  # the late frame was missing when its successor arrived
    assert ecg["restarts"] == 0


# --- the feature (L1) session -----------------------------------------------------------------------------


def test_the_l1_session_has_two_lost_frames_one_restart_and_one_foreign_version() -> None:
    l1 = DOC["l1"]
    assert l1["expected"] == {
        "lost_frames": 2,
        "restarts": 1,
        "out_of_order": 0,
        "unknown_version_frames": 1,
        "accepted_frames": 120,
    }
    sequences = [
        consumer.decode_feature(bytes.fromhex(f["hex"]))["sequence"]
        for f in l1["frames"]
        if f["values"] is not None
    ]
    jumps = [(a, b) for a, b in zip(sequences, sequences[1:], strict=False) if b != a + 1]
    assert jumps[0][1] - jumps[0][0] - 1 == 1  # 20 -> 22 and 22 -> 24: one frame each
    assert (90, 0) not in jumps  # the reboot goes back to 0 from wherever it was
    assert any(b == 0 for _a, b in jumps)


def test_the_warm_up_frames_carry_sentinels_that_decode_to_none() -> None:
    first = DOC["l1"]["frames"][0]
    decoded = consumer.decode_feature(bytes.fromhex(first["hex"]))
    assert decoded["heart_rate_bpm"] == 60
    for field in ("ppg_sqi", "motion_artifact", "motion_level", "rr_last", "stress_level"):
        assert decoded[field] is None, field
    later = consumer.decode_feature(bytes.fromhex(DOC["l1"]["frames"][20]["hex"]))
    assert later["ppg_sqi"] is not None


def test_the_foreign_version_frame_is_refused_by_the_codec() -> None:
    foreign = next(f for f in DOC["l1"]["frames"] if f["values"] is None)
    with pytest.raises(consumer.ConsumerCodecError, match="version"):
        consumer.decode_feature(bytes.fromhex(foreign["hex"]))
