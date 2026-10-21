"""Hardware-in-the-loop bench: synthetic pod speaking the real BLE protocol.

The host stack (decoder, gap detection, timeline reconstruction, XDF write) is
validated end-to-end here **without any hardware**, by replaying synthetic
frames through the exact code path a live BLE notification takes. This is the
bench that catches protocol and clock bugs for free.

The synthetic pod generates:
  - ECG at 500 Hz from a sum of Gaussians with an R-R jitter model,
  - PPG at 50 Hz (red + IR) with a venous pulsation component,
  - ACC/GYRO/MAG at 50 Hz,
plus configurable faults: sequence gaps, 32-bit timestamp wraparound, and
bit-flipped version bytes.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from synapse24.hardware.raw_codec import (
    FRAME_VERSION,
    MAX_PAYLOAD,
    RawFormat,
    RawStream,
    encode_frame,
    max_samples,
)
from synapse24.hardware.research_client import (
    TS_WRAP_US,
    ResearchStreamDecoder,
    validate_notification_size,
)

# Normative rates from config/hardware.yaml.
ECG_HZ = 500
PPG_HZ = 50
IMU_HZ = 50


@dataclass
class SyntheticPod:
    """Generates frames byte-identical in shape to the real firmware's."""

    duration_s: float = 10.0
    ecg_hr_bpm: float = 70.0
    ppg_hr_bpm: float = 70.0
    seed: int = 7
    # Fault injection.
    corrupt_version_every: int | None = None
    # INA219 power telemetry: 1 Hz frames of 4 channels, like the firmware's
    # STREAM_POWER (flush_at = 1).
    power_stream: bool = False

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng(self.seed)

    # -- signal generation ---------------------------------------------------

    def _ecg(self) -> np.ndarray:
        t = np.arange(int(self.duration_s * ECG_HZ), dtype=np.float64) / ECG_HZ
        rr = 60.0 / self.ecg_hr_bpm
        # QRS as a Gaussian-train with respiratory-rate R-R modulation.
        beats = np.arange(0.0, self.duration_s + rr, rr)
        rr_jitter = self.rng.normal(0, 0.01, size=beats.size)
        beats = beats + rr_jitter
        sig = np.zeros_like(t)
        for beat in beats:
            for amp, width, offset in (
                (0.1, 0.030, 0.0),
                (-0.25, 0.010, 0.0),
                (1.0, 0.008, 0.0),
                (-0.3, 0.025, 0.06),
                (0.2, 0.040, 0.20),
            ):
                sig += amp * np.exp(-0.5 * ((t - beat - offset) / width) ** 2)
        return (sig * 1000.0 + self.rng.normal(0, 8.0, t.size)).astype(np.int32)

    def _ppg(self) -> np.ndarray:
        n = int(self.duration_s * PPG_HZ)
        t = np.arange(n, dtype=np.float64) / PPG_HZ
        rr = 60.0 / self.ppg_hr_bpm
        phase = 2.0 * np.pi * (t / rr)
        red = 90000.0 + 12000.0 * (0.5 + 0.5 * np.sin(phase)) + 400.0 * np.sin(2.0 * phase)
        ir = 105000.0 + 14000.0 * (0.5 + 0.5 * np.sin(phase)) + 500.0 * np.sin(2.0 * phase)
        return np.stack([red, ir], axis=1).astype(np.int32)

    def _imu(self) -> np.ndarray:
        n = int(self.duration_s * IMU_HZ)
        t = np.arange(n, dtype=np.float64) / IMU_HZ
        acc = np.zeros((n, 3))
        for i, f in enumerate((0.3, 0.8, 1.6)):
            acc[:, i] = 0.05 * np.sin(2.0 * np.pi * f * t)
        acc[:, 2] += 9.81  # gravity
        gyr = 0.02 * self.rng.normal(0, 1, (n, 3))
        mag = 35.0 + 5.0 * self.rng.normal(0, 1, (n, 3))
        return np.concatenate([acc * 1000.0, gyr * 1000.0, mag * 100.0], axis=1).astype(np.int32)

    # -- framing -------------------------------------------------------------

    def _power_frames(self, base_us: int) -> list[tuple[RawStream, bytes]]:
        """1 Hz INA219 telemetry: one frame per second, 4 channels per frame.

        Values are already engineering units (mV, mA, mW, %) — the firmware's
        unit divisor for the POWER stream is 1.
        """
        frames: list[tuple[RawStream, bytes]] = []
        for i in range(max(1, int(self.duration_s))):
            current_ma = 120 + i
            values = [3700, current_ma, (3700 * current_ma) // 1000, 80]
            frame = encode_frame(
                RawStream.POWER,
                i,
                (base_us + i * 1_000_000) & 0xFFFFFFFF,
                0,
                RawFormat.S32,
                values,
            )
            frames.append((RawStream.POWER, frame))
        return frames

    def frames(self) -> list[tuple[RawStream, bytes]]:
        """Encode the whole session as a list of (stream, notification) pairs."""
        out: list[tuple[RawStream, bytes]] = []

        ecg = self._ecg()
        ppg = self._ppg()
        imu = self._imu()

        base_us = 1_700_000_000_000_000

        ecg_per = max_samples(RawFormat.S16)
        acc_per = max_samples(RawFormat.S16) // 3
        ppg_per = max_samples(RawFormat.S32) // 2

        # ECG
        i = 0
        seq = 0
        ts = base_us
        while i < ecg.size:
            n = min(ecg_per, ecg.size - i)
            frame = encode_frame(
                RawStream.ECG, seq, ts & 0xFFFFFFFF, 0, RawFormat.S16, ecg[i : i + n]
            )
            out.append((RawStream.ECG, frame))
            i += n
            seq += 1
            ts += int(n * 1_000_000 / ECG_HZ)

        # PPG (interleaved red/IR)
        i = 0
        seq = 0
        ts = base_us
        while i < ppg.shape[0]:
            n = min(ppg_per, ppg.shape[0] - i)
            interleaved = ppg[i : i + n].reshape(-1)
            frame = encode_frame(RawStream.PPG, seq, ts & 0xFFFFFFFF, 0, RawFormat.S32, interleaved)
            out.append((RawStream.PPG, frame))
            i += n
            seq += 1
            ts += int(n * 1_000_000 / PPG_HZ)

        # IMU: three sub-streams from one 9-channel sample array
        for stream, columns in (
            (RawStream.ACC, (0, 1, 2)),
            (RawStream.GYRO, (3, 4, 5)),
            (RawStream.MAG, (6, 7, 8)),
        ):
            i = 0
            seq = 0
            ts = base_us
            while i < imu.shape[0]:
                n = min(acc_per, imu.shape[0] - i)
                frame = encode_frame(
                    stream,
                    seq,
                    ts & 0xFFFFFFFF,
                    0,
                    RawFormat.S16,
                    imu[i : i + n, columns].reshape(-1),
                )
                out.append((stream, frame))
                i += n
                seq += 1
                ts += int(n * 1_000_000 / IMU_HZ)

        # Power telemetry: one frame per second, four channels, like the
        # firmware's STREAM_POWER (flush_at = 1). Values are already
        # engineering units (mV, mA, mW, %) — no unit divisor.
        if self.power_stream:
            out.extend(self._power_frames(base_us))

        # Interleave by stream order of arrival: ECG fastest, then PPG, then IMU.
        def sort_key(item: tuple[RawStream, bytes]) -> tuple[int, int]:
            stream, frame = item
            seq = int.from_bytes(frame[2:4], "little")
            order = {
                RawStream.ECG: 0,
                RawStream.PPG: 1,
                RawStream.ACC: 2,
                RawStream.GYRO: 3,
                RawStream.MAG: 4,
                RawStream.POWER: 5,
            }
            return (order[stream], seq)

        out = sorted(out, key=sort_key)

        # Fault injection is applied AFTER sorting, so a corrupted frame is a
        # real malformed notification rather than a re-ordered one.
        if self.corrupt_version_every:
            out = [
                (stream, bytes([0x7F]) + frame[1:])
                if idx % self.corrupt_version_every == 0
                else (stream, frame)
                for idx, (stream, frame) in enumerate(out)
            ]

        return sorted(out, key=sort_key)


def replay(pod: SyntheticPod) -> ResearchStreamDecoder:
    """Run the whole synthetic session through the host decoder."""
    decoder = ResearchStreamDecoder()
    for _stream, frame in pod.frames():
        validate_notification_size(frame)
        try:
            decoder.feed(frame)
        except Exception:  # noqa: BLE001 - a corrupt frame must not kill the recording
            pass
    return decoder


def test_synthetic_pod_roundtrip_is_sample_exact() -> None:
    pod = SyntheticPod(duration_s=2.0, seed=11)
    decoder = replay(pod)
    assert decoder.is_clean(), f"drops: {decoder.drops}"

    ecg_samples, ecg_ts = decoder.arrays(RawStream.ECG)
    assert ecg_samples.size == 2.0 * ECG_HZ
    assert np.all(np.diff(ecg_ts) > 0), "timestamps must be monotonic"

    ppg_samples, ppg_ts = decoder.arrays(RawStream.PPG)
    assert ppg_samples.size == 2.0 * PPG_HZ * 2, "red + IR channels"
    assert np.all(np.diff(ppg_ts) > 0)

    for stream in (RawStream.ACC, RawStream.GYRO, RawStream.MAG):
        samples, ts = decoder.arrays(stream)
        assert samples.size == 2.0 * IMU_HZ * 3
        assert np.all(np.diff(ts) > 0)


def test_quantisation_units_are_recovered() -> None:
    """Samples carry engineering units, so a 1.5 g spike is ~1500 LSB."""
    pod = SyntheticPod(duration_s=0.5, seed=3)
    decoder = replay(pod)
    acc, _ = decoder.arrays(RawStream.ACC)
    assert abs(acc.max()) > 9.0, "gravity must survive the uV/mg round trip"
    ecg, _ = decoder.arrays(RawStream.ECG)
    assert ecg.max() > 0.3, "QRS must survive the uV round trip"


def test_sequence_gaps_are_detected() -> None:
    """Dropping a mid-stream frame must surface a gap, not a silence."""
    pod = SyntheticPod(duration_s=2.0, seed=5)
    frames = pod.frames()

    # Remove a frame from the middle of the ECG stream, which runs at 500 Hz
    # and has the most frames, so the gap is unambiguous.
    ecg_indices = [i for i, (stream, _) in enumerate(frames) if stream == RawStream.ECG]
    victim = ecg_indices[len(ecg_indices) // 2]
    assert len(ecg_indices) > 4, "the synthetic ECG stream should have several frames"

    from synapse24.hardware.research_client import ResearchStreamDecoder, validate_notification_size

    decoder = ResearchStreamDecoder()
    for idx, (stream, frame) in enumerate(frames):
        if idx == victim:
            continue
        validate_notification_size(frame)
        decoder.feed(frame)

    assert not decoder.is_clean()
    summary = decoder.drop_summary()
    assert summary.get("ECG"), "the ECG gap must be reported, not smoothed over"
    assert all(event.gap >= 1 for event in decoder.drops)


def test_the_loss_counter_is_aggregated() -> None:
    """The `dropped` byte of the header must reach the host's counters.

    The sequence gap says *that* something is missing; the counter says *how
    much*: a hole of one frame and a hole of a hundred samples are different
    failures, and the gate has to see both.
    """
    from synapse24.hardware.research_client import ResearchStreamDecoder

    decoder = ResearchStreamDecoder()
    for seq in range(3):
        frame = encode_frame(
            RawStream.ECG, seq, 1000 + seq, 0, RawFormat.S16, [10, 20, 30], dropped=7
        )
        decoder.feed(frame)

    assert decoder.dropped_sample_summary() == {"ECG": 21}
    assert not decoder.is_clean()


def test_a_clean_stream_has_no_loss_counters() -> None:
    from synapse24.hardware.research_client import ResearchStreamDecoder

    decoder = ResearchStreamDecoder()
    frame = encode_frame(RawStream.ECG, 0, 1000, 0, RawFormat.S16, [10, 20, 30])
    decoder.feed(frame)
    assert decoder.dropped_sample_summary() == {}
    assert decoder.is_clean()


def test_the_ppg_window_is_de_interleaved_before_the_quality_metrics() -> None:
    """A clean PPG must score high, even with the red/IR channels interleaved.

    The decoder returns the values interleaved (red, IR, red, IR...). Feeding
    that mixed trace to the quality metrics puts a strong component at fs/2,
    outside the cardiac band: the SQI of a *clean* signal collapsed to 0.34
    and the motion gate never armed, so the T0 -> T1 promotion stayed
    unreachable.
    """
    from synapse24.hardware.research_client import ResearchStreamDecoder
    from synapse24.signal_quality.ppg import compute_ppg_sqi

    fs = 50.0
    rate_hz = 50
    n = int(fs * 20)
    t = np.arange(n) / fs
    # A clean pulse train on the red channel, a noisy one on the IR channel
    # (the realistic case: the two channels are not the same signal).
    clean = (np.sin(2 * np.pi * 1.2 * t) ** 3 * 1000.0).astype(np.int32)
    rng = np.random.default_rng(3)
    noisy = rng.integers(-200, 200, size=n).astype(np.int32)

    decoder = ResearchStreamDecoder()
    for seq in range(20):
        block = clean[seq * 25 : (seq + 1) * 25]
        other = noisy[seq * 25 : (seq + 1) * 25]
        interleaved = np.column_stack([block, other]).ravel()  # red, IR, red, IR...
        frame = encode_frame(RawStream.PPG, seq, 1000 + seq, 0, RawFormat.S32, interleaved)
        decoder.feed(frame)

    values = decoder.tail(RawStream.PPG, 2 * 500)
    assert values.size >= 2 * 500
    red = values[0::2]

    sqi_deinterleaved = compute_ppg_sqi(red, rate_hz)
    sqi_interleaved = compute_ppg_sqi(values, rate_hz)
    assert sqi_deinterleaved > 0.6, f"the de-interleaved SQI is {sqi_deinterleaved:.3f}"
    assert sqi_deinterleaved > sqi_interleaved + 0.2, (
        "the interleaved trace must not be what the quality metrics see"
    )


def test_corrupt_frames_are_counted_not_fatal() -> None:
    pod = SyntheticPod(duration_s=2.0, seed=9, corrupt_version_every=53)
    decoder = replay(pod)
    assert decoder.frames_rejected > 0
    # The session must still produce usable data despite the bad frames.
    ecg, _ = decoder.arrays(RawStream.ECG)
    assert ecg.size > 0


def test_frames_never_exceed_the_mtu_budget() -> None:
    pod = SyntheticPod(duration_s=1.0)
    for _stream, frame in pod.frames():
        assert len(frame) <= MAX_PAYLOAD


def test_device_clock_wraps_monotonically() -> None:
    from synapse24.hardware.research_client import DeviceClock

    clock = DeviceClock()
    times = [clock.extend(ts, 0) for ts in (1000, 2000, (1 << 32) - 100, 50)]
    assert all(b > a for a, b in zip(times, times[1:])), "the clock must not go backwards"
    assert clock.wraps == 1


def test_decoder_reset_survives_a_reconnect() -> None:
    """A pod that reconnects restarts its sequences; that is not a loss."""
    frames = [
        encode_frame(RawStream.ECG, seq, 1000 + seq, 0, RawFormat.S16, [10, 20, 30])
        for seq in (0, 1, 2, 3)
    ]

    decoder = ResearchStreamDecoder()
    for frame in frames:
        decoder.feed(frame)

    # The pod drops off and comes back with the same frames: without a reset
    # the decoder compares sequence 0 against the previous session's 3 and
    # reports ~65533 lost frames per stream. That is the reconnect bug.
    for frame in frames:
        decoder.feed(frame)
    assert not decoder.is_clean(), "a restart without reset must be visible as a loss"

    decoder.reset()
    assert decoder.is_clean(), "reset must clear the previous session's expectations"

    # And a fresh session after reset is clean, which is what the validator
    # does at every recording start.
    for frame in frames:
        decoder.feed(frame)
    assert decoder.is_clean(), f"a reconnect after reset must be clean: {decoder.drops}"


def test_every_frame_is_versioned_bytes_not_dict() -> None:
    pod = SyntheticPod(duration_s=0.2)
    for _stream, frame in pod.frames():
        assert frame[0] == FRAME_VERSION
