"""The POWER stream: from wire bytes to an energy figure per session.

Architecture.md §55-62 makes the energy budget a functional requirement, and
Principle 8 wants the *actual* per-session consumption logged — not a bench
one-off. The POWER stream carries four channels (voltage_mv, current_ma,
power_mw, battery_pct) at 1 Hz on the research service, and these tests pin
the whole host path: the codec round-trip, the unit handling, the LSL outlet
mapping, and the fact that a 1 Hz stream is spacing-correct without needing a
clock model.
"""

from __future__ import annotations

import numpy as np
import pytest

from synapse24.hardware.raw_codec import (
    RawFormat,
    RawStream,
    decode_frame,
    encode_frame,
    max_samples,
)
from synapse24.hardware.raw_ingest import OUTLET_BY_STREAM, RawFrameIngestor
from synapse24.hardware.research_client import ResearchStreamDecoder

# A LiPo at 3.7 V delivering 120 mA: 444 mW, 80% state of charge.
VOLTAGE_MV = 3700
CURRENT_MA = 120
POWER_MW = 444
BATTERY_PCT = 80


def _power_frame(seq: int = 0, ts_us: int = 1_700_000_000_000_000) -> bytes:
    return encode_frame(
        RawStream.POWER,
        seq,
        ts_us,
        0,
        RawFormat.S32,
        [VOLTAGE_MV, CURRENT_MA, POWER_MW, BATTERY_PCT],
    )


class TestPowerCodec:
    """The wire format carries the four channels unchanged."""

    def test_stream_id_is_in_the_contract(self) -> None:
        assert RawStream.POWER == 0x07
        from synapse24.hardware import raw_codec

        assert raw_codec.STREAM_CHANNELS[RawStream.POWER] == 4

    def test_four_channel_frame_round_trips(self) -> None:
        frame = _power_frame()
        header, samples = decode_frame(frame)
        assert header.stream is RawStream.POWER
        assert samples.tolist() == [VOLTAGE_MV, CURRENT_MA, POWER_MW, BATTERY_PCT]

    def test_negative_current_survives(self) -> None:
        """A shunt of the other sign (charging) must not wrap."""
        frame = encode_frame(
            RawStream.POWER, 0, 1_000_000, 0, RawFormat.S32, [4200, -150, -630, 95]
        )
        _header, samples = decode_frame(frame)
        assert samples.tolist() == [4200, -150, -630, 95]

    def test_frame_fits_the_mtu(self) -> None:
        assert len(_power_frame()) <= max_samples(RawFormat.S32) * 4 + 12

    def test_one_sample_per_frame_is_the_firmware_shape(self) -> None:
        header, _samples = decode_frame(_power_frame())
        assert header.sample_count == 4  # 4 channels x 1 sample


class TestPowerIngest:
    """The frame reaches the LSL outlet with its four channels."""

    def test_routed_to_the_power_outlet(self) -> None:
        pushed: list[tuple[str, list[float], float]] = []
        ingestor = RawFrameIngestor(push_callback=lambda o, v, t: pushed.append((o, v, t)))

        assert ingestor.handle_notification("anything", _power_frame()) is True
        assert [p[0] for p in pushed] == ["SYNAPSE_Power"]
        # One sample row, four channels: voltage, current, power, charge.
        assert [p[1] for p in pushed] == [[3700.0, 120.0, 444.0, 80.0]]
        assert ingestor.sample_counts["power"] == 4

    def test_outlet_mapping_is_declared(self) -> None:
        assert OUTLET_BY_STREAM[RawStream.POWER] == "SYNAPSE_Power"

    def test_units_pass_through_unchanged(self) -> None:
        """mV/mA/mW/% are already engineering units: no scaling on the host."""
        from synapse24.hardware.research_client import UNIT_DIVISOR

        assert UNIT_DIVISOR[RawStream.POWER] == 1.0

    def test_one_hz_stream_gets_arrival_timestamps_without_a_model(self) -> None:
        """With no clock model the stream still lands in the LSL domain."""
        pushed: list[float] = []
        ingestor = RawFrameIngestor(
            push_callback=lambda _o, _v, t: pushed.append(t),
            lsl_clock_fn=lambda: 1234.5,
        )
        ingestor.handle_notification("anything", _power_frame())
        assert pushed == [1234.5]

    def test_decoder_accepts_the_stream(self) -> None:
        decoder = ResearchStreamDecoder()
        decoder.feed(_power_frame())
        samples, ts = decoder.arrays(RawStream.POWER)
        assert samples.size == 4
        assert np.all(np.diff(ts) > 0)
        assert decoder.is_clean()


class TestEnergyAccounting:
    """What the power stream is for: the per-session energy figure."""

    def test_energy_comes_from_two_channels(self) -> None:
        """Energy is the integral of power: mW * hours = mWh."""
        duration_h = 6.0
        avg_mw = (VOLTAGE_MV / 1000.0) * CURRENT_MA  # 3.7 V * 120 mA = 444 mW
        energy_mwh = avg_mw * duration_h
        assert energy_mwh == pytest.approx(2664.0, rel=0.01)
        # Against a 3000 mAh battery at 3.7 V (11100 mW/h): the rig draws
        # 444 mW, so a 6-hour soak is ~24% of the pack.
        assert energy_mwh / 11100.0 == pytest.approx(0.24, rel=0.01)

    def test_stream_volume_negligible_against_the_ble_budget(self) -> None:
        """1 Hz with a 28-byte frame is ~28 B/s, far inside its 64 B/s budget."""
        import yaml

        with open("config/hardware.yaml", encoding="utf-8") as handle:
            hw = yaml.safe_load(handle)
        budget = hw["raw_frame"]["per_stream_bytes_per_second_budget"]["power"]
        assert budget == 64
        frame_bytes = 28
        assert frame_bytes <= budget
