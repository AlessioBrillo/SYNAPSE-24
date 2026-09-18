"""LSL Gateway for hub-side BLE/USB synchronization.

Architecture.md §27-30: Decoupled sensor pods + hub.
Architecture.md §92: Multi-pod clock synchronization with per-tier budget.

This module provides the hub-side LSL gateway that:
- Receives BLE/USB data from sensor pods
- Forwards to LSL outlets with proper StreamInfo
- Runs MultiPodClockSync for drift estimation and correction
- Handles sync marker broadcast/receipt
- Provides real-time data access for acquisition controller
"""

from __future__ import annotations

import asyncio
import logging
import time
import types
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Optional

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
    TierTransition,
    TransitionEvent,
)
from synapse24.utils import (
    LSLStreamManager,
    StreamConfig,
    create_stream_info,
)

try:
    from pylsl import StreamInfo, StreamInlet, StreamOutlet, local_clock, resolve_streams
except ImportError:  # pragma: no cover
    StreamInlet = None
    StreamOutlet = None
    StreamInfo = None
    local_clock = None
    resolve_streams = None

logger = logging.getLogger(__name__)


@dataclass
class PodStreamConfig:
    """Configuration for a pod's LSL stream."""

    pod_id: str
    stream_name: str
    stream_type: str
    channel_count: int
    sampling_rate: float
    channel_names: list[str]
    channel_units: list[str]
    source_id: str
    tier: int


@dataclass
class GatewayConfig:
    """Configuration for LSL Gateway."""

    # Gateway identification
    gateway_id: str = "SYNAPSE_HUB"
    gateway_name: str = "SYNAPSE-24 Hub"

    # Sync configuration
    sync_config: SyncConfig = field(default_factory=SyncConfig)

    # Clock domain (LSL local_clock by default)
    use_lsl_clock: bool = True

    # Data buffering
    buffer_duration_s: float = 10.0  # Ring buffer for each stream
    chunk_size: int = 256

    # Marker stream
    marker_stream_name: str = "SYNAPSE_Markers"
    marker_source_id: str = ""

    # BLE/USB settings
    ble_adapter: str = "hci0"
    usb_vid_pid: list[tuple[int, int]] | None = None

    def __post_init__(self) -> None:
        if not self.marker_source_id:
            self.marker_source_id = f"synapse24_{self.gateway_id}_markers"


class LSLGateway:
    """Hub-side LSL gateway for multi-pod data aggregation and synchronization.

    Features:
    - Discovers and connects to pod LSL streams (BLE/USB/TCP)
    - Creates unified LSL outlets with tier-appropriate StreamInfo
    - Runs MultiPodClockSync with per-tier budget (T0=10ms/60s, T1=1ms/10s)
    - Broadcasts sync markers at tier-appropriate intervals
    - Provides corrected timestamps for all streams
    - Ring buffer for recent data access
    """

    def __init__(
        self,
        config: GatewayConfig | None = None,
        clock_fn: Callable[[], float] | None = None,
    ) -> None:
        self.config = config or GatewayConfig()
        self._clock: Callable[[], float] = clock_fn or (local_clock if local_clock else time.time)

        # Stream management
        self._lsl_manager = LSLStreamManager()
        self._inlets: dict[str, StreamInlet] = {}
        self._stream_configs: dict[str, PodStreamConfig] = {}

        # Clock synchronization
        self.clock_sync = MultiPodClockSync(self.config.sync_config, clock_fn=self._clock)

        # Ring buffers for recent data
        self._buffers: dict[str, deque] = {}
        self._timestamps: dict[str, deque] = {}
        self._buffer_maxlen = int(self.config.buffer_duration_s * 1000)  # max samples

        # Sync marker handling
        self._marker_outlet: StreamOutlet | None = None
        self._marker_inlet: StreamInlet | None = None
        self._sync_sequence = 0
        self._last_tier1_sync = 0.0
        self._last_tier0_sync = 0.0

        # State
        self._running = False
        self._tasks: list[asyncio.Task] = []

    # ==================== Stream Discovery & Registration ====================

    async def discover_pods(self, timeout: float = 5.0) -> list[dict[str, Any]]:
        """Discover available LSL streams from pods.

        Returns:
            List of stream info dicts with keys: name, type, channel_count, nominal_srate, source_id
        """
        if resolve_streams is None:
            raise RuntimeError("pylsl not available")

        logger.info("Discovering LSL streams (timeout=%.1fs)...", timeout)
        streams = resolve_streams(timeout)

        discovered = []
        for stream in streams:
            info = {
                "name": stream.name(),
                "type": stream.type(),
                "channel_count": stream.channel_count(),
                "nominal_srate": stream.nominal_srate(),
                "source_id": stream.source_id(),
            }
            discovered.append(info)
            logger.info(
                "Found stream: %s (%s, %d ch @ %.1f Hz)",
                info["name"],
                info["type"],
                info["channel_count"],
                info["nominal_srate"],
            )

        return discovered

    def register_pod_stream(self, pod_config: PodStreamConfig) -> StreamOutlet:
        """Register a pod stream and create LSL outlet.

        Args:
            pod_config: PodStreamConfig with stream metadata

        Returns:
            Created StreamOutlet for pushing data
        """
        if pod_config.pod_id in self._stream_configs:
            raise ValueError(f"Pod {pod_config.pod_id} already registered")

        # Create LSL StreamInfo
        stream_config = StreamConfig(
            name=pod_config.stream_name,
            stream_type=pod_config.stream_type,
            channel_count=pod_config.channel_count,
            sampling_rate=pod_config.sampling_rate,
            channel_format="float32",
            channel_names=pod_config.channel_names,
            channel_units=pod_config.channel_units,
            source_id=pod_config.source_id,
            tier=pod_config.tier,
            device="SYNAPSE-24",
            model=f"Pod-{pod_config.pod_id}",
        )

        outlet = self._lsl_manager.add_stream(pod_config.pod_id, stream_config)

        # Register with clock sync
        self.clock_sync.register_pod(pod_config.pod_id, acc_sampling_rate=100)

        # Initialize ring buffer
        maxlen = int(self.config.buffer_duration_s * pod_config.sampling_rate)
        self._buffers[pod_config.pod_id] = deque(maxlen=maxlen)
        self._timestamps[pod_config.pod_id] = deque(maxlen=maxlen)

        self._stream_configs[pod_config.pod_id] = pod_config

        logger.info(
            "Registered pod stream: %s (%s, %d ch @ %.1f Hz, tier=%d)",
            pod_config.stream_name,
            pod_config.stream_type,
            pod_config.channel_count,
            pod_config.sampling_rate,
            pod_config.tier,
        )

        return outlet

    def create_marker_outlet(self) -> StreamOutlet:
        """Create marker outlet for sync markers and events."""
        from pylsl import StreamInfo, StreamOutlet

        marker_config = StreamConfig(
            name=self.config.marker_stream_name,
            stream_type="Markers",
            channel_count=1,
            sampling_rate=0,  # Irregular
            channel_format="string",
            channel_names=["marker"],
            channel_units=[""],
            source_id=self.config.marker_source_id,
            tier=1,  # Markers at highest tier
        )

        info = create_stream_info(marker_config)
        self._marker_outlet = StreamOutlet(info, chunk_size=1, max_buffered=100)
        logger.info("Created marker outlet: %s", self.config.marker_stream_name)
        return self._marker_outlet

    # ==================== Gateway Lifecycle ====================

    async def start(self) -> None:
        """Start the gateway (connect inlets, start tasks)."""
        if self._running:
            logger.warning("Gateway already running")
            return

        if self._marker_outlet is None:
            self.create_marker_outlet()

        # Resolve inlets for registered pods
        if resolve_streams is None:
            raise RuntimeError("pylsl not available")

        for pod_id, stream_config in self._stream_configs.items():
            try:
                streams = resolve_streams(2.0)
                target_streams = [s for s in streams if s.source_id() == stream_config.source_id]
                if target_streams:
                    inlet = StreamInlet(
                        target_streams[0], max_buflen=360, max_chunklen=self.config.chunk_size
                    )
                    self._inlets[pod_id] = inlet
                    logger.info("Connected inlet for pod: %s", pod_id)
                else:
                    logger.warning(
                        "No stream found for pod %s (source_id=%s)", pod_id, stream_config.source_id
                    )
            except Exception:
                logger.exception("Failed to create inlet for pod %s", pod_id)

        self._running = True

        # Start background tasks
        self._tasks = [
            asyncio.create_task(self._data_pump_loop()),
            asyncio.create_task(self._sync_marker_loop()),
            asyncio.create_task(self._clock_sync_loop()),
        ]

        logger.info("LSL Gateway started with %d inlets", len(self._inlets))

    async def stop(self) -> None:
        """Stop the gateway."""
        self._running = False

        # Cancel tasks
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        # Close inlets
        for inlet in self._inlets.values():
            inlet.close_stream()
        self._inlets.clear()

        logger.info("LSL Gateway stopped")

    # ==================== Background Loops ====================

    async def _data_pump_loop(self) -> None:
        """Continuous data pump: pull from inlets, push to outlets with corrected timestamps."""
        while self._running:
            try:
                for pod_id, inlet in self._inlets.items():
                    if pod_id not in self._stream_configs:
                        continue

                    # Pull chunk
                    chunk, timestamps = inlet.pull_chunk(
                        timeout=0.0,
                        max_samples=self.config.chunk_size,
                    )

                    if not chunk:
                        continue

                    # Convert to numpy
                    data = np.array(chunk, dtype=np.float32)  # (n_samples, n_channels)
                    ts = np.array(timestamps, dtype=np.float64)

                    # Apply clock correction
                    corrected_ts = self.clock_sync.correct_pod_timestamps(pod_id, ts)

                    # Push to LSL outlet
                    self._lsl_manager.push_chunk(pod_id, data, corrected_ts)

                    # Update ring buffer
                    self._buffers[pod_id].extend(data)
                    self._timestamps[pod_id].extend(corrected_ts)

                    # Update clock sync with ACC data if available
                    stream_config = self._stream_configs[pod_id]
                    if stream_config.stream_type in ("ACC_T0", "ACC_T1", "ACC"):
                        # Compute ACC magnitude
                        if data.shape[1] >= 3:
                            acc_mag = np.linalg.norm(data[:, :3], axis=1)
                            self.clock_sync.add_pod_acc(
                                pod_id, float(np.mean(acc_mag)), float(corrected_ts[-1])
                            )

            except Exception:
                logger.exception("Data pump error")

            await asyncio.sleep(0.001)  # 1ms yield

    async def _sync_marker_loop(self) -> None:
        """Broadcast sync markers at tier-appropriate intervals."""
        while self._running:
            try:
                # Get current tier from acquisition controller (would be injected)
                # For now, use Tier 1 interval if any Tier 1 pod is streaming
                tier = (
                    Tier.T1
                    if any(sc.tier == 1 for sc in self._stream_configs.values())
                    else Tier.T0
                )

                current_time = self._clock()

                if tier == Tier.T1:
                    interval = self.config.sync_config.tier_budget.tier1_sync_interval_s
                    if current_time - self._last_tier1_sync >= interval:
                        self._broadcast_sync_marker(current_time)
                        self._last_tier1_sync = current_time
                else:
                    interval = self.config.sync_config.tier_budget.tier0_sync_interval_s
                    if current_time - self._last_tier0_sync >= interval:
                        self._broadcast_sync_marker(current_time)
                        self._last_tier0_sync = current_time

            except Exception:
                logger.exception("Sync marker loop error")

            await asyncio.sleep(1.0)  # Check every second

    def _broadcast_sync_marker(self, timestamp: float) -> SyncMarker:
        """Broadcast sync marker to all pods via LSL marker stream."""
        marker = SyncMarker(
            sequence=self._sync_sequence,
            hub_timestamp=timestamp,
            pod_timestamps={},
        )

        if self._marker_outlet:
            try:
                self._marker_outlet.push_sample([f"SYNC_{self._sync_sequence}"], timestamp)
            except Exception:
                logger.exception("Failed to push sync marker")

        # Also notify clock sync
        self.clock_sync.marker_manager.broadcast_sync(timestamp)
        self._sync_sequence += 1

        logger.debug("Broadcast sync marker %d at %.6f", marker.sequence, timestamp)
        return marker

    async def _clock_sync_loop(self) -> None:
        """Periodic clock sync update with drift estimation."""
        while self._running:
            try:
                self.clock_sync.update_drift_estimates()

                # Log sync status periodically
                status = self.clock_sync.get_sync_status()
                for pod_id, info in status["pods"].items():
                    if not info["within_tolerance"]:
                        logger.warning(
                            "Pod %s drift %.2f ms exceeds tolerance %.1f ms (tier=%s)",
                            pod_id,
                            info["offset_ms"],
                            info["tolerance_ms"],
                            info["tier_evaluated"],
                        )

            except Exception:
                logger.exception("Clock sync loop error")

            await asyncio.sleep(5.0)  # Update every 5 seconds

    # ==================== Data Access ====================

    def get_recent_data(
        self, pod_id: str, duration_s: float | None = None
    ) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]] | None:
        """Get recent data from ring buffer.

        Args:
            pod_id: Pod identifier
            duration_s: Duration of data to retrieve (None = all buffered)

        Returns:
            Tuple of (data, timestamps) or None if pod not found
        """
        if pod_id not in self._buffers:
            return None

        data = np.array(self._buffers[pod_id], dtype=np.float32)
        timestamps = np.array(self._timestamps[pod_id], dtype=np.float64)

        if duration_s is not None and len(timestamps) > 0:
            # Find samples within duration_s of latest timestamp
            latest = timestamps[-1]
            cutoff = latest - duration_s
            mask = timestamps >= cutoff
            data = data[mask]
            timestamps = timestamps[mask]

        return data, timestamps

    def get_all_recent_data(self, duration_s: float | None = None) -> dict[str, tuple]:
        """Get recent data from all pods."""
        result = {}
        for pod_id in self._buffers:
            recent = self.get_recent_data(pod_id, duration_s)
            if recent is not None:
                result[pod_id] = recent
        return result

    # ==================== Sync Status ====================

    def get_sync_status(self, tier: Tier | None = None) -> dict[str, Any]:
        """Get synchronization status for all pods."""
        if tier is None:
            # Determine from active streams
            tier = Tier.T1 if any(sc.tier == 1 for sc in self._stream_configs.values()) else Tier.T0

        return self.clock_sync.get_sync_status(tier=tier)

    def quantify_residual_drift(
        self, pod_id: str, hub_timestamps: npt.NDArray[np.float64]
    ) -> dict[str, float | str] | None:
        """Quantify residual drift for a specific pod after correction.

        Args:
            pod_id: Pod identifier
            hub_timestamps: Reference hub timestamps (same length as pod data)

        Returns:
            Drift quantification report or None if pod not found
        """
        if pod_id not in self._timestamps:
            return None

        pod_ts = np.array(self._timestamps[pod_id])
        if len(pod_ts) != len(hub_timestamps):
            # Align by time range
            return None

        from synapse24.acquisition.clock_sync import quantify_residual_drift

        return quantify_residual_drift(
            pod_ts, hub_timestamps, tier=Tier.T1, config=self.config.sync_config
        )

    # ==================== Tier Event Handling ====================

    def on_tier_change(self, event: TransitionEvent) -> None:
        """Handle tier change event from acquisition controller."""
        logger.info(
            "Gateway received tier change: %s -> %s (%s)",
            event.from_tier.name,
            event.to_tier.name,
            event.reason,
        )

        # Force immediate sync marker on tier change
        self._broadcast_sync_marker(self._clock())

        # Log new sync budget
        if event.to_tier == Tier.T1:
            logger.info("Tier 1 active: sync budget 1ms/10s")
        else:
            logger.info("Tier 0 active: sync budget 10ms/60s")

    # ==================== Context Manager ====================

    async def __aenter__(self) -> LSLGateway:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        await self.stop()


def create_pod_stream_config(
    pod_id: str,
    modality: str,
    channel_count: int,
    sampling_rate: float,
    channel_names: list[str] | None = None,
    channel_units: list[str] | None = None,
    tier: Tier = Tier.T1,
) -> PodStreamConfig:
    """Create a PodStreamConfig for a pod modality stream."""
    stream_type_map = {
        "eeg": "EEG",
        "acc": "ACC",
        "gyro": "GYRO",
        "mag": "MAG",
        "ppg": "PPG",
        "ecg": "ECG",
        "temp": "TEMP",
    }
    base_type = stream_type_map.get(modality.lower(), modality.upper())
    tier_suffix = f"_T{tier.value}"

    return PodStreamConfig(
        pod_id=pod_id,
        stream_name=f"SYNAPSE_{base_type}_{pod_id}",
        stream_type=f"{base_type}{tier_suffix}",
        channel_count=channel_count,
        sampling_rate=sampling_rate,
        channel_names=channel_names or [f"{base_type}{i + 1}" for i in range(channel_count)],
        channel_units=channel_units or ["µV"] * channel_count,
        source_id=f"synapse24_{base_type.lower()}_{pod_id}",
        tier=tier.value,
    )


__all__ = [
    "PodStreamConfig",
    "GatewayConfig",
    "LSLGateway",
    "create_pod_stream_config",
]
