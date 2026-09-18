"""Tier 1 Coordinator for head pod + forearm hub orchestration.

Architecture.md §27-30: Decoupled sensor pods + hub architecture.
Architecture.md §33-43: Three-tier acquisition strategy.
Architecture.md §92: Clock drift risk mitigation - Tier 1 requires 1ms tolerance, 10s sync interval.

This coordinator manages:
- Head pod (Cerelog ESP-EEG): 8-ch EEG @ 500Hz, IMU @ 100Hz, Tier 1
- Forearm hub (Tier 0): PPG @ 64Hz, IMU @ 100Hz, ECG @ 250Hz, Tier 0
- MultiPodClockSync with Tier 1 budget (1ms residual, 10s interval)
- Tier 0→1 promotion/demotion with sync marker alignment
"""

from __future__ import annotations

import time
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

import numpy as np
import numpy.typing as npt

from synapse24.acquisition.clock_sync import (
    MultiPodClockSync,
    SyncConfig,
    SyncMarker,
    Tier,
    TierSyncBudget,
)
from synapse24.acquisition.state_machine import (
    AcquisitionController,
    MotionGateConfig,
    TierTransition,
    TransitionEvent,
)
from synapse24.hardware import BoardConfig, BoardManager, DeviceRegistry, SensorPodConfig
from synapse24.ingestion import CerelogEEGConfig, CerelogEEGManager
from synapse24.signal_quality import Tier as QualityTier


@dataclass
class Tier1PodState:
    """Runtime state of a Tier 1 pod (head EEG)."""

    pod_id: str
    config: SensorPodConfig
    cerelog_manager: CerelogEEGManager | None = None
    is_streaming: bool = False
    last_sync_time: float = 0.0
    clock_offset_ms: float = 0.0
    error_count: int = 0
    last_data_time: float = 0.0
    impedance_checked: bool = False
    quality_passed: bool = False


@dataclass
class Tier0PodState:
    """Runtime state of a Tier 0 pod (forearm hub)."""

    pod_id: str
    config: SensorPodConfig
    manager: BoardManager | None = None
    is_streaming: bool = False
    last_sync_time: float = 0.0
    clock_offset_ms: float = 0.0
    error_count: int = 0
    last_data_time: float = 0.0


class Tier1Coordinator:
    """Coordinates Tier 1 head pod + Tier 0 forearm hub with tight sync.

    Architecture Decision (Principal Architect):
    - Tier 0 (PPG/IMU/Temp): 10 ms tolerance, 60s sync interval
    - Tier 1 (EEG/fNIRS/ECG): 1.0 ms tolerance, 10s sync interval
    - Rationale: Tier 0's fastest signal (PPG 64Hz = 15.6ms period, IMU 100Hz = 10ms)
      does not benefit from sub-ms precision; Tier 1's ECG 700Hz (1.43ms) + EEG 500Hz (2ms)
      for cardio-neuro coherence genuinely requires ~1ms.

    Manages:
    - SensorPodCoordinator for basic pod lifecycle
    - MultiPodClockSync with per-tier budget
    - AcquisitionController for tier state machine
    - CerelogEEGManager for EEG-specific operations (impedance, quality)
    """

    def __init__(
        self,
        registry: DeviceRegistry | None = None,
        sync_config: SyncConfig | None = None,
        motion_gate: MotionGateConfig | None = None,
        clock_fn: Callable[[], float] | None = None,
    ) -> None:
        self.registry = registry or DeviceRegistry()
        self.clock_fn = clock_fn

        # Multi-pod clock sync with per-tier budget (Architecture.md §92)
        self.sync_config = sync_config or SyncConfig()
        self.clock_sync = MultiPodClockSync(self.sync_config, clock_fn=clock_fn)

        # Acquisition controller with tier state machine
        self.controller = AcquisitionController(
            clock_fn=clock_fn,
            motion_gate=motion_gate,
        )

        # Pod states
        self._tier1_pods: dict[str, Tier1PodState] = {}  # Head pods (EEG)
        self._tier0_pods: dict[str, Tier0PodState] = {}  # Forearm hubs

        # Hub reference
        self._hub_pod_id: str | None = None

        # Sync marker sequence
        self._sync_sequence = 0
        self._last_tier1_sync = 0.0
        self._last_tier0_sync = 0.0

        # Callbacks
        self._on_tier1_promotion: Callable[[], None] | None = None
        self._on_tier1_demotion: Callable[[], None] | None = None

    # ==================== Pod Registration ====================

    def register_head_pod(
        self,
        pod_id: str,
        name: str,
        cerelog_config: CerelogEEGConfig,
        placement: str = "head",
        electrode_type: str = "dry",
    ) -> None:
        """Register a Cerelog ESP-EEG head pod for Tier 1 acquisition."""
        pod_config = SensorPodConfig(
            pod_id=pod_id,
            name=name,
            board_type="CERELOG_BOARD",
            modalities=["eeg", "acc", "gyro", "mag"],
            tier=QualityTier.T1,
            channels={"eeg": cerelog_config.n_channels, "acc": 3, "gyro": 3, "mag": 3},
            sampling_rate={"eeg": cerelog_config.sampling_rate, "imu": 100},
            ble_address=cerelog_config.mac_address,
            serial_port=cerelog_config.serial_port,
            placement=placement,
            electrode_type=electrode_type,
            is_hub=False,
        )

        self.registry.register(pod_config)

        cerelog_manager = CerelogEEGManager(cerelog_config)
        self._tier1_pods[pod_id] = Tier1PodState(
            pod_id=pod_id,
            config=pod_config,
            cerelog_manager=cerelog_manager,
        )

        # Register with clock sync (ACC at 100Hz for cross-correlation)
        self.clock_sync.register_pod(pod_id, acc_sampling_rate=100)

        logger.info("Registered Tier 1 head pod: %s (%s)", pod_id, name)

    def register_forearm_hub(
        self,
        pod_id: str,
        name: str,
        board_config: BoardConfig,
        placement: str = "forearm",
    ) -> None:
        """Register a Tier 0 forearm hub pod (PPG + IMU + ECG)."""
        pod_config = SensorPodConfig(
            pod_id=pod_id,
            name=name,
            board_type=board_config.board_id,
            modalities=["ppg", "ecg", "acc", "gyro", "temp"],
            tier=QualityTier.T0,
            channels={"ppg": 3, "ecg": 1, "acc": 3, "gyro": 3, "temp": 1},
            sampling_rate={"ppg": 64, "ecg": 250, "imu": 100},
            ble_address=board_config.mac_address,
            serial_port=board_config.serial_port,
            placement=placement,
            electrode_type="wet",
            is_hub=True,
        )

        self.registry.register(pod_config)
        self._tier0_pods[pod_id] = Tier0PodState(
            pod_id=pod_id,
            config=pod_config,
            manager=None,
        )
        self._hub_pod_id = pod_id

        # Register with clock sync (ACC at 100Hz for cross-correlation)
        self.clock_sync.register_pod(pod_id, acc_sampling_rate=100)

        logger.info("Registered Tier 0 forearm hub: %s (%s)", pod_id, name)

    # ==================== Connection & Streaming ====================

    def connect_all(self) -> dict[str, bool]:
        """Connect all registered pods."""
        results = {}

        # Connect Tier 1 pods (Cerelog)
        for pod_id, state in self._tier1_pods.items():
            try:
                assert state.cerelog_manager is not None, f"Tier 1 pod {pod_id} not properly registered"
                state.cerelog_manager.prepare()
                state.impedance_checked = False
                state.quality_passed = False
                results[pod_id] = True
            except Exception:
                logger.exception("Failed to connect Tier 1 pod %s", pod_id)

        # Connect Tier 0 pods (forearm hubs)
        for pod_id, state_t0 in self._tier0_pods.items():
            try:
                board_config = state_t0.config.to_board_config()
                manager = BoardManager(board_config)
                manager.prepare()
                state_t0.manager = manager
                state_t0.error_count = 0
                results[pod_id] = True
            except Exception:
                logger.exception("Failed to connect Tier 0 pod %s", pod_id)

        return results

    def check_impedance_all(self) -> dict[str, dict[str, Any]]:
        """Check impedance on all Tier 1 head pods."""
        results = {}
        for pod_id, state in self._tier1_pods.items():
            if state.cerelog_manager:
                impedance_results = state.cerelog_manager.check_impedance()
                state.impedance_checked = True
                report = state.cerelog_manager.get_impedance_report()
                state.quality_passed = report["passed"] > 0
                results[pod_id] = report
        return results

    def start_streaming_all(self) -> dict[str, bool]:
        """Start streaming on all connected pods."""
        results = {}

        # Start Tier 1 pods
        for pod_id, state in self._tier1_pods.items():
            if state.cerelog_manager:
                try:
                    state.cerelog_manager.start_streaming()
                    state.is_streaming = True
                    state.last_data_time = time.time()
                    results[pod_id] = True
                except Exception:
                    logger.exception("Failed to start Tier 1 pod %s", pod_id)

        # Start Tier 0 pods
        for pod_id, state_t0 in self._tier0_pods.items():
            if state_t0.manager and state_t0.manager.state.name == "CONNECTED":
                try:
                    state_t0.manager.start_stream()
                    state_t0.is_streaming = True
                    state_t0.last_data_time = time.time()
                    results[pod_id] = True
                except Exception as e:
                    state_t0.error_count += 1
                    results[pod_id] = False

        # Reset sync timers
        self._last_tier1_sync = self.clock_fn() if self.clock_fn else time.time()
        self._last_tier0_sync = self._last_tier1_sync

        return results

    def stop_streaming_all(self) -> None:
        """Stop streaming on all pods."""
        for state_t1 in self._tier1_pods.values():
            if state_t1.cerelog_manager and state_t1.is_streaming:
                state_t1.cerelog_manager.stop_streaming()
                state_t1.is_streaming = False

        for state_t0 in self._tier0_pods.values():
            if state_t0.manager and state_t0.is_streaming:
                state_t0.manager.stop_stream()
                state_t0.is_streaming = False

    def disconnect_all(self) -> None:
        """Disconnect all pods."""
        self.stop_streaming_all()

        for state_t1 in self._tier1_pods.values():
            if state_t1.cerelog_manager:
                state_t1.cerelog_manager.release()
                state_t1.cerelog_manager = None

        for state_t0 in self._tier0_pods.values():
            if state_t0.manager:
                state_t0.manager.release()
                state_t0.manager = None
            state_t0.is_streaming = False

    # ==================== Real-time Data & Sync ====================

    def push_lsl_data(self) -> dict[str, int]:
        """Push latest data from all streaming pods to LSL outlets.

        Returns:
            Dict of pod_id -> samples_pushed
        """
        results = {}
        current_time = self.clock_fn() if self.clock_fn else time.time()

        # Push Tier 1 (head) data
        for pod_id, state in self._tier1_pods.items():
            if state.cerelog_manager and state.is_streaming:
                try:
                    n_pushed = state.cerelog_manager.push_lsl_chunk()
                    results[pod_id] = n_pushed
                    if n_pushed > 0:
                        state.last_data_time = current_time
                        # Update clock sync with ACC data (for cross-correlation)
                        # Note: CerelogManager doesn't expose raw ACC yet, would need extension
                except Exception:
                    logger.exception("Error pushing Tier 1 pod %s data", pod_id)

        # Push Tier 0 (forearm) data
        for pod_id, state_t0 in self._tier0_pods.items():
            if state_t0.manager and state_t0.is_streaming:
                try:
                    data = state_t0.manager.get_board_data()
                    if data.size > 0:
                        n_samples = data.shape[1]
                        timestamps = state_t0.manager.get_board_timestamp()
                        if len(timestamps) != n_samples:
                            timestamps = np.linspace(
                                current_time, current_time + n_samples / 100, n_samples
                            )

                        # Push to LSL outlets (would need LSLStreamManager integration)
                        # For now, just track that data was received
                        state_t0.last_data_time = current_time
                        results[pod_id] = n_samples

                        # Update clock sync with hub ACC (reference)
                        if "acc" in state_t0.config.modalities:
                            # Extract ACC magnitude for cross-correlation
                            acc_channels: int = 3
                            if isinstance(state_t0.config.channels, dict):
                                acc_channels = state_t0.config.channels.get("acc", 3)
                            elif isinstance(state_t0.config.channels, int):
                                acc_channels = min(3, state_t0.config.channels)
                            if data.shape[0] >= acc_channels:
                                acc_data = data[-acc_channels:, :]
                                acc_mag = np.linalg.norm(acc_data, axis=0)
                                self.clock_sync.add_hub_acc(
                                    float(np.mean(acc_mag)), float(timestamps[-1])
                                )

                except Exception:
                    logger.exception("Error pushing Tier 0 pod %s data", pod_id)

        return results

    def broadcast_sync_marker(self) -> SyncMarker | None:
        """Broadcast synchronization marker to all pods.

        Uses Tier 1 interval (10s) when in Tier 1, Tier 0 interval (60s) otherwise.
        """
        current_time = self.clock_fn() if self.clock_fn else time.time()
        tier = self.controller.state_machine.current_tier

        # Check if we should broadcast based on tier
        if tier == QualityTier.T1:
            interval = self.sync_config.tier_budget.tier1_sync_interval_s
            if current_time - self._last_tier1_sync < interval:
                return None
            self._last_tier1_sync = current_time
        else:
            interval = self.sync_config.tier_budget.tier0_sync_interval_s
            if current_time - self._last_tier0_sync < interval:
                return None
            self._last_tier0_sync = current_time

        # Broadcast marker
        marker = self.clock_sync.broadcast_sync(current_time)
        self._sync_sequence += 1

        logger.debug(
            "Broadcast sync marker %d at %.3f (tier=%s)",
            self._sync_sequence,
            current_time,
            tier.name,
        )
        return marker

    def update_clock_sync(self) -> dict[str, Any]:
        """Update clock drift estimates using both markers and ACC cross-correlation."""
        # Update with any new markers (would be called from marker callback)
        # Update with ACC cross-correlation
        estimates = self.clock_sync.update_drift_estimates()

        # Update pod state offsets
        for pod_id, estimate in estimates.items():
            if pod_id in self._tier1_pods:
                self._tier1_pods[pod_id].clock_offset_ms = estimate.offset_ms
            elif pod_id in self._tier0_pods:
                self._tier0_pods[pod_id].clock_offset_ms = estimate.offset_ms

        return self.clock_sync.get_sync_status(tier=self.controller.state_machine.current_tier)

    def check_sync_drift(self, tier: QualityTier | None = None) -> dict[str, bool]:
        """Check if any pod exceeds tier-specific drift tolerance."""
        if tier is None:
            tier = self.controller.state_machine.current_tier

        if tier == QualityTier.T1:
            max_drift = self.sync_config.tier_budget.tier1_max_residual_drift_ms
        else:
            max_drift = self.sync_config.tier_budget.tier0_max_residual_drift_ms

        status = self.clock_sync.get_sync_status(tier=tier)
        return {pod_id: info["within_tolerance"] for pod_id, info in status["pods"].items()}

    # ==================== Tier State Machine Integration ====================

    def set_tier_callbacks(
        self,
        on_promotion: Callable[[], None] | None = None,
        on_demotion: Callable[[], None] | None = None,
    ) -> None:
        """Set callbacks for Tier 1 promotion/demotion."""
        self._on_tier1_promotion = on_promotion
        self._on_tier1_demotion = on_demotion

    def handle_tier_promotion(self, event: TransitionEvent) -> None:
        """Handle Tier 0 -> Tier 1 promotion.

        Called by AcquisitionController when tier changes.
        Activates head pod streaming with tight sync budget.
        """
        if event.to_tier != QualityTier.T1:
            return

        logger.info("Tier promotion to T1: activating head pod(s)")

        # Start Tier 1 pods if not already streaming
        for pod_id, state in self._tier1_pods.items():
            if state.cerelog_manager and not state.is_streaming:
                try:
                    state.cerelog_manager.start_streaming()
                    state.is_streaming = True
                    state.last_data_time = time.time()
                except Exception:
                    logger.exception("Failed to activate Tier 1 pod %s on promotion", pod_id)

        # Ensure tight sync budget is enforced
        self._last_tier1_sync = 0  # Force immediate sync marker

        if self._on_tier1_promotion:
            self._on_tier1_promotion()

    def handle_tier_demotion(self, event: TransitionEvent) -> None:
        """Handle Tier 1 -> Tier 0 demotion.

        Stops head pod streaming, relaxes sync budget.
        """
        if event.from_tier != QualityTier.T1:
            return

        logger.info("Tier demotion to T0: deactivating head pod(s)")

        # Stop Tier 1 pods
        for pod_id, state in self._tier1_pods.items():
            if state.cerelog_manager and state.is_streaming:
                try:
                    state.cerelog_manager.stop_streaming()
                    state.is_streaming = False
                except Exception:
                    logger.exception("Failed to deactivate Tier 1 pod %s on demotion", pod_id)

        # Reset Tier 1 sync timer
        self._last_tier1_sync = 0

        if self._on_tier1_demotion:
            self._on_tier1_demotion()

    def acquire_tier1_blocking(
        self,
        duration_s: float,
        output_dir: Path,
        session_name: str | None = None,
    ) -> dict[str, Any]:
        """Acquire Tier 1 EEG data blocking with quality check and XDF export.

        Used for validation sessions and Tier 2 calibration.
        Applies drift correction via MultiPodClockSync (Architecture.md §92: <1ms residual).
        """
        if not self._tier1_pods:
            raise RuntimeError("No Tier 1 pods registered")

        # Use first head pod (extend for multi-head)
        pod_id, state = next(iter(self._tier1_pods.items()))

        if not state.cerelog_manager:
            raise RuntimeError(f"Tier 1 pod {pod_id} not prepared")

        # Ensure impedance checked
        if not state.impedance_checked:
            state.cerelog_manager.check_impedance()

        # Acquire blocking with drift correction
        eeg_data, timestamps, quality_report = state.cerelog_manager.acquire_blocking(
            duration_s=duration_s,
            check_quality=True,
            clock_sync=self.clock_sync,
            pod_id=pod_id,
        )

        # Export XDF with drift correction
        session_name = session_name or f"tier1_{pod_id}_{int(time.time())}"
        xdf_path = output_dir / f"{session_name}.xdf"
        xdf_report = state.cerelog_manager.export_xdf(
            xdf_path, eeg_data, timestamps, clock_sync=self.clock_sync, pod_id=pod_id
        )

        return {
            "pod_id": pod_id,
            "duration_s": duration_s,
            "quality": quality_report,
            "xdf_verification": xdf_report,
            "xdf_path": str(xdf_path),
        }

    # ==================== Status & Monitoring ====================

    def get_status(self) -> dict[str, Any]:
        """Get comprehensive coordinator status."""
        tier = self.controller.state_machine.current_tier
        sync_status = self.clock_sync.get_sync_status(tier=tier)

        return {
            "current_tier": tier.name,
            "tier1_pods": {
                pod_id: {
                    "name": state.config.name,
                    "connected": state.cerelog_manager is not None,
                    "streaming": state.is_streaming,
                    "impedance_checked": state.impedance_checked,
                    "quality_passed": state.quality_passed,
                    "clock_offset_ms": state.clock_offset_ms,
                    "error_count": state.error_count,
                    "last_data_age_s": time.time() - state.last_data_time
                    if state.last_data_time
                    else None,
                }
                for pod_id, state in self._tier1_pods.items()
            },
            "tier0_pods": {
                pod_id: {
                    "name": state.config.name,
                    "connected": state.manager is not None,
                    "streaming": state.is_streaming,
                    "clock_offset_ms": state.clock_offset_ms,
                    "error_count": state.error_count,
                    "last_data_age_s": time.time() - state.last_data_time
                    if state.last_data_time
                    else None,
                }
                for pod_id, state in self._tier0_pods.items()
            },
            "sync": sync_status,
            "controller": self.controller.get_status(),
            "sync_sequence": self._sync_sequence,
        }

    def __enter__(self) -> Tier1Coordinator:
        self.connect_all()
        self.check_impedance_all()
        self.start_streaming_all()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        self.disconnect_all()


# Import logger at module level
import logging

logger = logging.getLogger(__name__)

__all__ = [
    "Tier1PodState",
    "Tier0PodState",
    "Tier1Coordinator",
]
