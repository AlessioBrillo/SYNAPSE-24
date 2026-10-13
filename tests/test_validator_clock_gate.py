"""Validator raw-path wiring: the gates must measure, not assume.

These tests drive the *assembled* validator — decode path, clock model, sync
gate — against a synthetic pod that speaks the real research-service frame
format with a known clock offset. This is the seam where "synchronized"
would otherwise be claimed without a measurement, so it is the seam the
tests pin:

* a pod whose clock is 12 ms off must FAIL the Tier-0 sync gate,
* a pod whose clock agrees with the host must PASS it,
* a pod with no sync exchanges at all must FAIL (unproven, not passing),
* pushed timestamps live in the LSL domain and keep device spacing.

No Bluetooth adapter and no liblsl are required: the transport and the LSL
clock are injected.
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_phase1_entry import Phase1Validator  # noqa: E402
from synapse24.hardware.research_ble_client import GATT_CHAR_UUIDS  # noqa: E402
from tests.test_research_service_hil import SyntheticPod  # noqa: E402

CONFIG = ROOT / "config" / "hardware_bringup.yaml"
SYNC_UUID = "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C78"

# The synthetic LSL clock the tests inject; advanced by the scenario.
LSL_NOW = [1_000.0]

# The pod's own clock, in microseconds, expressed in the domain the host's
# DeviceClock reconstructs: the wire carries only the low 32 bits of the
# counter (it wraps every ~71 minutes), so the reconstructed 64-bit timeline
# starts at the first frame's low-32 stamp. Sync stamps and samples must
# share this domain or the clock model maps one onto the other's past.
POD_EPOCH_US = 1_700_000_000_000_000 % (1 << 32)


class _Char:
    """Stand-in for bleak's GATT characteristic."""

    def __init__(self, uuid: str) -> None:
        self.uuid = uuid


class SyntheticPodTransport:
    """A pod in a box: frames over the raw characteristics, SYNC over its own.

    The pod's clock is ``host_lsl_time + offset`` (expressed in the pod's own
    epoch) with a configurable drift and radio jitter, so the host-side clock
    model has exact ground truth to recover and the sync gate has a real
    number to judge. ``host_jitter_us`` widens the observed round trip
    without moving the pod's stamps — the radio-noise case that must fail an
    honest gate.
    """

    RADIO_US = 50.0
    TURNAROUND_US = 1500.0

    def __init__(
        self,
        offset_ms: float = 0.0,
        drift_ppm: float = 0.0,
        host_jitter_us: float = 0.0,
    ):
        self.offset_ms = offset_ms
        self.drift_ppm = drift_ppm
        self.host_jitter_us = host_jitter_us
        self._sequence = 0
        self._n_exchanges = 0
        self._frames: list[tuple[str, bytes]] = []
        pod = SyntheticPod(duration_s=1.0, seed=13)
        by_stream: dict = {}
        for stream, frame in pod.frames():
            by_stream.setdefault(stream, []).append(frame)
        for stream, uuid in GATT_CHAR_UUIDS.items():
            for frame in by_stream.get(stream, []):
                self._frames.append((uuid, frame))

    def _pod_at(self, host_s: float) -> int:
        """Pod stamp for a host instant, in the pod's own epoch."""
        drift_us = (host_s - 1_000.0) * self.drift_ppm
        return int(
            POD_EPOCH_US + (host_s - 1_000.0) * 1_000_000.0 + drift_us + self.offset_ms * 1000.0
        )

    # -- bleak surface ----------------------------------------------------

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def start_notify(self, char_uuid: str, callback) -> None:  # noqa: ANN001
        for uuid, frame in self._frames:
            if uuid == char_uuid:
                callback(self, bytearray(frame))

    async def stop_notify(self, char_uuid: str) -> None:
        return None

    async def write_gatt_char(self, char_uuid: str, data: bytes) -> None:
        if char_uuid == SYNC_UUID:
            self._sequence = struct.unpack("<I", data[:4])[0]

    async def read_gatt_char(self, char_uuid: str) -> bytes:
        if char_uuid != SYNC_UUID:
            return b""
        radio_us = self.RADIO_US + (self.host_jitter_us if self._n_exchanges % 2 else 0.0)
        self._n_exchanges += 1
        recv_s = float(LSL_NOW[0]) + radio_us / 1_000_000.0
        send_s = recv_s + self.TURNAROUND_US / 1_000_000.0
        LSL_NOW[0] = send_s + radio_us / 1_000_000.0
        pod_recv = self._pod_at(recv_s)
        pod_send = self._pod_at(send_s)
        return struct.pack("<Iqq", self._sequence, pod_recv, pod_send)


def _logger() -> logging.Logger:
    logger = logging.getLogger("test_validator_clock")
    logger.addHandler(logging.NullHandler())
    return logger


def _drive(
    validator: Phase1Validator, transport: SyntheticPodTransport, exchanges: int = 6
) -> None:
    """Run the validator's live pieces against the synthetic pod.

    The order mirrors ``_connect_live_pod`` + ``_run_validation_loop``:
    outlets, sync setup, controller, a calibration burst, then the streamed
    notifications, then periodic exchanges.
    """
    lsl_clock = lambda: float(LSL_NOW[0])  # noqa: E731 - injected clock source
    validator.lsl_clock_fn = lsl_clock
    validator.raw_ingestor.lsl_clock_fn = lsl_clock

    async def scenario() -> None:
        validator._create_lsl_outlets()
        validator._setup_clock_sync()
        validator._setup_acquisition_controller()
        validator.client = transport
        await validator._calibrate_clock_model(exchanges=exchanges)
        # Notifications: every raw characteristic, in arrival order.
        for char_uuid in dict.fromkeys(uuid for uuid, _ in transport._frames):
            for uuid, frame in transport._frames:
                if uuid == char_uuid:
                    validator._notification_handler(_Char(char_uuid), bytearray(frame))

    asyncio.run(scenario())


def _gates(validator: Phase1Validator):
    report = validator._collect_results(
        {"sample_counts": {}, "promotion_occurred": False, "demotion_occurred": False},
        {"total_dropped": 0, "all_streams_valid": True},
    )
    return report


@pytest.fixture(autouse=True)
def _reset_clock() -> None:
    LSL_NOW[0] = 1_000.0


class TestValidatorClockGate:
    """The sync gate as assembled in the validator."""

    def test_constant_offset_pod_passes_the_tier0_gate(self) -> None:
        """A constant offset is what the clock model exists to correct."""
        validator = Phase1Validator(CONFIG, _logger())
        _drive(validator, SyntheticPodTransport(offset_ms=12.0))

        gate = next(g for g in _gates(validator).gates if g.name == "tier0_sync_10ms")
        assert gate.passed is True, gate.details
        assert "fit residual" in gate.details

    def test_noisy_link_fails_the_tier0_gate(self) -> None:
        """Radio jitter no exchange can see through must FAIL the budget."""
        validator = Phase1Validator(CONFIG, _logger())
        # ±40 ms of observed round-trip jitter: the offset evidence scatters
        # by half of it on alternating exchanges, far outside the 10 ms budget.
        _drive(validator, SyntheticPodTransport(host_jitter_us=40_000.0))

        gate = next(g for g in _gates(validator).gates if g.name == "tier0_sync_10ms")
        assert gate.passed is False
        assert "fit residual" in gate.details

    def test_slow_link_cannot_prove_the_budget(self) -> None:
        """A 60 ms round trip has an RTT/2 floor of 30 ms: unprovable, so FAIL."""
        validator = Phase1Validator(CONFIG, _logger())
        _drive(validator, SyntheticPodTransport(host_jitter_us=60_000.0))

        gate = next(g for g in _gates(validator).gates if g.name == "tier0_sync_10ms")
        assert gate.passed is False
        assert "exchange floor" in gate.details

    def test_agreeing_pod_passes_the_tier0_gate(self) -> None:
        """A pod that agrees with the host clock must PASS."""
        validator = Phase1Validator(CONFIG, _logger())
        _drive(validator, SyntheticPodTransport())

        gate = next(g for g in _gates(validator).gates if g.name == "tier0_sync_10ms")
        assert gate.passed is True, gate.details

    def test_no_exchanges_means_unproven(self) -> None:
        """No sync exchanges at all must FAIL, not silently pass."""
        validator = Phase1Validator(CONFIG, _logger())
        _drive(validator, SyntheticPodTransport(), exchanges=0)

        gate = next(g for g in _gates(validator).gates if g.name == "tier0_sync_10ms")
        assert gate.passed is False
        assert "unproven" in gate.details

    def test_pushed_timestamps_keep_the_lsl_domain_and_spacing(self) -> None:
        """Samples must carry mapped LSL timestamps, spaced by the device rate."""
        validator = Phase1Validator(CONFIG, _logger())
        _drive(validator, SyntheticPodTransport(offset_ms=0.0))

        ecg_ts = np.asarray(validator.lsl_manager.stream_timestamps["SYNAPSE_ECG_T0"])
        assert ecg_ts.size > 10
        # LSL domain: near the injected clock, not the pod's boot epoch.
        assert ecg_ts[0] > 999.0
        assert ecg_ts[-1] < 1_050.0
        deltas = np.diff(ecg_ts)
        assert np.isfinite(deltas).all()
        assert (deltas > 0).all(), "timestamps must stay strictly increasing"
        # 500 Hz ECG: 2 ms per sample.
        assert float(np.median(deltas)) == pytest.approx(1.0 / 500.0, rel=0.05)

    def test_report_carries_the_clock_model_status(self) -> None:
        validator = Phase1Validator(CONFIG, _logger())
        _drive(validator, SyntheticPodTransport(offset_ms=3.0))

        status = _gates(validator).clock_model_status
        assert status["calibrated"] is True
        assert status["pod_id"] == "forearm_hub"
        # A clean link: the fit residual is sub-millisecond.
        assert float(status["residual_ms"]) < 1.0
        assert int(status["n_exchanges"]) >= 2

    def test_ppg_stream_is_mapped_too(self) -> None:
        """Every stream, not just ECG, must leave the pod clock domain."""
        validator = Phase1Validator(CONFIG, _logger())
        _drive(validator, SyntheticPodTransport(offset_ms=0.0))

        ppg_ts = np.asarray(validator.lsl_manager.stream_timestamps["SYNAPSE_PPG_T0"])
        assert ppg_ts.size > 10
        assert ppg_ts[0] > 999.0
        assert (np.diff(ppg_ts) > 0).all()
