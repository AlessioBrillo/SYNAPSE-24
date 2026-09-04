"""Sensor pod coordinator for multi-pod synchronization."""

from __future__ import annotations

import time
import types
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import numpy.typing as npt

from synapse24.acquisition.state_machine import TierTransition, TransitionEvent
from synapse24.hardware import BoardConfig, BoardManager, DeviceRegistry, SensorPodConfig
from synapse24.signal_quality import Tier


@dataclass
class PodState:
    """Runtime state of a sensor pod."""

    pod_id: str
    config: SensorPodConfig
    manager: BoardManager | None = None
    is_streaming: bool = False
    last_sync_time: float = 0
    clock_offset_ms: float = 0.0  # Offset from hub clock
    error_count: int = 0
    last_data_time: float = 0


class SensorPodCoordinator:
    """Coordinates multiple sensor pods and hub.

    Architecture.md §27-30: Decoupled pods (head, in-ear, forearm) + hub.
    - All pods stream via LSL with local_clock() timestamps
    - Hub records all streams simultaneously
    - Post-hoc alignment using XDF per-stream timestamps
    - Optional: periodic sync markers for drift quantification
    """

    def __init__(self, registry: DeviceRegistry | None = None) -> None:
        self.registry = registry or DeviceRegistry()
        self._pods: dict[str, PodState] = {}
        self._hub_pod_id: str | None = None
        self._sync_interval_s = 60.0  # Sync marker interval
        self._last_sync_marker = 0.0

    def register_pod(self, pod: SensorPodConfig) -> None:
        """Register a sensor pod."""
        self.registry.register(pod)
        self._pods[pod.pod_id] = PodState(pod_id=pod.pod_id, config=pod)

        if pod.is_hub:
            self._hub_pod_id = pod.pod_id

    def connect_all(self) -> dict[str, bool]:
        """Connect all registered pods."""
        results = {}
        for pod_id, state in self._pods.items():
            try:
                board_config = state.config.to_board_config()
                manager = BoardManager(board_config)
                manager.prepare()
                state.manager = manager
                state.error_count = 0
                results[pod_id] = True
            except Exception as e:
                state.error_count += 1
                results[pod_id] = False
        return results

    def start_streaming_all(self) -> dict[str, bool]:
        """Start streaming on all connected pods."""
        results = {}
        for pod_id, state in self._pods.items():
            if state.manager and state.manager.state.name == "CONNECTED":
                try:
                    state.manager.start_stream()
                    state.is_streaming = True
                    state.last_data_time = time.time()
                    results[pod_id] = True
                except Exception:
                    state.error_count += 1
                    results[pod_id] = False
            else:
                results[pod_id] = False
        return results

    def stop_streaming_all(self) -> None:
        """Stop streaming on all pods."""
        for state in self._pods.values():
            if state.manager and state.is_streaming:
                state.manager.stop_stream()
                state.is_streaming = False

    def disconnect_all(self) -> None:
        """Disconnect all pods."""
        for state in self._pods.values():
            if state.manager:
                state.manager.release()
                state.manager = None
            state.is_streaming = False

    def get_pod_data(
        self, pod_id: str, num_samples: int | None = None
    ) -> npt.NDArray[np.float64] | None:
        """Get data from a specific pod."""
        state = self._pods.get(pod_id)
        if state and state.manager and state.is_streaming:
            data = state.manager.get_board_data(num_samples)
            state.last_data_time = time.time()
            return data
        return None

    def get_all_data(
        self, num_samples: int | None = None
    ) -> dict[str, npt.NDArray[np.float64] | None]:
        """Get data from all streaming pods."""
        return {
            pod_id: self.get_pod_data(pod_id, num_samples)
            for pod_id in self._pods
            if self._pods[pod_id].is_streaming
        }

    def on_tier_change(self, event: TransitionEvent) -> None:
        """Handle tier transition from state machine."""
        # Notify pods of tier change
        for pod_id, state in self._pods.items():
            # Pods with matching tier should activate/deactivate
            if event.to_tier == state.config.tier:
                # Activate pod for this tier
                pass
            elif event.from_tier == state.config.tier:
                # Deactivate pod (will be handled by controller)
                pass

    def sync_all_to_hub(self) -> dict[str, float]:
        """Synchronize all pods to hub clock.

        In practice, all pods use pylsl.local_clock() which is NTP-synchronized.
        This method quantifies clock drift between pods.
        """
        from pylsl import local_clock

        hub_time = local_clock()
        offsets = {}

        for pod_id, state in self._pods.items():
            if state.manager and state.is_streaming:
                # Get pod's latest timestamp
                pod_timestamps = state.manager.get_board_timestamp()
                if len(pod_timestamps) > 0:
                    pod_time = pod_timestamps[-1]
                    offset = (pod_time - hub_time) * 1000  # ms
                    state.clock_offset_ms = offset
                    offsets[pod_id] = offset

        self._last_sync_marker = hub_time
        return offsets

    def check_sync_drift(self, max_drift_ms: float = 10.0) -> dict[str, bool]:
        """Check if any pod exceeds max drift from hub."""
        offsets = self.sync_all_to_hub()
        return {pod_id: abs(offset) <= max_drift_ms for pod_id, offset in offsets.items()}

    def send_sync_marker(self, label: str = "SYNC") -> None:
        """Send synchronization marker to all pods' marker streams."""
        # In practice, this would push to an LSL marker stream
        # that all pods and hub subscribe to

    def get_pod_status(self, pod_id: str) -> dict[str, Any] | None:
        """Get status of a specific pod."""
        state = self._pods.get(pod_id)
        if not state:
            return None

        return {
            "pod_id": pod_id,
            "name": state.config.name,
            "tier": state.config.tier.name,
            "modalities": state.config.modalities,
            "connected": state.manager is not None,
            "streaming": state.is_streaming,
            "clock_offset_ms": state.clock_offset_ms,
            "error_count": state.error_count,
            "last_data_age_s": time.time() - state.last_data_time if state.last_data_time else None,
        }

    def get_all_status(self) -> dict[str, Any]:
        """Get status of all pods."""
        return {
            "hub_pod": self._hub_pod_id,
            "pods": {pod_id: self.get_pod_status(pod_id) for pod_id in self._pods},
            "sync": {
                "last_sync_marker": self._last_sync_marker,
                "sync_interval_s": self._sync_interval_s,
            },
        }

    def __enter__(self) -> SensorPodCoordinator:
        self.connect_all()
        self.start_streaming_all()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        self.stop_streaming_all()
        self.disconnect_all()
