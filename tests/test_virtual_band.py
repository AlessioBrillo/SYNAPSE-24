"""The virtual band's core: the table, the replay program and the band's behaviour (no radio).

``scripts/virtual_band_core.py`` replays the phone sidecar fixtures as a band would. Everything
here runs without Bluetooth: a test plays a whole minute of band time in a few milliseconds by
feeding the session a fake clock.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "sdk" / "python"))

import virtual_band_core as vb  # noqa: E402
from synapse_device import consumer  # noqa: E402

T0 = 1_000  # the host clock when the phone connects (any value: band time is relative)
RAW_NAMES = set(vb.RAW_STREAM_NAMES.values())


def _chars() -> dict[str, str]:
    return {c.name: c.uuid for c in vb.build_table()}


def _names() -> dict[str, str]:
    return {c.uuid: c.name for c in vb.build_table()}


def _connected(scenario: str = "clean", *, subscribe: bool = True) -> tuple[vb.BandSession, list]:
    log: list[dict] = []
    session = vb.BandSession(scenario, log=log.append)
    session.connect(T0)
    if subscribe:
        for c in vb.build_table():
            if vb.NOTIFY in c.props:
                session.subscribe(T0, c.uuid, True)
    return session, log


def _play(session: vb.BandSession, until_ms: int = 61_000, step: int = 50) -> list[vb.Event]:
    delivered: list[vb.Event] = []
    for t in range(0, until_ms, step):
        events, _ = session.poll(T0 + t)
        delivered += events
    return delivered


def _by_name(events: list[vb.Event]) -> dict[str, list[bytes]]:
    names, out = _names(), {}
    for ev in events:
        out.setdefault(names[ev.char], []).append(ev.data)
    return out


# --- the table ------------------------------------------------------------------------------


def test_the_table_carries_the_contract_uuids() -> None:
    gatt = json.loads(vb.GATT_V2.read_text(encoding="utf-8"))
    chars = _chars()
    for name, uuid_s in gatt["characteristics"].items():
        assert chars[name] == uuid_s
    assert next(c.service for c in vb.build_table() if c.name == "feature") == gatt["service"]
    assert {"ecg", "ppg", "acc", "gyro", "mag", "power"} <= set(chars)


def test_the_stream_metadata_value_decodes_to_the_contract_rows() -> None:
    gatt = json.loads(vb.GATT_V2.read_text(encoding="utf-8"))
    value = next(c.value for c in vb.build_table() if c.name == "stream_metadata")
    rows = consumer.decode_stream_metadata(value)
    streams = gatt["stream_metadata"]["streams"]
    assert [(r.stream_id, r.unit, r.rate_hz, r.unit_scale) for r in rows] == [
        (s["stream_id"], s["unit"], s["rate_hz"], s["unit_scale"]) for s in streams
    ]


# --- the program ----------------------------------------------------------------------------


def test_the_clean_program_is_the_fixtures_in_time_order() -> None:
    program = vb.load_program("clean")
    by_name = _by_name(program)
    raw = vb.sidecars.unpack((vb.FIXTURES / "session_60s.synapse-raw.bin").read_bytes())
    l1 = vb.sidecars.unpack((vb.FIXTURES / "session_60s.synapse.bin").read_bytes())
    assert sum(len(by_name.get(n, [])) for n in RAW_NAMES) == len(raw)
    assert len(by_name["feature"]) + len(by_name["status"]) == len(l1)
    assert [e.t_ms for e in program] == sorted(e.t_ms for e in program)
    # the frames are the fixture's, byte for byte
    assert {f for _, f in raw} == {f for n in RAW_NAMES for f in by_name.get(n, [])}


def test_the_standard_heart_rate_follows_the_feature_frames() -> None:
    by_name = _by_name(vb.load_program("clean"))
    known = [
        v
        for f in by_name["feature"]
        if f[0] == consumer.FEATURE_VERSION
        and (v := consumer.decode_feature(f))["heart_rate_bpm"] is not None
    ]
    hr = by_name["heart_rate_measurement"]
    assert len(hr) == len(known)  # the unknown-version frame yields none
    assert hr[0][0] & 0x07 == 0x07  # 16-bit value, contact detected
    assert int.from_bytes(hr[0][1:3], "little") == known[0]["heart_rate_bpm"]


def test_an_unknown_scenario_is_refused() -> None:
    with pytest.raises(ValueError, match="scenario"):
        vb.BandSession("nope")


# --- the session ----------------------------------------------------------------------------


def test_nothing_is_sent_before_a_phone_is_connected_and_subscribed() -> None:
    session = vb.BandSession("clean")
    assert session.poll(T0) == ([], [])  # the band's clock starts at the first connection
    session.connect(T0)
    assert _play(session, 5_000) == []  # connected, subscribed to nothing


def test_a_clean_session_delivers_every_frame_of_the_program() -> None:
    session, _ = _connected()
    delivered = _play(session)
    assert delivered == vb.load_program("clean")


def test_only_the_subscribed_characteristics_are_notified() -> None:
    session, _ = _connected(subscribe=False)
    session.subscribe(T0, _chars()["feature"], True)
    assert set(_by_name(_play(session))) == {"feature"}


def test_stop_gates_the_raw_streams_and_start_brings_them_back() -> None:
    session, log = _connected()
    assert session.write_control(T0, consumer.encode_control("stop")) == 0
    stopped = _by_name(_play(session, 10_000))
    assert not RAW_NAMES & set(stopped)  # no raw while stopped
    assert {"feature", "status"} <= set(stopped)  # feature and status keep flowing...
    for frame in stopped["feature"]:
        assert not frame[1] & vb.TRANSMITTING  # ...and say the raw is paused
    for frame in stopped["status"]:
        assert not frame[3] & vb.TRANSMITTING
    session.write_control(T0 + 10_000, consumer.encode_control("start"))
    resumed = _by_name([e for t in range(10_000, 20_000, 50) for e in session.poll(T0 + t)[0]])
    assert RAW_NAMES & set(resumed)
    assert all(f[1] & vb.TRANSMITTING for f in resumed["feature"])
    assert [r["op"] for r in log if r["event"] == "control"] == ["stop", "start"]


def test_a_new_connection_always_starts_with_the_raw_streams_running() -> None:
    session, _ = _connected()
    session.write_control(T0, consumer.encode_control("stop"))
    session.disconnect(T0 + 100)
    session.connect(T0 + 200)
    assert session.raw_on


def test_a_refused_write_changes_nothing_and_returns_the_contract_error() -> None:
    session, log = _connected()
    errors = consumer.CONTROL_ERRORS
    assert session.write_control(T0, bytes([0x7F])) == errors["bad_opcode"]
    assert session.write_control(T0, bytes([1, 0])) == errors["bad_length"]
    assert session.write_control(T0, bytes([3, 6])) == errors["bad_argument"]
    assert session.raw_on
    assert session.feature_rate_hz == 1
    assert session.time_ms is None
    assert [r["error"] for r in log if r["event"] == "control"] == [1, 2, 3]


def test_set_time_is_remembered() -> None:
    session, _ = _connected()
    assert session.write_control(T0, consumer.encode_control("set_time", 1_798_848_000_123)) == 0
    assert session.time_ms == 1_798_848_000_123


# --- the scenarios --------------------------------------------------------------------------


def test_cp_error_refuses_only_the_first_set_time() -> None:
    session, log = _connected("cp_error")
    write = consumer.encode_control("set_time", 1_798_848_000_123)
    assert session.write_control(T0, write) == consumer.CONTROL_ERRORS["bad_argument"]
    assert session.time_ms is None
    assert session.write_control(T0 + 500, write) == 0  # the phone's retry goes through
    assert session.time_ms == 1_798_848_000_123
    controls = [r for r in log if r["event"] == "control"]
    assert [r["injected"] for r in controls] == [True, False]


def test_disconnect_drops_the_link_and_the_gap_is_lost_frames() -> None:
    session, log = _connected("disconnect")
    delivered, actions = [], []
    for t in range(0, 61_000, 50):
        events, acts = session.poll(T0 + t)
        delivered += events
        actions += [(t, a) for a in acts]
    assert [a for _, a in actions] == ["drop_link", "readvertise"]
    assert actions[0][0] == vb.DISCONNECT_AT_MS
    assert actions[1][0] == vb.DISCONNECT_AT_MS + vb.READVERTISE_AFTER_MS
    gap = range(vb.DISCONNECT_AT_MS, vb.DISCONNECT_AT_MS + vb.READVERTISE_AFTER_MS)
    assert not any(e.t_ms in gap for e in delivered)
    assert any(e.t_ms < vb.DISCONNECT_AT_MS for e in delivered)
    assert delivered[-1].t_ms > vb.DISCONNECT_AT_MS + vb.READVERTISE_AFTER_MS
    assert vb.load_program("disconnect") == vb.load_program("clean")  # only the delivery differs
    assert {"drop_link", "readvertise"} <= {r["event"] for r in log}


def test_restart_starts_every_raw_stream_over_at_zero() -> None:
    clean, restarted = vb.load_program("clean"), vb.load_program("restart")
    names = _names()
    assert [e.t_ms for e in clean] == [e.t_ms for e in restarted]
    first_after: dict[str, vb.Event] = {}
    for old, new in zip(clean, restarted, strict=True):
        if old.t_ms < vb.RESTART_AT_MS or names[new.char] not in RAW_NAMES:
            assert old == new, "nothing changes before the restart or outside the raw streams"
        elif old.data[0] == 1:  # a frame of the known version
            first_after.setdefault(new.char, new)
    assert first_after, "there are raw frames after the restart"
    for ev in first_after.values():
        assert int.from_bytes(ev.data[2:4], "little") == 0  # sequence
        assert int.from_bytes(ev.data[4:8], "little") == 0  # device time
