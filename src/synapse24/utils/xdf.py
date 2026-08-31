"""Utilities for XDF (Extensible Data Format) writing and LSL stream configuration."""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyxdf
from pylsl import StreamInfo, StreamOutlet, local_clock


def create_stream_info(
    name: str,
    stream_type: str,
    channel_count: int,
    sampling_rate: float,
    channel_names: list[str] | None = None,
    channel_units: list[str] | None = None,
    source_id: str | None = None,
) -> StreamInfo:
    """Create an LSL StreamInfo with proper metadata.

    Args:
        name: Stream name (e.g., "SYNAPSE_ECG")
        stream_type: LSL type (e.g., "ECG", "PPG", "EEG", "ACC", "Markers")
        channel_count: Number of channels
        sampling_rate: Sampling rate in Hz (0 for irregular)
        channel_names: Optional list of channel names
        channel_units: Optional list of channel units
        source_id: Unique source identifier

    Returns:
        Configured StreamInfo object
    """
    if source_id is None:
        source_id = f"synapse24_{name}_{uuid.uuid4().hex[:8]}"

    info = StreamInfo(
        name=name,
        type=stream_type,
        channel_count=channel_count,
        nominal_srate=sampling_rate,
        channel_format="float32",
        source_id=source_id,
    )

    # Add channel metadata
    chns = info.desc().append_child("channels")
    if channel_names is None:
        channel_names = [f"CH{i+1}" for i in range(channel_count)]
    if channel_units is None:
        channel_units = [""] * channel_count

    for ch_name, ch_unit in zip(channel_names, channel_units):
        ch = chns.append_child("channel")
        ch.append_child_value("label", ch_name)
        ch.append_child_value("unit", ch_unit)
        ch.append_child_value("type", stream_type)

    # Add device metadata
    device = info.desc().append_child("device")
    device.append_child_value("manufacturer", "SYNAPSE-24")
    device.append_child_value("model", "Phase0-Simulation")

    # Add acquisition metadata
    acq = info.desc().append_child("acquisition")
    acq.append_child_value("software", "synapse24.ingestion")
    acq.append_child_value("version", "0.1.0")

    return info


def write_xdf(
    output_path: Path,
    streams: list[dict[str, Any]],
    metadata: dict[str, Any] | None = None,
) -> None:
    """Write multiple streams to XDF file with LSL-compatible structure.

    Args:
        output_path: Path to output .xdf file
        streams: List of stream dictionaries with keys:
            - 'data': np.ndarray of shape (n_samples, n_channels)
            - 'timestamps': np.ndarray of shape (n_samples,) in seconds
            - 'info': StreamInfo object or dict with stream metadata
        metadata: Optional file-level metadata
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Convert StreamInfo to dict for pyxdf
    xdf_streams = []
    for stream in streams:
        info = stream["info"]
        if isinstance(info, StreamInfo):
            # Extract metadata from StreamInfo
            stream_dict = {
                "info": {
                    "name": [info.name()],
                    "type": [info.type()],
                    "channel_count": [info.channel_count()],
                    "nominal_srate": [info.nominal_srate()],
                    "channel_format": [info.channel_format()],
                    "source_id": [info.source_id()],
                    "created_at": [info.created_at()],
                    "uid": [info.uid()],
                    "desc": info.desc().child("channels").to_xml() if info.desc().child("channels") else "",
                },
                "time_series": stream["data"].astype(np.float32),
                "time_stamps": stream["timestamps"].astype(np.float64),
            }
        else:
            stream_dict = stream
        xdf_streams.append(stream_dict)

    # Add file-level metadata stream if provided
    if metadata:
        meta_stream = {
            "info": {
                "name": ["SYNAPSE_Metadata"],
                "type": ["Metadata"],
                "channel_count": [1],
                "nominal_srate": [0],
                "channel_format": ["string"],
                "source_id": [f"synapse24_meta_{uuid.uuid4().hex[:8]}"],
            },
            "time_series": np.array([[json.dumps(metadata)]], dtype=object),
            "time_stamps": np.array([local_clock()], dtype=np.float64),
        }
        xdf_streams.append(meta_stream)

    pyxdf.write_xdf(str(output_path), xdf_streams)


def generate_synthetic_timestamps(
    n_samples: int,
    sampling_rate: float,
    start_time: float | None = None,
) -> np.ndarray:
    """Generate regularly spaced timestamps."""
    if start_time is None:
        start_time = local_clock()
    return start_time + np.arange(n_samples) / sampling_rate


def validate_xdf(file_path: Path) -> dict[str, Any]:
    """Validate an XDF file and return stream summary."""
    streams, header = pyxdf.load_xdf(str(file_path))

    summary = {
        "file": str(file_path),
        "header": header,
        "n_streams": len(streams),
        "streams": [],
    }

    for stream in streams:
        info = stream["info"]
        time_series = stream["time_series"]
        time_stamps = stream["time_stamps"]

        stream_summary = {
            "name": info.get("name", [""])[0],
            "type": info.get("type", [""])[0],
            "channel_count": info.get("channel_count", [0])[0],
            "nominal_srate": info.get("nominal_srate", [0])[0],
            "source_id": info.get("source_id", [""])[0],
            "n_samples": len(time_series),
            "duration_s": float(time_stamps[-1] - time_stamps[0]) if len(time_stamps) > 1 else 0,
            "actual_srate": len(time_series) / (time_stamps[-1] - time_stamps[0]) if len(time_stamps) > 1 else 0,
        }
        summary["streams"].append(stream_summary)

    return summary


# Need json import for metadata stream
import json