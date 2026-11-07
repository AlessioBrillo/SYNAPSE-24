"""The phone-side sidecars (``contracts/fixtures/session_60s.*``) and their answer key.

Averyn's server and screens read these files, so the test reads them the way they will: with its
own ``struct`` unpacking, not the generator's helper, and checks the answer key against facts that
do not come from the oracle (what was removed, what was injected, how long the minute is).
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT / "scripts"))

import make_band_sidecars  # noqa: E402
from synapse_device import codec, consumer  # noqa: E402

FIXTURES = ROOT / "contracts" / "fixtures"
RAW = FIXTURES / "session_60s.synapse-raw.bin"
L1 = FIXTURES / "session_60s.synapse.bin"
KEY: dict[str, Any] = json.loads((FIXTURES / "session_60s.expected.json").read_text("utf-8"))


def _records(path: Path) -> list[tuple[int, bytes]]:
    """u16 length | i64 t_rx_ns | frame, little endian: read by hand, as a consumer would."""
    blob = path.read_bytes()
    out, pos = [], 0
    while pos < len(blob):
        length, t_ns = struct.unpack_from("<Hq", blob, pos)
        pos += 10
        out.append((t_ns, blob[pos : pos + length]))
        pos += length
    assert pos == len(blob), "the file ends inside a record"
    return out


def test_the_files_are_what_the_generator_writes() -> None:
    assert make_band_sidecars.main(["--check"]) == 0


def test_the_raw_sidecar_is_a_minute_of_five_streams_in_reception_order() -> None:
    records = _records(RAW)
    assert len(records) == KEY["raw"]["records"]
    times = [t for t, _ in records]
    assert times == sorted(times), "the phone appends in reception order"
    assert times[0] > 0, "the first frame is received after the recording starts"
    assert times[-1] < 61_000 * 1_000_000, "a 60 s recording"
    streams = {frame[1] for _, frame in records if frame[0] == 1}
    assert streams == {int(s) for s in (1, 2, 3, 4, 5)}  # ECG PPG ACC GYRO MAG


def test_every_injected_fault_is_in_the_answer_key() -> None:
    """Facts that were injected, not computed: 4 ECG frames removed, a restart, 9 dropped, ..."""
    streams = KEY["raw"]["streams"]
    assert streams["ecg"]["lost_frames"] == 4
    assert streams["ecg"]["restarts"] == 1
    assert streams["ppg"]["device_dropped"] == 9
    assert streams["acc"]["out_of_order"] == 1
    assert KEY["raw"]["invalid_frames"] == {"unknown_version": 1}
    for name in ("gyro", "mag"):
        assert streams[name]["lost_frames"] == 0
        assert streams[name]["restarts"] == 0


def test_the_sidecar_frames_decode_with_the_sdk_codec() -> None:
    decoded = invalid = 0
    for _t, frame in _records(RAW):
        try:
            codec.decode_frame(frame)
            decoded += 1
        except codec.RawCodecError:
            invalid += 1
    assert invalid == 1
    assert decoded == KEY["raw"]["records"] - 1


def test_the_l1_sidecar_has_features_and_status_told_apart_by_length() -> None:
    records = _records(L1)
    features = [f for _, f in records if len(f) == consumer.FEATURE_BYTES]
    statuses = [f for _, f in records if len(f) == consumer.STATUS_BYTES]
    assert len(features) + len(statuses) == len(records), "a record of neither length"
    assert len(features) == 60, "one feature frame per second for a minute"
    assert len(statuses) == 12, "a status record every 5 s"
    # two frames lost on the link, plus the frame of an unknown version: it was skipped, but it
    # used a sequence number, so a reader sees a hole there too (and counts it apart as unknown)
    assert KEY["l1"]["lost_frames"] == 2 + 1
    assert KEY["l1"]["unknown_version_frames"] == 1
    assert KEY["l1"]["feature_frames"] == 59
    assert KEY["l1"]["status_records"] == 12
    assert consumer.decode_status(statuses[-1])["uptime_s"] == 55
