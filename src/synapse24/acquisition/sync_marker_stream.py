"""LSL Sync Marker Stream for SYNAPSE-24 multi-pod synchronization.

Provides a shared LSL Markers stream that all pods and hub connect to
for hardware sync marker broadcast.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

import numpy as np
import numpy.typing as npt
from pylsl import StreamInfo, StreamInlet, StreamOutlet, local_clock, resolve_byprop

from synapse24.utils.xdf import StreamConfig, create_stream_info


@dataclass
class SyncStreamConfig:
    """Configuration for sync marker stream."""

    stream_name: str = "SYNAPSE_Sync"
    stream_type: str = "Markers"
    source_id: str = ""
    sync_interval_s: float = 60.0
    marker_timeout_s: float = 5.0


class SyncMarkerStream:
    """Manages the shared LSL sync marker stream.

    Hub: Creates outlet, broadcasts SYNC markers
    Pods: Create inlet, listen for SYNC markers
    """

    def __init__(self, config: SyncStreamConfig | None = None) -> None:
        self.config = config or SyncStreamConfig()
        if not self.config.source_id:
            self.config.source_id = f"synapse24_sync_{uuid.uuid4().hex[:8]}"

        self._outlet: StreamOutlet | None = None
        self._inlet: StreamInlet | None = None
        self._is_hub = False
        self._callbacks: list[Callable[[str, float], None]] = []
        self._last_marker_time = 0.0
        self._sequence = 0

    def start_as_hub(self) -> None:
        """Initialize as hub (broadcaster)."""
        info = StreamInfo(
            name=self.config.stream_name,
            type=self.config.stream_type,
            channel_count=1,
            nominal_srate=0,  # Irregular
            channel_format="string",
            source_id=self.config.source_id,
        )
        # Add metadata
        chns = info.desc().append_child("channels")
        ch = chns.append_child("channel")
        ch.append_child_value("label", "sync_marker")
        ch.append_child_value("type", "marker")

        self._outlet = StreamOutlet(info, chunk_size=1, max_buffered=10)
        self._is_hub = True
        self._last_marker_time = local_clock()

    def start_as_pod(self, timeout: float = 5.0) -> bool:
        """Initialize as pod (listener). Resolves stream by source_id."""
        streams = resolve_byprop("source_id", self.config.source_id, timeout=timeout)
        if not streams:
            return False

        self._inlet = StreamInlet(streams[0], max_buflen=10, max_chunklen=1)
        self._is_hub = False
        return True

    def register_callback(self, callback: Callable[[str, float], None]) -> None:
        """Register callback for received markers: callback(marker_label, receipt_timestamp)."""
        self._callbacks.append(callback)

    def broadcast_sync(self, timestamp: float | None = None) -> str:
        """Broadcast a SYNC marker (hub only)."""
        if not self._is_hub or not self._outlet:
            raise RuntimeError("Not initialized as hub")

        if timestamp is None:
            timestamp = local_clock()

        marker_label = f"SYNC_{self._sequence:06d}"
        self._outlet.push_sample([marker_label], timestamp)
        self._sequence += 1
        self._last_marker_time = timestamp
        return marker_label

    def poll_markers(self, timeout: float = 0.0) -> list[tuple[str, float]]:
        """Poll for new markers (pod only). Returns list of (label, receipt_timestamp)."""
        if self._is_hub or not self._inlet:
            return []

        markers = []
        try:
            chunk, timestamps = self._inlet.pull_chunk(timeout=timeout, max_samples=10)
            for sample, ts in zip(chunk, timestamps):
                label = sample[0] if sample else ""
                if label.startswith("SYNC_"):
                    receipt_time = local_clock()
                    markers.append((label, receipt_time))
                    for cb in self._callbacks:
                        try:
                            cb(label, receipt_time)
                        except Exception:
                            pass
        except Exception:
            pass

        return markers

    def should_broadcast(self) -> bool:
        """Check if hub should broadcast a sync marker."""
        if not self._is_hub:
            return False
        return bool(local_clock() - self._last_marker_time >= self.config.sync_interval_s)

    def close(self) -> None:
        """Clean up resources."""
        if self._outlet:
            self._outlet = None
        if self._inlet:
            self._inlet.close_stream()
            self._inlet = None

    def __enter__(self) -> SyncMarkerStream:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()


class SyncMarkerRecorder:
    """Records sync marker timestamps for post-hoc drift analysis.

    Each pod records (marker_sequence, hub_timestamp_from_marker, pod_local_clock_at_receipt)
    Hub records (marker_sequence, hub_timestamp_at_broadcast)
    """

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []

    def record_hub_broadcast(self, sequence: int, hub_timestamp: float) -> None:
        """Record hub broadcast timestamp."""
        self._records.append({
            "sequence": sequence,
            "hub_timestamp": hub_timestamp,
            "pod_timestamp": None,
            "pod_id": "hub",
        })

    def record_pod_receipt(
        self, sequence: int, pod_id: str, pod_timestamp: float, hub_timestamp_from_marker: float | None = None
    ) -> None:
        """Record pod receipt timestamp."""
        self._records.append({
            "sequence": sequence,
            "hub_timestamp": hub_timestamp_from_marker,
            "pod_timestamp": pod_timestamp,
            "pod_id": pod_id,
        })

    def get_pod_records(self, pod_id: str) -> list[dict[str, Any]]:
        """Get all records for a specific pod."""
        return [r for r in self._records if r["pod_id"] == pod_id]

    def get_all_records(self) -> list[dict[str, Any]]:
        """Get all records."""
        return self._records.copy()

    def clear(self) -> None:
        """Clear all records."""
        self._records.clear()


def create_sync_stream_info(config: SyncStreamConfig | None = None) -> StreamInfo:
    """Create LSL StreamInfo for sync marker stream."""
    cfg = config or SyncStreamConfig()
    stream_config = StreamConfig(
        name=cfg.stream_name,
        stream_type=cfg.stream_type,
        channel_count=1,
        sampling_rate=0,
        channel_names=["sync_marker"],
        channel_units=[""],
        source_id=cfg.source_id,
    )
    return create_stream_info(stream_config)
