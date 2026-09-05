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
from typing import IO, TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from types import TracebackType

import numpy as np
import numpy.typing as npt
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
    channel_format: str = "float32"
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
        channel_format=config.channel_format,
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
    stream_type = d.get("type", "Other")
    # Markers/Metadata carry varlen strings on the wire — default their format
    # to "string" unless the caller overrides explicitly (a float32 header on
    # string payload misaligns every third-party XDF reader).
    default_format = "string" if stream_type in ("Markers", "Metadata") else "float32"
    config = StreamConfig(
        name=d.get("name", "SYNAPSE_STREAM"),
        stream_type=stream_type,
        channel_count=d.get("channel_count", 1),
        sampling_rate=d.get("sampling_rate", 0),
        channel_format=d.get("channel_format", default_format),
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
        self, stream_id: str, sample: npt.NDArray[np.float64], timestamp: float | None = None
    ) -> None:
        """Push a single sample to the outlet with LSL clock timestamp."""
        if stream_id not in self._outlets:
            raise KeyError(f"Stream {stream_id} not registered")

        if timestamp is None:
            timestamp = local_clock()

        self._outlets[stream_id].push_sample(sample.astype(np.float32), timestamp)

    def push_chunk(
        self,
        stream_id: str,
        data: npt.NDArray[np.float64],
        timestamps: npt.NDArray[np.float64] | None = None,
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

    def stream_all(
        self, streams_data: dict[str, tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]]
    ) -> None:
        """Stream all registered streams synchronously from pre-loaded data.

        Args:
            streams_data: Dict of stream_id -> (data, timestamps)
                data: (n_samples, n_channels) float32
                timestamps: (n_samples,) float64 in LSL clock domain
        """
        # Verify all streams have same number of samples (for synchronous streaming)
        n_samples: int = 0
        for stream_id, (data, timestamps) in streams_data.items():
            if stream_id not in self._outlets:
                raise KeyError(f"Stream {stream_id} not registered")
            if n_samples == 0:
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

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit context manager - outlets auto-cleanup on garbage collection."""


# XDF 1.0 boundary-chunk signature (enables forward-scan recovery on corruption).
XDF_BOUNDARY_SIGNATURE = bytes(
    [
        0x43,
        0xA5,
        0x46,
        0xDC,
        0xCB,
        0xF5,
        0x41,
        0x0F,
        0xB3,
        0x0E,
        0xD5,
        0x46,
        0x73,
        0x83,
        0xCB,
        0xE4,
    ]
)

XDF_FILEHEADER_XML = b"<info><version>1.0</version></info>"


def _write_varlen_int(f: IO[bytes] | bytearray, value: int) -> None:
    """Write an XDF variable-length integer (1/4/8-byte selector + value).

    Matches the encoding ``pyxdf._read_varlen_int`` expects: a single
    length-of-length byte (1, 4 or 8) followed by the little-endian value.
    """
    if value < 0:
        raise ValueError(f"XDF varlen int must be non-negative, got {value}")
    if value < 256:
        prefix, payload = b"\x01", struct.pack("B", value)
    elif value < 2**32:
        prefix, payload = b"\x04", struct.pack("<I", value)
    else:
        prefix, payload = b"\x08", struct.pack("<Q", value)
    if isinstance(f, bytearray):
        f.extend(prefix)
        f.extend(payload)
    else:
        f.write(prefix)
        f.write(payload)


def _write_xdf_chunk(f: IO[bytes], tag: int, data: bytes, stream_id: int | None = None) -> None:
    """Write a single XDF 1.0 chunk: [varlen len][u16 tag][u32 stream_id?][data]."""
    header = struct.pack("<H", tag)
    if stream_id is not None:
        header += struct.pack("<I", stream_id)
    _write_varlen_int(f, len(header) + len(data))
    f.write(header)
    f.write(data)


def _write_string(f: IO[bytes], s: str) -> None:
    """Write UTF-8 string with varlen length prefix."""
    encoded = s.encode("utf-8")
    _write_varlen_int(f, len(encoded))
    f.write(encoded)


def write_xdf(  # noqa: PLR0915
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
        # Magic bytes (not a chunk), then FILEHEADER chunk with XML payload.
        f.write(XDF_MAGIC)
        _write_xdf_chunk(f, XDF_CHUNK_TAGS["FILEHEADER"], XDF_FILEHEADER_XML)

        # Process each stream (1-based stream IDs per XDF 1.0)
        for stream_id, stream_dict in enumerate(streams, start=1):
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
                dict_type = info.get("type", "Other")
                dict_format = info.get(
                    "channel_format",
                    "string" if dict_type in ("Markers", "Metadata") else "float32",
                )
                config = StreamConfig(
                    name=info.get("name", "STREAM"),
                    stream_type=dict_type,
                    channel_count=info.get("channel_count", n_channels),
                    sampling_rate=info.get("sampling_rate", 0),
                    channel_format=dict_format,
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
            _write_xdf_chunk(f, XDF_CHUNK_TAGS["STREAMHEADER"], xml_bytes, stream_id)

            # SAMPLES chunk(s), one per block. XDF 1.0 layout per sample:
            # [u8 ts_flag][f64 ts if flag != 0][channel values]. Timestamps are
            # always written explicitly (no delta inference on the write path).
            block_size = 10000  # samples per block
            for block_start in range(0, n_samples, block_size):
                block_end = min(block_start + block_size, n_samples)
                block_samples = data[block_start:block_end]
                block_timestamps = np.asarray(timestamps[block_start:block_end], dtype=np.float64)

                samples_data = bytearray()
                _write_varlen_int(samples_data, block_end - block_start)

                # Check if data is string/object type (for markers, metadata)
                is_string_data = block_samples.dtype.kind in ("U", "S", "O")

                if is_string_data:
                    for i in range(block_end - block_start):
                        samples_data.extend(b"\x01")
                        samples_data.extend(struct.pack("<d", float(block_timestamps[i])))
                        for j in range(n_channels):
                            encoded = str(block_samples[i, j]).encode("utf-8")
                            _write_varlen_int(samples_data, len(encoded))
                            samples_data.extend(encoded)
                else:
                    rows = np.ascontiguousarray(block_samples, dtype=np.float32)
                    for i in range(block_end - block_start):
                        samples_data.extend(b"\x01")
                        samples_data.extend(struct.pack("<d", float(block_timestamps[i])))
                        samples_data.extend(rows[i].tobytes())

                _write_xdf_chunk(f, XDF_CHUNK_TAGS["SAMPLES"], bytes(samples_data), stream_id)

        # BOUNDARY chunk with spec signature (enables pyxdf forward-scan).
        _write_xdf_chunk(f, XDF_CHUNK_TAGS["BOUNDARY"], XDF_BOUNDARY_SIGNATURE)


def generate_synthetic_timestamps(
    n_samples: int,
    sampling_rate: float,
    start_time: float | None = None,
) -> npt.NDArray[np.float64]:
    """Generate regularly spaced timestamps in LSL clock domain."""
    if start_time is None:
        start_time = local_clock()
    return start_time + np.arange(n_samples, dtype=np.float64) / sampling_rate


def verify_xdf_roundtrip(
    streams: list[dict[str, Any]],
    output_path: Path,
) -> dict[str, Any]:
    """Write streams to XDF and verify every sample survives the round-trip.

    Two-node LSL/XDF proof for the Phase 0 exit gate (Roadmap.md §4, LSL/XDF
    from day one): any dropped packet fails loudly instead of biasing
    downstream fusion windows.

    Args:
        streams: List of dicts with keys ``name``, ``type``, ``data``
            ``(n_samples, n_channels)``, ``timestamps`` ``(n_samples,)``,
            ``sampling_rate``. Optional ``drop_last_n`` fault-injects a
            truncated write while still expecting the full sample count.
        output_path: Destination ``.xdf`` file.

    Returns:
        Dict with ``n_streams``, ``all_streams_valid``, ``total_expected``,
        ``total_recovered``, ``total_dropped`` and per-stream details.
    """
    output_path = Path(output_path)
    expected_counts: dict[str, int] = {}
    write_payload: list[dict[str, Any]] = []

    for spec in streams:
        data = np.asarray(spec["data"])
        timestamps = np.asarray(spec["timestamps"], dtype=np.float64)
        expected_counts[spec["name"]] = int(data.shape[0])

        drop_n = int(spec.get("drop_last_n", 0))
        if drop_n:
            data = data[: data.shape[0] - drop_n]
            timestamps = timestamps[: timestamps.shape[0] - drop_n]

        n_channels = data.shape[1] if data.ndim == 2 else 1
        is_string = data.dtype.kind in ("U", "S", "O")
        write_payload.append(
            {
                "data": data,
                "timestamps": timestamps,
                "info": StreamConfig(
                    name=spec["name"],
                    stream_type=spec.get("type", "Other"),
                    channel_count=n_channels,
                    sampling_rate=float(spec.get("sampling_rate", 0)),
                    channel_format=spec.get("channel_format", "string" if is_string else "float32"),
                ),
            }
        )

    write_xdf(output_path, write_payload)
    summary = validate_xdf(output_path)

    recovered_by_name = {s["name"]: int(s["n_samples"]) for s in summary["streams"]}
    per_stream = []
    total_expected = 0
    total_recovered = 0
    for name, expected in expected_counts.items():
        recovered = recovered_by_name.get(name, 0)
        dropped = max(expected - recovered, 0)
        total_expected += expected
        total_recovered += recovered
        per_stream.append(
            {
                "name": name,
                "expected": expected,
                "recovered": recovered,
                "dropped": dropped,
                "valid": dropped == 0 and name in recovered_by_name,
            }
        )

    all_valid = bool(
        summary["validation"]["all_streams_valid"]
        and len(summary["streams"]) == len(expected_counts)
        and all(s["valid"] for s in per_stream)
    )
    return {
        "file": str(output_path),
        "n_streams": len(per_stream),
        "all_streams_valid": all_valid,
        "total_expected": total_expected,
        "total_recovered": total_recovered,
        "total_dropped": total_expected - total_recovered,
        "per_stream": per_stream,
    }


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

        duration_s = float(time_stamps[-1] - time_stamps[0]) if len(time_stamps) > 1 else 0.0
        stream_summary = {
            "name": info.get("name", [""])[0],
            "type": info.get("type", [""])[0],
            "channel_count": info.get("channel_count", [0])[0],
            "nominal_srate": info.get("nominal_srate", [0])[0],
            "source_id": info.get("source_id", [""])[0],
            "n_samples": len(time_series),
            "duration_s": duration_s,
            "actual_srate": len(time_series) / duration_s if duration_s > 0 else 0.0,
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
        channel_format="string",
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
        channel_format="string",
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
