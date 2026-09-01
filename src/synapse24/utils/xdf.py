"""Utilities for XDF (Extensible Data Format) writing and LSL stream configuration.

Implements full XDF 1.0 writing capability without LabRecorder dependency,
and provides multi-stream LSL synchronization for SYNAPSE-24 tiered acquisition.
"""

from __future__ import annotations

import json
import struct
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import numpy as np
import pyxdf
from pylsl import StreamInfo, StreamOutlet, local_clock

# XDF 1.0 format constants
XDF_MAGIC = b"XDF:"
XDF_VERSION = 1
XDF_CHUNK_TAGS = {
    "FILEHEADER": 1,
    "STREAMHEADER": 2,
    "SAMPLES": 3,
    "CLOCK": 4,
    "BOUNDARY": 5,
}


@dataclass(frozen=True)
class StreamConfig:
    """Immutable stream configuration for XDF export."""

    name: str
    stream_type: str
    channel_count: int
    sampling_rate: float
    channel_names: list[str] = field(default_factory=list)
    channel_units: list[str] = field(default_factory=list)
    source_id: str = ""
    tier: int = 0
    device: str = "SYNAPSE-24"
    model: str = "Phase0"
    software: str = "synapse24"
    version: str = "0.1.0"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Set default values for optional fields."""
        if not self.channel_names:
            object.__setattr__(
                self, "channel_names", [f"CH{i + 1}" for i in range(self.channel_count)]
            )
        if not self.channel_units:
            object.__setattr__(self, "channel_units", [""] * self.channel_count)
        if not self.source_id:
            object.__setattr__(self, "source_id", f"synapse24_{self.name}_{uuid.uuid4().hex[:8]}")


def create_stream_info(config: StreamConfig) -> StreamInfo:
    """Create an LSL StreamInfo from StreamConfig."""
    info = StreamInfo(
        name=config.name,
        type=config.stream_type,
        channel_count=config.channel_count,
        nominal_srate=config.sampling_rate,
        channel_format="float32",
        source_id=config.source_id,
    )

    # Channel metadata
    chns = info.desc().append_child("channels")
    for ch_name, ch_unit in zip(config.channel_names, config.channel_units):
        ch = chns.append_child("channel")
        ch.append_child_value("label", ch_name)
        ch.append_child_value("unit", ch_unit)
        ch.append_child_value("type", config.stream_type)

    # Device metadata
    device = info.desc().append_child("device")
    device.append_child_value("manufacturer", config.device)
    device.append_child_value("model", config.model)

    # Acquisition metadata
    acq = info.desc().append_child("acquisition")
    acq.append_child_value("software", config.software)
    acq.append_child_value("version", config.version)
    acq.append_child_value("tier", str(config.tier))

    # Custom metadata
    if config.metadata:
        meta = info.desc().append_child("metadata")
        for key, value in config.metadata.items():
            meta.append_child_value(key, str(value))

    return info


def create_stream_info_from_dict(d: dict[str, Any]) -> StreamInfo:
    """Create StreamInfo from dictionary (backward compatibility)."""
    config = StreamConfig(
        name=d.get("name", "SYNAPSE_STREAM"),
        stream_type=d.get("type", "Other"),
        channel_count=d.get("channel_count", 1),
        sampling_rate=d.get("sampling_rate", 0),
        channel_names=d.get("channel_names", []),
        channel_units=d.get("channel_units", []),
        source_id=d.get("source_id", ""),
        tier=d.get("tier", 0),
        device=d.get("device", "SYNAPSE-24"),
        model=d.get("model", "Phase0"),
        metadata=d.get("metadata", {}),
    )
    return create_stream_info(config)


class LSLStreamManager:
    """Manages multiple synchronized LSL outlets for real-time streaming."""

    def __init__(self) -> None:
        """Initialize empty stream manager."""
        self._outlets: dict[str, StreamOutlet] = {}
        self._configs: dict[str, StreamConfig] = {}

    def add_stream(self, stream_id: str, config: StreamConfig) -> StreamOutlet:
        """Register a stream and create its LSL outlet."""
        if stream_id in self._outlets:
            raise ValueError(f"Stream {stream_id} already registered")

        info = create_stream_info(config)
        outlet = StreamOutlet(info, chunk_size=32, max_buffered=360)
        self._outlets[stream_id] = outlet
        self._configs[stream_id] = config
        return outlet

    def push_sample(
        self, stream_id: str, sample: np.ndarray, timestamp: float | None = None
    ) -> None:
        """Push a single sample to the outlet with LSL clock timestamp."""
        if stream_id not in self._outlets:
            raise KeyError(f"Stream {stream_id} not registered")

        if timestamp is None:
            timestamp = local_clock()

        self._outlets[stream_id].push_sample(sample.astype(np.float32), timestamp)

    def push_chunk(
        self, stream_id: str, data: np.ndarray, timestamps: np.ndarray | None = None
    ) -> None:
        """Push a chunk of samples with timestamps."""
        if stream_id not in self._outlets:
            raise KeyError(f"Stream {stream_id} not registered")

        if timestamps is None:
            n_samples = data.shape[0]
            timestamps = (
                local_clock() + np.arange(n_samples) / self._configs[stream_id].sampling_rate
            )

        self._outlets[stream_id].push_chunk(data.astype(np.float32), timestamps.astype(np.float64))

    def stream_all(self, streams_data: dict[str, tuple[np.ndarray, np.ndarray]]) -> None:
        """Stream all registered streams synchronously from pre-loaded data.

        Args:
            streams_data: Dict of stream_id -> (data, timestamps)
                data: (n_samples, n_channels) float32
                timestamps: (n_samples,) float64 in LSL clock domain
        """
        # Verify all streams have same number of samples (for synchronous streaming)
        n_samples = None
        for stream_id, (data, timestamps) in streams_data.items():
            if stream_id not in self._outlets:
                raise KeyError(f"Stream {stream_id} not registered")
            if n_samples is None:
                n_samples = data.shape[0]
            elif data.shape[0] != n_samples:
                raise ValueError(
                    f"Stream {stream_id} has {data.shape[0]} samples, expected {n_samples}"
                )

        # Push in chunks for efficiency
        chunk_size = 256
        for i in range(0, n_samples, chunk_size):
            end = min(i + chunk_size, n_samples)
            for stream_id, (data, timestamps) in streams_data.items():
                self.push_chunk(stream_id, data[i:end], timestamps[i:end])

    def get_outlet(self, stream_id: str) -> StreamOutlet:
        return self._outlets[stream_id]

    def __enter__(self) -> Self:
        """Enter context manager."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit context manager - outlets auto-cleanup on garbage collection."""


def _write_xdf_chunk(f, tag: int, data: bytes) -> None:
    """Write a single XDF chunk with header."""
    f.write(struct.pack("<II", tag, len(data)))
    f.write(data)


def _write_varlen_int(f, value: int) -> None:
    """Write variable-length integer (XDF format) to file or bytearray."""
    if isinstance(f, bytearray):
        while value >= 0x80:
            f.extend(struct.pack("B", (value & 0x7F) | 0x80))
            value >>= 7
        f.extend(struct.pack("B", value & 0x7F))
    else:
        while value >= 0x80:
            f.write(struct.pack("B", (value & 0x7F) | 0x80))
            value >>= 7
        f.write(struct.pack("B", value & 0x7F))


def _write_string(f, s: str) -> None:
    """Write UTF-8 string with varlen length prefix."""
    encoded = s.encode("utf-8")
    _write_varlen_int(f, len(encoded))
    f.write(encoded)


def write_xdf(
    output_path: Path,
    streams: list[dict[str, Any]],
    file_metadata: dict[str, Any] | None = None,
) -> None:
    """Write multiple streams to XDF 1.0 file.

    Args:
        output_path: Path to output .xdf file
        streams: List of stream dictionaries with keys:
            - 'data': np.ndarray of shape (n_samples, n_channels) or (n_samples,)
            - 'timestamps': np.ndarray of shape (n_samples,) in seconds (LSL clock domain)
            - 'info': StreamInfo object, StreamConfig, or dict with stream metadata
        file_metadata: Optional file-level metadata
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "wb") as f:
        # FILEHEADER chunk
        header_data = bytearray()
        header_data.extend(XDF_MAGIC)
        header_data.extend(struct.pack("<I", XDF_VERSION))
        # Future: add file-level metadata here
        _write_xdf_chunk(f, XDF_CHUNK_TAGS["FILEHEADER"], bytes(header_data))

        # Process each stream
        for stream_dict in streams:
            data = stream_dict["data"]
            timestamps = stream_dict["timestamps"]
            info = stream_dict["info"]

            # Normalize data to 2D (n_samples, n_channels)
            if data.ndim == 1:
                data = data.reshape(-1, 1)
            n_samples, n_channels = data.shape

            # Validate timestamps
            if len(timestamps) != n_samples:
                raise ValueError(
                    f"Timestamp count ({len(timestamps)}) != sample count ({n_samples})"
                )
            if not np.all(np.diff(timestamps) >= 0):
                # Allow equal timestamps for irregular streams, but warn
                pass

            # Extract StreamConfig
            if isinstance(info, StreamConfig):
                config = info
            elif isinstance(info, StreamInfo):
                # Extract from StreamInfo (limited)
                config = StreamConfig(
                    name=info.name(),
                    stream_type=info.type(),
                    channel_count=info.channel_count(),
                    sampling_rate=info.nominal_srate(),
                    source_id=info.source_id(),
                )
            elif isinstance(info, dict):
                config = StreamConfig(
                    name=info.get("name", "STREAM"),
                    stream_type=info.get("type", "Other"),
                    channel_count=info.get("channel_count", n_channels),
                    sampling_rate=info.get("sampling_rate", 0),
                    channel_names=info.get("channel_names", []),
                    channel_units=info.get("channel_units", []),
                    source_id=info.get("source_id", ""),
                    tier=info.get("tier", 0),
                    metadata=info.get("metadata", {}),
                )
            else:
                raise TypeError(f"Unsupported info type: {type(info)}")

            # Validate channel count
            if config.channel_count != n_channels:
                raise ValueError(
                    f"Channel count mismatch: config={config.channel_count}, data={n_channels}"
                )

            # STREAMHEADER chunk (XML)
            stream_info = create_stream_info(config)
            xml_bytes = stream_info.as_xml().encode("utf-8")
            _write_xdf_chunk(f, XDF_CHUNK_TAGS["STREAMHEADER"], xml_bytes)

            # SAMPLES chunk(s) - write in blocks for memory efficiency
            block_size = 10000  # samples per block
            for block_start in range(0, n_samples, block_size):
                block_end = min(block_start + block_size, n_samples)
                block_samples = data[block_start:block_end]
                block_timestamps = timestamps[block_start:block_end]

                # Flatten samples in column-major order (XDF format)
                flat_samples = block_samples.T.ravel().astype(np.float32)

                samples_data = bytearray()
                # Sample count
                _write_varlen_int(samples_data, block_samples.shape[0])
                # Timestamps (double precision)
                samples_data.extend(block_timestamps.astype(np.float64).tobytes())
                # Sample values (float32)
                samples_data.extend(flat_samples.tobytes())

                _write_xdf_chunk(f, XDF_CHUNK_TAGS["SAMPLES"], bytes(samples_data))

        # BOUNDARY chunk (end of file)
        _write_xdf_chunk(f, XDF_CHUNK_TAGS["BOUNDARY"], b"")


def generate_synthetic_timestamps(
    n_samples: int,
    sampling_rate: float,
    start_time: float | None = None,
) -> np.ndarray:
    """Generate regularly spaced timestamps in LSL clock domain."""
    if start_time is None:
        start_time = local_clock()
    return start_time + np.arange(n_samples, dtype=np.float64) / sampling_rate


def validate_xdf(file_path: Path) -> dict[str, Any]:
    """Validate an XDF file and return stream summary."""
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"XDF file not found: {file_path}")

    streams, header = pyxdf.load_xdf(str(file_path))

    summary = {
        "file": str(file_path),
        "header": header,
        "n_streams": len(streams),
        "streams": [],
        "validation": {
            "timestamp_monotonic": True,
            "sample_count_match": True,
            "all_streams_valid": True,
        },
    }

    for stream in streams:
        info = stream["info"]
        time_series = stream["time_series"]
        time_stamps = stream["time_stamps"]

        # Check timestamp monotonicity
        is_monotonic = np.all(np.diff(time_stamps) >= -1e-9)  # Allow tiny numerical noise

        stream_summary = {
            "name": info.get("name", [""])[0],
            "type": info.get("type", [""])[0],
            "channel_count": info.get("channel_count", [0])[0],
            "nominal_srate": info.get("nominal_srate", [0])[0],
            "source_id": info.get("source_id", [""])[0],
            "n_samples": len(time_series),
            "duration_s": float(time_stamps[-1] - time_stamps[0]) if len(time_stamps) > 1 else 0,
            "actual_srate": len(time_series) / (time_stamps[-1] - time_stamps[0])
            if len(time_stamps) > 1
            else 0,
            "timestamp_monotonic": bool(is_monotonic),
        }

        if not is_monotonic:
            summary["validation"]["timestamp_monotonic"] = False

        if len(time_series) != len(time_stamps):
            summary["validation"]["sample_count_match"] = False

        summary["streams"].append(stream_summary)

    summary["validation"]["all_streams_valid"] = (
        summary["validation"]["timestamp_monotonic"] and summary["validation"]["sample_count_match"]
    )

    return summary


def create_quality_metadata_stream(
    quality_metrics: dict[str, Any],
    stream_name: str = "SYNAPSE_Metadata",
    source_id: str | None = None,
) -> dict[str, Any]:
    """Create a metadata stream containing signal quality metrics as JSON."""
    if source_id is None:
        source_id = f"synapse24_{stream_name}_{uuid.uuid4().hex[:8]}"

    config = StreamConfig(
        name=stream_name,
        stream_type="Metadata",
        channel_count=1,
        sampling_rate=0,  # Irregular
        channel_names=["quality_json"],
        channel_units=[""],
        source_id=source_id,
        tier=quality_metrics.get("tier", 0),
        metadata={"content": "signal_quality_metrics"},
    )

    return {
        "info": create_stream_info(config),
        "data": np.array([[json.dumps(quality_metrics, default=str)]], dtype=object),
        "timestamps": np.array([0.0], dtype=np.float64),
    }


def create_marker_stream(
    markers: list[tuple[float, str]],
    stream_name: str = "SYNAPSE_Markers",
    source_id: str | None = None,
) -> dict[str, Any]:
    """Create a marker stream from (timestamp, label) tuples."""
    if source_id is None:
        source_id = f"synapse24_{stream_name}_{uuid.uuid4().hex[:8]}"

    config = StreamConfig(
        name=stream_name,
        stream_type="Markers",
        channel_count=1,
        sampling_rate=0,  # Irregular
        channel_names=["marker"],
        channel_units=[""],
        source_id=source_id,
    )

    if markers:
        timestamps = np.array([m[0] for m in markers], dtype=np.float64)
        data = np.array([[m[1]] for m in markers], dtype=object)
    else:
        timestamps = np.array([0.0], dtype=np.float64)
        data = np.array([[""]], dtype=object)

    return {
        "info": create_stream_info(config),
        "data": data,
        "timestamps": timestamps,
    }
