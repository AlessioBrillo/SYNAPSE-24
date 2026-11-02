"""The live feed behind ``/ws/live`` (MVP-1.3 PR-E2b).

Covers decimation, losses with their reason, the synthetic demo source, the transport source
and the viewer page.

Nothing here needs a Bluetooth adapter: the synthetic source and the SDK's synthetic transport
speak the real research-service frame format through the real encoder and decoder.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import numpy as np
import pytest
from synapse_device.codec import RawFormat, RawStream, encode_frame

from backend import live
from backend import main as main_module
from backend.main import app

try:
    from fastapi.testclient import TestClient  # type: ignore
except ImportError:  # pragma: no cover
    TestClient = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]


def _frame(stream: RawStream, seq: int, values: list[int], dropped: int = 0) -> bytes:
    fmt = RawFormat.S32 if stream == RawStream.PPG else RawFormat.S16
    return encode_frame(stream, seq, seq * 1000, 0, fmt, values, dropped)


# --- decimation -------------------------------------------------------------------------------------------


def test_the_envelope_keeps_the_peak_a_plain_decimation_would_drop() -> None:
    signal = np.zeros((200, 1))
    signal[101, 0] = 1000.0  # one R wave in 200 samples
    lows, highs = live.minmax_envelope(signal, 10)
    assert len(lows) == len(highs) == 10
    assert max(h[0] for h in highs) == 1000.0
    assert min(low[0] for low in lows) == 0.0


def test_the_envelope_never_has_more_columns_than_samples_and_handles_empty() -> None:
    assert live.minmax_envelope(np.zeros((0, 2)), 8) == ([], [])
    lows, _ = live.minmax_envelope(np.arange(6.0).reshape(3, 2), 32)
    assert len(lows) == 3


def test_the_envelope_is_per_channel() -> None:
    samples = np.array([[1.0, 10.0], [3.0, 30.0], [2.0, 20.0], [4.0, 40.0]])
    lows, highs = live.minmax_envelope(samples, 2)
    assert lows == [[1.0, 10.0], [2.0, 20.0]]
    assert highs == [[3.0, 30.0], [4.0, 40.0]]


# --- losses, with their reason ------------------------------------------------------------------------------


def test_a_sequence_gap_is_reported_with_the_number_of_lost_frames() -> None:
    tracker = live.LossTracker()
    assert tracker.observe(1, 10, 0) == []
    assert tracker.observe(1, 11, 0) == []
    assert tracker.observe(1, 14, 0) == [{"reason": "sequence_gap", "lost_frames": 2}]


def test_the_sequence_wraps_at_65535_without_a_false_loss() -> None:
    tracker = live.LossTracker()
    tracker.observe(1, 0xFFFF, 0)
    assert tracker.observe(1, 0, 0) == []


def test_a_repeated_or_reordered_frame_is_not_a_loss() -> None:
    tracker = live.LossTracker()
    tracker.observe(1, 20, 0)
    tracker.observe(1, 21, 0)
    assert tracker.observe(1, 20, 0) == []


def test_a_device_reported_drop_is_a_separate_reason() -> None:
    tracker = live.LossTracker()
    tracker.observe(1, 0, 0)
    assert tracker.observe(1, 1, 7) == [{"reason": "device_dropped", "lost_samples": 7}]


def test_streams_are_tracked_independently() -> None:
    tracker = live.LossTracker()
    tracker.observe(1, 5, 0)
    tracker.observe(2, 100, 0)
    assert tracker.observe(1, 6, 0) == []
    assert tracker.observe(2, 101, 0) == []


# --- decoder ----------------------------------------------------------------------------------------------------


def test_the_decoder_turns_a_frame_into_a_plot_message_with_the_contract_metadata() -> None:
    decoder = live.FrameDecoder()
    messages = decoder.decode(_frame(RawStream.ACC, 0, [1, 2, 3, 4, 5, 6, 7, 8, 9]))
    assert [m["type"] for m in messages] == ["frame"]
    message = messages[0]
    assert message["stream"] == "acc"
    assert message["n"] == 3  # nine values / three channels
    assert len(message["min"][0]) == 3


def test_the_decoder_puts_the_loss_before_the_frame_that_revealed_it() -> None:
    decoder = live.FrameDecoder()
    decoder.decode(_frame(RawStream.ECG, 0, [1, 2]))
    messages = decoder.decode(_frame(RawStream.ECG, 3, [1, 2]))
    assert [m["type"] for m in messages] == ["loss", "frame"]
    assert messages[0]["lost_frames"] == 2
    assert messages[0]["stream"] == "ecg"


def test_a_stream_the_contract_does_not_describe_is_ignored() -> None:
    decoder = live.FrameDecoder()
    assert decoder.decode(_frame(RawStream.TEMP, 0, [1])) == []


def test_a_corrupt_frame_is_reported_and_never_plotted() -> None:
    hub = live.LiveHub(None)

    async def go() -> list[dict]:
        queue = await hub.attach()
        hub._on_frame(b"\x00\x01")  # far too short to be a frame
        out = []
        while not queue.empty():
            out.append(queue.get_nowait())
        await hub.detach(queue)
        return out

    messages = asyncio.run(go())
    assert [m["type"] for m in messages] == ["error"]
    assert "undecodable" in messages[0]["error"]


def test_the_metadata_comes_from_the_contract_not_from_the_viewer() -> None:
    meta = live.stream_metadata()
    doc = json.loads((ROOT / "contracts" / "gatt_v2.json").read_text(encoding="utf-8"))
    assert {m["name"] for m in meta.values()} == {
        s["name"] for s in doc["stream_metadata"]["streams"]
    }
    assert meta[int(RawStream.ECG)]["unit"] == "uV"


# --- source selection ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("spec", ["", "  ", "off", "OFF"])
def test_no_source_is_the_default(spec: str) -> None:
    assert live.make_source(spec) is None


def test_synthetic_is_a_declared_demo() -> None:
    source = live.make_source("synthetic")
    assert source is not None
    assert source.demo is True


def test_a_ble_source_is_not_a_demo() -> None:
    source = live.make_source("ble:AA:BB:CC:DD:EE:FF")
    assert source is not None
    assert source.demo is False
    assert "AA:BB:CC:DD:EE:FF" in source.name


@pytest.mark.parametrize("spec", ["bluetooth", "ble:", "synthetic2"])
def test_a_bad_spec_is_refused(spec: str) -> None:
    with pytest.raises(ValueError, match="SYNAPSE_LIVE_SOURCE"):
        live.make_source(spec)


# --- hub ------------------------------------------------------------------------------------------------------------


async def _collect(hub: live.LiveHub, count: int, timeout_s: float = 5.0) -> list[dict]:
    queue = await hub.attach()
    out: list[dict] = []
    try:
        while len(out) < count:
            out.append(await asyncio.wait_for(queue.get(), timeout_s))
    finally:
        await hub.detach(queue)
    return out


def test_the_synthetic_source_produces_plots_for_every_described_stream() -> None:
    hub = live.LiveHub(live.SyntheticSource(tick_s=0.01))
    # 5 streams per tick: 250 messages reach t = 0.5 s, past the first QRS (0.25 s at 72 bpm)
    messages = asyncio.run(_collect(hub, 250))
    streams = {m["stream"] for m in messages if m["type"] == "frame"}
    assert streams == {"ecg", "ppg", "acc", "gyro", "mag"}
    ecg = [m for m in messages if m["type"] == "frame" and m["stream"] == "ecg"]
    # a real-looking ECG: the QRS is far above the baseline in the envelope
    assert max(h[0] for m in ecg for h in m["max"]) > 500


def test_the_synthetic_source_can_inject_a_loss_and_the_hub_reports_it() -> None:
    hub = live.LiveHub(live.SyntheticSource(tick_s=0.005, gap_every=3))
    messages = asyncio.run(_collect(hub, 60))
    losses = [m for m in messages if m["type"] == "loss"]
    assert losses, "no loss reported although a frame was skipped"
    assert all(m["stream"] == "ecg" and m["reason"] == "sequence_gap" for m in losses)


def test_the_source_stops_when_the_last_client_leaves() -> None:
    async def go() -> tuple[bool, bool]:
        hub = live.LiveHub(live.SyntheticSource(tick_s=0.005))
        first = await hub.attach()
        running_with_client = hub._task is not None and not hub._task.done()
        await hub.detach(first)
        return running_with_client, hub._task is None

    assert asyncio.run(go()) == (True, True)


def test_a_slow_client_loses_its_oldest_messages_not_the_feed() -> None:
    async def go() -> tuple[int, int]:
        hub = live.LiveHub(None)
        slow = await hub.attach()
        fast = await hub.attach()
        for i in range(300):
            hub._publish({"type": "frame", "i": i})
            fast.get_nowait()  # the fast client keeps up
        return slow.qsize(), hub.overflowed

    size, overflowed = asyncio.run(go())
    assert size == 256
    assert overflowed == 300 - 256


def test_a_failing_source_is_reported_to_the_clients() -> None:
    class Broken:
        name = "broken"
        demo = False

        async def run(self, on_frame):  # noqa: ANN001, ANN202
            raise RuntimeError("adapter unplugged")

    hub = live.LiveHub(Broken())
    messages = asyncio.run(_collect(hub, 1))
    assert messages[0]["type"] == "error"
    assert "adapter unplugged" in messages[0]["error"]


def test_the_transport_source_decodes_what_the_sdk_transport_delivers() -> None:
    from synapse_device.transport import CHAR_ECG, SyntheticTransport

    transport = SyntheticTransport()

    async def go() -> list[dict]:
        hub = live.LiveHub(live.TransportSource("SYNTH", transport=transport))
        queue = await hub.attach()
        try:
            for _ in range(100):  # wait until the source has subscribed
                if CHAR_ECG in transport._subscribed:
                    break
                await asyncio.sleep(0.01)
            await transport.start([CHAR_ECG], rate_hz=200, duration_s=0.2)
            out = [await asyncio.wait_for(queue.get(), 5.0) for _ in range(3)]
            await transport.stop()
            return out
        finally:
            await hub.detach(queue)

    messages = asyncio.run(go())
    assert {m["stream"] for m in messages} == {"ecg"}
    assert all(m["type"] == "frame" for m in messages)
    assert transport.connected is False  # the source disconnects on the way out


# --- the socket and the page ------------------------------------------------------------------------------------------


pytestmark_fastapi = pytest.mark.skipif(TestClient is None, reason="fastapi is not installed")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("SYNAPSE_SESSIONS_DIR", str(tmp_path))
    monkeypatch.delenv("SYNAPSE_API_TOKEN", raising=False)
    main_module._reset_services()
    yield TestClient(app)
    main_module._reset_services()


@pytestmark_fastapi
def test_without_a_source_the_socket_says_off_and_still_reports_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SYNAPSE_LIVE_SOURCE", raising=False)
    with client.websocket_connect("/ws/live") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["source"] == "off"
        assert hello["demo"] is False
        assert ws.receive_json()["type"] == "status_update"


@pytestmark_fastapi
def test_with_the_synthetic_source_the_socket_streams_plots_and_says_it_is_a_demo(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNAPSE_LIVE_SOURCE", "synthetic")
    with client.websocket_connect("/ws/live") as ws:
        hello = ws.receive_json()
        assert hello["source"] == "synthetic"
        assert hello["demo"] is True
        assert {s["name"] for s in hello["streams"]} == {"ecg", "ppg", "acc", "gyro", "mag"}
        kinds = {ws.receive_json()["type"] for _ in range(12)}
    assert "frame" in kinds


@pytestmark_fastapi
def test_an_invalid_source_spec_means_no_source_not_a_crash(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNAPSE_LIVE_SOURCE", "bluetooth")
    with client.websocket_connect("/ws/live") as ws:
        assert ws.receive_json()["source"] == "off"


@pytestmark_fastapi
def test_the_viewer_page_is_served_and_authenticates_through_the_subprotocol(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNAPSE_API_TOKEN", "s3cr3t")  # the page itself carries no data
    response = client.get("/live")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    html = response.text
    assert '["bearer", token]' in html  # the token never goes in the URL
    assert "/ws/live" in html
    assert "innerHTML" not in html  # every value reaches the DOM as text
    assert "http://" not in html.replace(
        "http://www.w3.org", ""
    )  # no external resource: works offline
    assert "https://" not in html


# --- review follow-ups (PR #61) ---------------------------------------------------------------------------------------


def test_a_client_attaching_while_the_last_one_detaches_still_gets_a_source() -> None:
    async def go() -> tuple[bool, bool]:
        hub = live.LiveHub(live.SyntheticSource(tick_s=0.005))
        first = await hub.attach()
        # the last client leaves at the same moment a new one arrives (a page reload)
        _, second = await asyncio.gather(hub.detach(first), hub.attach())
        running = hub._task is not None and not hub._task.done()
        message = await asyncio.wait_for(second.get(), 5.0)
        await hub.detach(second)
        return running, message["type"] in {"frame", "loss"}

    assert asyncio.run(go()) == (True, True)


def test_a_source_that_died_is_restarted_by_the_next_client_and_the_error_is_not_lost() -> None:
    class Flaky:
        name = "flaky"
        demo = False
        starts = 0

        async def run(self, on_frame):  # noqa: ANN001, ANN202
            Flaky.starts += 1
            if Flaky.starts == 1:
                raise RuntimeError("adapter unplugged")
            await asyncio.sleep(3600)

    async def go() -> tuple[dict, dict, int]:
        hub = live.LiveHub(Flaky())
        first = await hub.attach()
        await asyncio.wait_for(first.get(), 5.0)  # the error reaches the first client
        # a client that arrives after the source died is told why there is no feed...
        late_hello = hub.hello()
        late = await hub.attach()  # ...and the attach restarts the source
        await asyncio.sleep(0.05)  # let the new task take its first step
        restarted_hello = hub.hello()
        await hub.detach(first)
        await hub.detach(late)
        return late_hello, restarted_hello, Flaky.starts

    late_hello, restarted_hello, starts = asyncio.run(go())
    assert "adapter unplugged" in late_hello["error"]
    assert "error" not in restarted_hello  # cleared by the restart
    assert starts == 2


def test_a_restarted_source_does_not_report_a_false_loss() -> None:
    """The sequence restarts at 0 with the source: the tracker must forget the old one."""

    class Counting:
        name = "counting"
        demo = False

        async def run(self, on_frame):  # noqa: ANN001, ANN202
            for seq in range(3):
                on_frame(_frame(RawStream.ECG, seq, [1, 2]))
            await asyncio.sleep(3600)

    async def go() -> list[str]:
        hub = live.LiveHub(Counting())
        first = await hub.attach()
        for _ in range(3):
            await asyncio.wait_for(first.get(), 5.0)
        await hub.detach(first)
        second = await hub.attach()  # the source starts again at sequence 0
        kinds = [(await asyncio.wait_for(second.get(), 5.0))["type"] for _ in range(3)]
        await hub.detach(second)
        return kinds

    assert asyncio.run(go()) == ["frame", "frame", "frame"]


def test_the_hub_survives_the_event_loop_it_started_in() -> None:
    hub = live.LiveHub(live.SyntheticSource(tick_s=0.005))
    first = asyncio.run(_collect(hub, 5))
    second = asyncio.run(_collect(hub, 5))  # a new loop (a restarted server, another test client)
    assert len(first) == len(second) == 5


def test_a_payload_that_is_not_whole_samples_is_an_error_not_a_shorter_signal() -> None:
    hub = live.LiveHub(None)

    async def go() -> list[dict]:
        queue = await hub.attach()
        hub._on_frame(_frame(RawStream.ACC, 0, [1, 2, 3, 4]))  # 4 values, 3 channels
        out = []
        while not queue.empty():
            out.append(queue.get_nowait())
        await hub.detach(queue)
        return out

    messages = asyncio.run(go())
    assert [m["type"] for m in messages] == ["error"]
    assert "do not fit 3 channel" in messages[0]["error"]


def test_a_band_missing_one_characteristic_still_streams_the_others() -> None:
    from synapse_device.transport import CHAR_ECG, CHAR_MAG, SyntheticTransport, TransportError

    class NoMag(SyntheticTransport):
        async def subscribe(self, char_uuid, handler):  # noqa: ANN001, ANN202
            if char_uuid == CHAR_MAG:
                raise TransportError("characteristic not found")
            await super().subscribe(char_uuid, handler)

    transport = NoMag()

    async def go() -> list[dict]:
        hub = live.LiveHub(live.TransportSource("SYNTH", transport=transport))
        queue = await hub.attach()
        try:
            for _ in range(100):
                if CHAR_ECG in transport._subscribed:
                    break
                await asyncio.sleep(0.01)
            await transport.start([CHAR_ECG], rate_hz=200, duration_s=0.1)
            out = [await asyncio.wait_for(queue.get(), 5.0) for _ in range(2)]
            await transport.stop()
            return out
        finally:
            await hub.detach(queue)

    assert {m["stream"] for m in asyncio.run(go())} == {"ecg"}


def test_a_device_with_none_of_the_streams_is_an_error() -> None:
    from synapse_device.transport import SyntheticTransport, TransportError

    class Empty(SyntheticTransport):
        async def subscribe(self, char_uuid, handler):  # noqa: ANN001, ANN202
            raise TransportError("characteristic not found")

    hub = live.LiveHub(live.TransportSource("SYNTH", transport=Empty()))
    messages = asyncio.run(_collect(hub, 1))
    assert messages[0]["type"] == "error"
    assert "none of the raw streams" in messages[0]["error"]


@pytestmark_fastapi
def test_a_client_that_leaves_stops_the_source_at_once(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNAPSE_LIVE_SOURCE", "synthetic")
    with client.websocket_connect("/ws/live") as ws:
        ws.receive_json()  # hello
        hub = main_module._live_hub()
        assert hub._task is not None
        assert not hub._task.done()
    # the socket is closed: the disconnect is noticed by reading, not by a failed send
    for _ in range(100):  # the handler's cleanup runs on the server loop
        if hub._task is None:
            break
        time.sleep(0.02)
    assert hub._clients == set()
    assert hub._task is None


@pytestmark_fastapi
def test_a_late_client_is_told_why_the_source_is_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNAPSE_LIVE_SOURCE", "synthetic")
    main_module._live_hub().last_error = "live source stopped: adapter unplugged"
    main_module._live_hub().source = None  # nothing restarts it: the hello carries the reason
    with client.websocket_connect("/ws/live") as ws:
        assert "adapter unplugged" in ws.receive_json()["error"]


def test_the_viewer_detaches_the_old_socket_and_survives_a_bad_token() -> None:
    html = (ROOT / "backend" / "static" / "live.html").read_text(encoding="utf-8")
    assert "socket.onmessage = socket.onclose = null" in html  # a late close of the old socket
    constructor = html[html.index("try {") : html.index('setState("Connecting')]
    assert (
        "catch (err)" in constructor
    )  # the constructor throws on a token that is not a subprotocol
