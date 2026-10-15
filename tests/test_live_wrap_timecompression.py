"""Time-compressed wrap and soak behaviour: what only hardware seemed to test.

The SOP's 6-hour soak exists to exercise three things a 30-minute gate run
cannot: the pod's 32-bit microsecond counter wrapping (every ~71.6 minutes),
the pod→host clock model over a long span, and the decoder's honesty after a
reconnect. All three are pure clock arithmetic, so they can be simulated at
1000x speed: a synthetic session of 10 hours runs in milliseconds here.

This file is the reason "wait for the soak" is no longer an acceptable answer
for the wrap path — the wrap is exercised in CI, where a regression fails the
build instead of the bench session.

Faults covered: 32-bit timestamp wrap (with and without the flag), truncated
notifications, and a pod reconnect that restarts its sequence at zero (a
reconnect is not 65533 lost frames).
"""

from __future__ import annotations

import numpy as np
import pytest

from synapse24.acquisition.pod_clock_model import ClockExchange, PodClockModel
from synapse24.hardware.raw_codec import (
    FRAME_VERSION,
    RawCodecError,
    RawFormat,
    RawStream,
    decode_frame,
    encode_frame,
)
from synapse24.hardware.research_client import TS_WRAP_US, ResearchStreamDecoder

# --- the compressed session -------------------------------------------------

SOAK_HOURS = 10.0
SOAK_SECONDS = SOAK_HOURS * 3600.0
FRAME_STEP_S = 600.0  # one ECG frame every 10 minutes: 60 frames = 10 hours
ECG_PER_FRAME = 50

BASE_US = 1_700_000_000_000_000


def _soak_frames(n_frames: int = 60, flags_on_wrap: bool = True) -> list[bytes]:
    """ECG frames whose device timestamps advance 10 h in 10-minute steps.

    The header only carries the low 32 bits, so the wrap shows up as the low
    bits stepping backwards — exactly what the DeviceClock rebuilds from.
    """
    frames = []
    previous_low: int | None = None
    for i in range(n_frames):
        ts_us = BASE_US + int(i * FRAME_STEP_S * 1_000_000.0)
        low = ts_us & 0xFFFFFFFF
        flags = 0
        if flags_on_wrap and previous_low is not None and low < previous_low:
            flags |= 0x01  # SYNAPSE_RAW_FLAG_TS_EPOCH_WRAPPED
        samples = [100 + (j % 7) for j in range(ECG_PER_FRAME)]
        frames.append(encode_frame(RawStream.ECG, i, low, flags, RawFormat.S16, samples))
        previous_low = low
    return frames


class TestWrapTimeCompression:
    """10 hours of device clock in milliseconds.

    A synthetic session of 10 hours runs in milliseconds here; every frame
    carries only the low 32 bits of the device counter, so the wrap shows up
    on the wire exactly as it does on a real soak.
    """

    def test_soak_timestamps_are_monotonic_across_wraps(self) -> None:
        decoder = ResearchStreamDecoder()
        for frame in _soak_frames():
            decoder.feed(frame)

        samples, timestamps_us = decoder.arrays(RawStream.ECG)
        assert samples.size == 60 * ECG_PER_FRAME
        assert np.all(np.diff(timestamps_us) > 0), "a wrapped session went backwards"

        # The reconstructed timeline spans the whole compressed session:
        # 60 frames at 10-minute steps cover 59 steps = 35400 s (~9.8 h).
        expected_span_us = (60 - 1) * FRAME_STEP_S * 1_000_000.0
        span_us = timestamps_us[-1] - timestamps_us[0]
        assert span_us == pytest.approx(expected_span_us, rel=0.01)

    def test_decoder_counts_the_wraps_the_session_contains(self) -> None:
        decoder = ResearchStreamDecoder()
        for frame in _soak_frames():
            decoder.feed(frame)

        expected_wraps = int((60 - 1) * FRAME_STEP_S * 1_000_000.0 // TS_WRAP_US)
        wraps = {stream: clock.wraps for stream, clock in decoder.clocks.items()}
        assert wraps[RawStream.ECG] == expected_wraps, (
            f"expected {expected_wraps} 32-bit wraps in the compressed session"
        )

    def test_wrap_flag_is_carried_on_the_frame_after_a_wrap(self) -> None:
        """The firmware's flag and the numeric regression are independent witnesses.

        The host rebuilds the 64-bit timeline from whichever witness fires, so
        both spellings of the same event must decode to the same world.
        """
        decoder = ResearchStreamDecoder()
        n_flagged = 0
        for frame in _soak_frames(flags_on_wrap=True):
            header, _ = decode_frame(frame)
            decoder.feed(frame)
            if header.flags & 0x01:
                n_flagged += 1
        assert n_flagged == int((60 - 1) * FRAME_STEP_S * 1_000_000.0 // TS_WRAP_US)
        assert decoder.is_clean()

    def test_clock_survives_the_wrap_without_the_flag(self) -> None:
        """The numeric regression alone must rebuild the timeline.

        The flag was dead code on the real firmware for a long time, so the
        host must never depend on it: the regression witness is authoritative
        and the flag is belt-and-braces.
        """
        decoder = ResearchStreamDecoder()
        for frame in _soak_frames(flags_on_wrap=False):
            decoder.feed(frame)

        _samples, timestamps_us = decoder.arrays(RawStream.ECG)
        assert np.all(np.diff(timestamps_us) > 0)

    def test_truncated_notifications_are_rejected_not_crashed(self) -> None:
        decoder = ResearchStreamDecoder()
        good = _soak_frames(n_frames=3)
        decoder.feed(good[0])

        # A notification cut mid-frame (BLE buffer underrun): the decoder
        # counts the rejection and re-raises so the caller can fall back —
        # either way it must never crash the recording or mis-parse it.
        with pytest.raises(RawCodecError):
            decoder.feed(good[1][:5])
        decoder.feed(good[2])

        assert decoder.frames_rejected == 1
        assert decoder.frames_seen == 2
        assert decoder.is_clean() is False

    def test_reconnect_is_not_65533_lost_frames(self) -> None:
        """A pod that comes back restarts its sequence at zero.

        The decoder must read that as a new session, not as a catastrophic
        gap: reporting a reconnect as ~65533 lost frames trains the reader
        to ignore the drop gate.
        """
        decoder = ResearchStreamDecoder()
        for frame in _soak_frames(n_frames=3):
            decoder.feed(frame)

        decoder.reset()  # what the validator does at the start of a recording
        for frame in _soak_frames(n_frames=2):
            decoder.feed(frame)

        assert decoder.drops == [], "a reconnect was counted as frame loss"
        assert decoder.is_clean()


class TestPodClockModelOverASoak:
    """The pod→hub fit must hold its numbers over a 10-hour span."""

    def test_drift_and_offset_recovered_over_ten_hours(self) -> None:
        model = PodClockModel(pod_id="forearm_hub")
        pod_drift_ppm = 35.0
        offset_s = 0.012
        rtt_us = 40_000.0

        # 60 exchanges, one every 10 minutes, pod clock running 35 ppm fast.
        for i in range(60):
            host_mid_s = 1_000.0 + i * FRAME_STEP_S
            pod_mid_us = (host_mid_s + offset_s) * 1_000_000.0 * (1.0 + pod_drift_ppm * 1e-6)
            half_rtt_us = rtt_us / 2.0
            model.record_exchange(
                ClockExchange(
                    sequence=i,
                    pod_recv_us=int(pod_mid_us - 100),
                    pod_send_us=int(pod_mid_us + 100),
                    host_send_us=int((host_mid_s - half_rtt_us / 1e6) * 1_000_000),
                    host_recv_us=int((host_mid_s + half_rtt_us / 1e6) * 1_000_000),
                )
            )

        status = model.status()
        assert status["calibrated"] is True
        assert status["n_exchanges"] == 60
        assert status["span_s"] == pytest.approx((60 - 1) * FRAME_STEP_S, rel=0.05)
        assert status["drift_ppm"] == pytest.approx(pod_drift_ppm, abs=1.0)
        assert status["residual_ms"] < 10.0

        # The mapping itself must be monotonic over the whole session.
        mapped = [model.to_lsl_seconds(us) for us in np.linspace(0, 36_000_000_000, 1000)]
        assert all(b > a for a, b in zip(mapped, mapped[1:]))

    def test_a_wild_exchange_cannot_drag_the_fit(self) -> None:
        model = PodClockModel(pod_id="forearm_hub")
        for i in range(40):
            host_mid_s = 1_000.0 + i * FRAME_STEP_S
            pod_mid_us = host_mid_s * 1_000_000.0
            model.record_exchange(
                ClockExchange(
                    sequence=i,
                    pod_recv_us=int(pod_mid_us - 100),
                    pod_send_us=int(pod_mid_us + 100),
                    host_send_us=int((host_mid_s - 0.02) * 1_000_000),
                    host_recv_us=int((host_mid_s + 0.02) * 1_000_000),
                )
            )
        # One radio stall 2 minutes wide in the middle.
        model.record_exchange(
            ClockExchange(
                sequence=99,
                pod_recv_us=int((1_000.0 + 20 * FRAME_STEP_S) * 1_000_000),
                pod_send_us=int((1_000.0 + 20 * FRAME_STEP_S) * 1_000_000 + 120_000),
                host_send_us=int((1_000.0 + 20 * FRAME_STEP_S - 120.0) * 1_000_000),
                host_recv_us=int((1_000.0 + 20 * FRAME_STEP_S) * 1_000_000),
            )
        )
        status = model.status()
        assert status["drift_ppm"] == pytest.approx(0.0, abs=1.0)
        assert status["residual_ms"] < 10.0
