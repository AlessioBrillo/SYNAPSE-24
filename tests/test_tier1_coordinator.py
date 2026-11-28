"""Fail-closed behaviour of the Tier 1 coordinator.

A synthetic payload or a fabricated host-clock timestamp must never enter a
live LSL stream, and a pod that fails to connect/start must be recorded as a
failure rather than dropped from the result dict.
"""

from __future__ import annotations

import numpy as np

from synapse24.acquisition.tier1_coordinator import Tier1Coordinator
from synapse24.hardware import BoardConfig
from synapse24.hardware.inear_eeg import InEarEEGConfig
from synapse24.ingestion import CerelogEEGConfig


class _BoomManager:
    """A device manager whose operations always fail."""

    def prepare(self) -> None:
        raise RuntimeError("device not reachable")

    def start_streaming(self) -> None:
        raise RuntimeError("device not reachable")

    def stop_streaming(self) -> None:
        return None

    def release(self) -> None:
        return None

    def push_lsl_chunk(self) -> int:
        return 0

    @property
    def state(self) -> object:
        class _State:
            name = "CONNECTED"

        return _State()


class _FakeHubManager:
    """Returns a chunk whose device clock does (not) map, per the constructor."""

    def __init__(self, with_timestamps: bool) -> None:
        self.with_timestamps = with_timestamps

    def get_board_data(self) -> np.ndarray:
        return np.zeros((11, 10))

    def get_board_timestamp(self) -> np.ndarray:
        if not self.with_timestamps:
            return np.array([], dtype=np.float64)
        return 1000.0 + np.arange(10, dtype=np.float64) / 100.0


class _FakeInEarFirmware:
    def __init__(self) -> None:
        self._eeg_outlet = object()
        self._acc_outlet = object()
        self.eeg_pushes = 0
        self.acc_pushes = 0

    def setup_lsl_streams(self) -> None:
        return None

    def start_streaming(self) -> None:
        return None

    def push_eeg_sample(self, data: np.ndarray) -> None:
        self.eeg_pushes += 1

    def push_acc_sample(self, data: np.ndarray) -> None:
        self.acc_pushes += 1


def _coroutine_with_head_pod() -> Tier1Coordinator:
    coordinator = Tier1Coordinator()
    coordinator.register_head_pod("head", "Head", CerelogEEGConfig())
    return coordinator


class TestConnectAndStartFailClosed:
    def test_connect_failure_is_recorded_not_omitted(self) -> None:
        coordinator = _coroutine_with_head_pod()
        coordinator._tier1_pods["head"].cerelog_manager = _BoomManager()  # type: ignore[assignment]

        results = coordinator.connect_all()

        assert results == {"head": False}
        assert not all(results.values())
        assert coordinator._tier1_pods["head"].error_count == 1

    def test_start_failure_is_recorded_not_omitted(self) -> None:
        coordinator = _coroutine_with_head_pod()
        coordinator._tier1_pods["head"].cerelog_manager = _BoomManager()  # type: ignore[assignment]

        results = coordinator.start_streaming_all()

        assert results == {"head": False}
        assert not all(results.values())

    def test_success_still_marks_every_pod_true(self) -> None:
        coordinator = _coroutine_with_head_pod()
        coordinator._tier1_pods["head"].cerelog_manager.prepare = lambda: None  # type: ignore[method-assign]
        coordinator._tier1_pods["head"].cerelog_manager.start_streaming = lambda: None  # type: ignore[method-assign]

        assert all(coordinator.connect_all().values())
        assert all(coordinator.start_streaming_all().values())


class TestDeviceTimestampsNeverFabricated:
    @staticmethod
    def _hub_coordinator(with_timestamps: bool) -> tuple[Tier1Coordinator, _FakeHubManager]:
        coordinator = Tier1Coordinator()
        coordinator.register_forearm_hub("hub", "Hub", BoardConfig(board_id="SYNTHETIC_BOARD"))
        manager = _FakeHubManager(with_timestamps)
        state = coordinator._tier0_pods["hub"]
        state.manager = manager  # type: ignore[assignment]
        state.is_streaming = True
        return coordinator, manager

    def test_missing_device_timestamps_drop_the_chunk(self) -> None:
        coordinator, _ = self._hub_coordinator(with_timestamps=False)

        results = coordinator.push_lsl_data()

        assert results["hub"] == 0, "no fabricated host-clock samples may be reported"
        assert "device timestamps" in coordinator.last_push_failures["hub"]
        assert coordinator._tier0_pods["hub"].error_count == 1

    def test_mapped_device_timestamps_keep_the_full_chunk(self) -> None:
        coordinator, _ = self._hub_coordinator(with_timestamps=True)

        results = coordinator.push_lsl_data()

        assert results["hub"] == 10
        assert "hub" not in coordinator.last_push_failures

    def test_clock_model_that_cannot_map_drops_the_chunk(self) -> None:
        coordinator, manager = self._hub_coordinator(with_timestamps=True)

        class _UncalibratedClock:
            def to_lsl_seconds(self, pod_us: float) -> float | None:
                return None

        manager.clock_model = _UncalibratedClock()  # type: ignore[attr-defined]
        results = coordinator.push_lsl_data()

        assert results["hub"] == 0
        assert "clock model" in coordinator.last_push_failures["hub"]


class TestInEarSyntheticTransportIsExplicit:
    @staticmethod
    def _in_ear_coordinator(
        *, synthetic: bool = False
    ) -> tuple[Tier1Coordinator, _FakeInEarFirmware]:
        coordinator = Tier1Coordinator(synthetic_inear_transport=synthetic)
        coordinator.register_in_ear_pod("ear", "Ear", InEarEEGConfig())
        firmware = _FakeInEarFirmware()
        state = coordinator._inear_pods["ear"]
        state.firmware = firmware  # type: ignore[assignment]
        state.is_streaming = True
        return coordinator, firmware

    def test_production_never_pushes_synthetic_samples(self, monkeypatch) -> None:
        def _forbidden(**kwargs: object) -> object:
            raise AssertionError("synthetic data must not be generated in production")

        monkeypatch.setattr(
            "synapse24.hardware.inear_eeg.create_synthetic_inear_eeg_data", _forbidden
        )
        coordinator, firmware = self._in_ear_coordinator()

        results = coordinator.push_lsl_data()

        assert results["ear"] == 0
        assert firmware.eeg_pushes == 0
        assert firmware.acc_pushes == 0
        assert "synthetic transport is not enabled" in coordinator.last_push_failures["ear"]

    def test_explicit_rehearsal_mode_still_pushes(self, monkeypatch) -> None:
        def _synthetic(**kwargs: object) -> dict[str, np.ndarray]:
            t = np.arange(20, dtype=np.float64) / 256.0
            ones = np.ones(20)
            return {
                "eeg": np.zeros((2, 20)),
                "acc_x": ones,
                "acc_y": ones,
                "acc_z": ones,
                "t_eeg": t,
            }

        monkeypatch.setattr(
            "synapse24.hardware.inear_eeg.create_synthetic_inear_eeg_data", _synthetic
        )
        coordinator, firmware = self._in_ear_coordinator(synthetic=True)

        results = coordinator.push_lsl_data()

        assert results["ear"] == 20
        assert firmware.eeg_pushes == 1
        assert firmware.acc_pushes == 20
