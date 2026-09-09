"""XDF timestamp correction post-processor for multi-pod clock drift compensation.

Applies per-stream DriftEstimate corrections to XDF files after acquisition,
preserving all stream metadata, channel formats, and chunk structure.
"""

from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np
import numpy.typing as npt
import pyxdf
from pylsl import StreamInfo

from synapse24.acquisition.clock_sync import DriftEstimate
from synapse24.utils.xdf import (
    XDF_BOUNDARY_SIGNATURE,
    XDF_CHUNK_TAGS,
    XDF_FILEHEADER_XML,
    XDF_MAGIC,
    StreamConfig,
    _channel_format_from_xml,
    _write_varlen_int,
    _write_xdf_chunk,
    create_stream_info,
    validate_xdf,
    verify_xdf_roundtrip,
)


@dataclass(frozen=True)
class CorrectionResult:
    """Result of XDF timestamp correction."""

    input_path: Path
    output_path: Path
    streams_corrected: int
    total_samples: int
    corrections_applied: dict[str, DriftEstimate]
    validation: dict[str, Any]


def _read_varlen_int(f: BinaryIO) -> int:
    """Read XDF variable-length integer (matches pyxdf._read_varlen_int)."""
    first_byte = f.read(1)
    if not first_byte:
        raise EOFError("Unexpected EOF reading varlen int")
    length = first_byte[0]
    if length == 1:
        return int(struct.unpack("B", f.read(1))[0])
    if length == 4:
        return int(struct.unpack("<I", f.read(4))[0])
    if length == 8:
        return int(struct.unpack("<Q", f.read(8))[0])
    raise ValueError(f"Invalid varlen prefix: {length}")


def _read_xdf_chunk(f: BinaryIO) -> tuple[int, int | None, bytes] | None:
    """Read a single XDF chunk: [varlen len][u16 tag][u32 stream_id?][data]."""
    try:
        chunk_len = _read_varlen_int(f)
    except EOFError:
        return None

    if chunk_len < 2:
        raise ValueError(f"Chunk too small: {chunk_len} bytes")

    header = f.read(2)
    if len(header) < 2:
        raise EOFError("Unexpected EOF reading chunk tag")
    tag = struct.unpack("<H", header)[0]

    stream_id = None
    data_len = chunk_len - 2
    if tag in (XDF_CHUNK_TAGS["STREAMHEADER"], XDF_CHUNK_TAGS["SAMPLES"], XDF_CHUNK_TAGS["CLOCK"]):
        stream_id_bytes = f.read(4)
        if len(stream_id_bytes) < 4:
            raise EOFError("Unexpected EOF reading stream_id")
        stream_id = struct.unpack("<I", stream_id_bytes)[0]
        data_len -= 4

    data = f.read(data_len)
    if len(data) < data_len:
        raise EOFError("Unexpected EOF reading chunk data")

    return tag, stream_id, data


def _parse_stream_header_xml(xml_bytes: bytes) -> dict[str, Any]:
    """Extract key fields from StreamInfo XML for reconstruction."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml_bytes)
    info: dict[str, Any] = {}

    def get_text(elem: ET.Element | None, path: str, default: str = "") -> str:
        if elem is None:
            return default
        child = elem.find(path)
        return child.text if child is not None and child.text else default

    info["name"] = get_text(root, "name")
    info["type"] = get_text(root, "type")
    info["channel_count"] = int(get_text(root, "channel_count", "1"))
    info["nominal_srate"] = float(get_text(root, "nominal_srate", "0"))
    info["channel_format"] = get_text(root, "channel_format", "float32")
    info["source_id"] = get_text(root, "source_id")

    # Channel names/units
    ch_names: list[str] = []
    ch_units: list[str] = []
    chns = root.find("channels")
    if chns is not None:
        for ch in chns.findall("channel"):
            ch_names.append(get_text(ch, "label", ""))
            ch_units.append(get_text(ch, "unit", ""))
    info["channel_names"] = ch_names
    info["channel_units"] = ch_units

    # Device/acquisition metadata
    device = root.find("device")
    if device is not None:
        info["device"] = get_text(device, "manufacturer", "")
        info["model"] = get_text(device, "model", "")
    acq = root.find("acquisition")
    if acq is not None:
        info["software"] = get_text(acq, "software", "")
        info["version"] = get_text(acq, "version", "")
        info["tier"] = int(get_text(acq, "tier", "0"))

    return info


def _correct_timestamps(
    timestamps: npt.NDArray[np.float64],
    correction: DriftEstimate,
) -> npt.NDArray[np.float64]:
    """Apply drift correction to timestamps.

    Corrected = (raw - offset_s) / (1 + drift_rate)
    where offset_s = offset_ms / 1000, drift_rate = drift_rate_ppm / 1e6
    """
    offset_s = correction.offset_ms / 1000.0
    drift_rate = correction.drift_rate_ppm / 1_000_000.0

    # (t - offset) / (1 + drift) ≈ t * (1 - drift) - offset for small drift
    return (timestamps - offset_s) / (1.0 + drift_rate)


def _process_samples_chunk(
    data: bytes,
    n_channels: int,
    channel_format: str,
    correction: DriftEstimate | None,
) -> bytes:
    """Process SAMPLES chunk: correct timestamps if correction provided."""
    if correction is None:
        return data

    # Parse samples: [varlen n_samples][sample1][sample2]...
    # Each sample: [u8 ts_flag][f64 ts][channel_values...]
    pos = 0
    n_samples = _read_varlen_int_from_bytes(data, pos)
    pos += _varlen_size(n_samples)

    output = bytearray()
    _write_varlen_int_to_bytes(output, n_samples)

    for _ in range(n_samples):
        if pos >= len(data):
            break
        ts_flag = data[pos]
        pos += 1

        if ts_flag != 0:
            if pos + 8 > len(data):
                break
            timestamp = struct.unpack("<d", data[pos : pos + 8])[0]
            pos += 8

            # Apply correction
            corrected_ts = _correct_timestamps(np.array([timestamp]), correction)[0]

            output.extend(bytes([ts_flag]))
            output.extend(struct.pack("<d", corrected_ts))
        else:
            output.extend(bytes([ts_flag]))

        # Copy channel data
        if channel_format in ("string", "string_utf8"):
            for _ in range(n_channels):
                if pos >= len(data):
                    break
                str_len = _read_varlen_int_from_bytes(data, pos)
                pos += _varlen_size(str_len)
                if pos + str_len > len(data):
                    break
                str_data = data[pos : pos + str_len]
                pos += str_len
                _write_varlen_int_to_bytes(output, str_len)
                output.extend(str_data)
        else:
            # float32: 4 bytes per channel
            sample_bytes = 4 * n_channels
            if pos + sample_bytes > len(data):
                break
            output.extend(data[pos : pos + sample_bytes])
            pos += sample_bytes

    return bytes(output)


def _read_varlen_int_from_bytes(data: bytes, offset: int) -> int:
    """Read varlen int from bytes at offset."""
    if offset >= len(data):
        return 0
    length = data[offset]
    if length == 1:
        return int(struct.unpack("B", data[offset + 1 : offset + 2])[0])
    if length == 4:
        return int(struct.unpack("<I", data[offset + 1 : offset + 5])[0])
    if length == 8:
        return int(struct.unpack("<Q", data[offset + 1 : offset + 9])[0])
    return 0


def _varlen_size(value: int) -> int:
    """Get byte size of varlen encoding for a value."""
    if value < 256:
        return 2  # 1 byte prefix + 1 byte value
    if value < 2**32:
        return 5  # 1 byte prefix + 4 bytes value
    return 9  # 1 byte prefix + 8 bytes value


def _write_varlen_int_to_bytes(buf: bytearray, value: int) -> None:
    """Write varlen int to bytearray."""
    if value < 0:
        raise ValueError(f"Varlen int must be non-negative, got {value}")
    if value < 256:
        buf.extend(b"\x01")
        buf.extend(struct.pack("B", value))
    elif value < 2**32:
        buf.extend(b"\x04")
        buf.extend(struct.pack("<I", value))
    else:
        buf.extend(b"\x08")
        buf.extend(struct.pack("<Q", value))


def _collect_chunks_and_configs(
    fin: BinaryIO, corrections: dict[str, DriftEstimate]
) -> tuple[
    list[tuple[int, int | None, bytes]],
    dict[int, StreamConfig],
    dict[int, DriftEstimate],
    dict[str, DriftEstimate],
    int,
]:
    """First pass: read all chunks, collect stream headers and corrections."""
    chunks = []
    stream_configs: dict[int, StreamConfig] = {}
    stream_corrections: dict[int, DriftEstimate] = {}
    corrections_applied = {}
    streams_corrected = 0

    while True:
        chunk = _read_xdf_chunk(fin)
        if chunk is None:
            break
        tag, stream_id, data = chunk
        chunks.append((tag, stream_id, data))

        if tag == XDF_CHUNK_TAGS["STREAMHEADER"] and stream_id is not None:
            info = _parse_stream_header_xml(data)
            source_id = info.get("source_id", "")
            correction = corrections.get(source_id)

            if correction:
                stream_corrections[stream_id] = correction
                corrections_applied[source_id] = correction
                streams_corrected += 1

            config = StreamConfig(
                name=info.get("name", f"STREAM_{stream_id}"),
                stream_type=info.get("type", "Other"),
                channel_count=info.get("channel_count", 1),
                sampling_rate=info.get("nominal_srate", 0),
                channel_format=info.get("channel_format", "float32"),
                channel_names=info.get("channel_names", []),
                channel_units=info.get("channel_units", []),
                source_id=source_id,
                tier=info.get("tier", 0),
                device=info.get("device", "SYNAPSE-24"),
                model=info.get("model", "Phase0"),
                metadata={"corrected": bool(correction)},
            )
            stream_configs[stream_id] = config

    return chunks, stream_configs, stream_corrections, corrections_applied, streams_corrected


def _write_corrected_chunks(
    fout: BinaryIO,
    chunks: list[tuple[int, int | None, bytes]],
    stream_configs: dict[int, StreamConfig],
    stream_corrections: dict[int, DriftEstimate],
) -> int:
    """Second pass: write chunks with timestamp corrections applied."""
    total_samples = 0

    for tag, stream_id, data in chunks:
        if tag == XDF_CHUNK_TAGS["STREAMHEADER"]:
            if stream_id is not None:
                config = stream_configs.get(stream_id)
                if config:
                    info = create_stream_info(config)
                    xml_bytes = info.as_xml().encode("utf-8")
                    _write_xdf_chunk(fout, tag, xml_bytes, stream_id)
                else:
                    _write_xdf_chunk(fout, tag, data, stream_id)
            else:
                _write_xdf_chunk(fout, tag, data, stream_id)

        elif tag == XDF_CHUNK_TAGS["SAMPLES"] and stream_id is not None:
            # stream_id is guaranteed to be int here due to the is not None check
            sid: int = stream_id
            config = stream_configs.get(sid)
            correction = stream_corrections.get(sid)

            if config and correction:
                corrected_data = _process_samples_chunk(
                    data,
                    config.channel_count,
                    config.channel_format,
                    correction,
                )
                _write_xdf_chunk(fout, tag, corrected_data, stream_id)
                total_samples += _read_varlen_int_from_bytes(data, 0)
            else:
                _write_xdf_chunk(fout, tag, data, stream_id)

        else:
            _write_xdf_chunk(fout, tag, data, stream_id)

    return total_samples


def correct_xdf_timestamps(
    input_path: Path,
    output_path: Path,
    corrections: dict[str, DriftEstimate],
    *,
    validate_output: bool = True,
) -> CorrectionResult:
    """Apply per-stream timestamp correction to XDF file.

    Args:
        input_path: Source XDF file
        output_path: Destination XDF file (will be created)
        corrections: Mapping of stream source_id -> DriftEstimate
        validate_output: If True, run validate_xdf on output

    Returns:
        CorrectionResult with statistics and validation info
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not input_path.exists():
        raise FileNotFoundError(f"Input XDF not found: {input_path}")

    with open(input_path, "rb") as fin, open(output_path, "wb") as fout:
        # Copy magic bytes
        magic = fin.read(len(XDF_MAGIC))
        if magic != XDF_MAGIC:
            raise ValueError(f"Invalid XDF magic: {magic!r}")
        fout.write(magic)

        # First pass: collect chunks and configs
        chunks, stream_configs, stream_corrections, corrections_applied, streams_corrected = (
            _collect_chunks_and_configs(fin, corrections)
        )

        # Write FILEHEADER
        _write_xdf_chunk(fout, XDF_CHUNK_TAGS["FILEHEADER"], XDF_FILEHEADER_XML)

        # Second pass: write corrected chunks
        total_samples = _write_corrected_chunks(fout, chunks, stream_configs, stream_corrections)

        # Write BOUNDARY chunk
        _write_xdf_chunk(fout, XDF_CHUNK_TAGS["BOUNDARY"], XDF_BOUNDARY_SIGNATURE)

    # Validate output
    validation = {}
    if validate_output:
        validation = validate_xdf(output_path)

    return CorrectionResult(
        input_path=input_path,
        output_path=output_path,
        streams_corrected=streams_corrected,
        total_samples=total_samples,
        corrections_applied=corrections_applied,
        validation=validation,
    )


def correct_xdf_from_sync(
    input_path: Path,
    output_path: Path,
    sync_estimates: dict[str, dict[str, Any]],
    *,
    validate_output: bool = True,
) -> CorrectionResult:
    """Convenience wrapper using MultiPodClockSync estimate format.

    Args:
        input_path: Source XDF file
        output_path: Destination XDF file
        sync_estimates: Output from MultiPodClockSync.get_sync_status()["pods"]
        validate_output: If True, run validate_xdf on output
    """
    # Extract DriftEstimate objects from sync status
    corrections = {}
    for pod_id, status in sync_estimates.items():
        if "offset_ms" in status and "drift_rate_ppm" in status:
            corrections[pod_id] = DriftEstimate(
                pod_id=pod_id,
                offset_ms=status["offset_ms"],
                drift_rate_ppm=status["drift_rate_ppm"],
                confidence=status.get("confidence", 1.0),
                method=status.get("method", "marker"),
            )

    return correct_xdf_timestamps(
        input_path, output_path, corrections, validate_output=validate_output
    )
