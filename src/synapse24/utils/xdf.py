"""Utilities for XDF (Extensible Data Format) writing and LSL stream configuration."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import numpy as np
import pyxdf
from pylsl import StreamInfo, local_clock

if TYPE_CHECKING:
    from pathlib import Path


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
    """Write multiple streams to XDF file using pylsl's recording capability.

    Note: pyxdf only provides reading. For writing, we use pylsl's
    StreamOutlet to stream data and record via LabRecorder, or we can
    write the XDF format manually. For now, this function creates a
    minimal XDF-compatible structure using pylsl.

    Args:
        output_path: Path to output .xdf file
        streams: List of stream dictionaries with keys:
            - 'data': np.ndarray of shape (n_samples, n_channels)
            - 'timestamps': np.ndarray of shape (n_samples,) in seconds
            - 'info': StreamInfo object or dict with stream metadata
        metadata: Optional file-level metadata
    """
    # Since pyxdf doesn't support writing, we'll write a simple
    # XDF-like file using the xdf library structure directly.
    # This is a placeholder for full XDF writing capability.
    raise NotImplementedError(
        "XDF writing requires LabRecorder or manual XDF format implementation. "
        "Use pylsl StreamOutlet + LabRecorder for recording."
    )


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
