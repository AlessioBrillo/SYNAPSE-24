# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""XDF 1.0 writer (chunk format) for the Synapse SDK.

This is the **canonical** XDF writer of the project. The host pipeline
(``src/synapse24/utils/xdf.py``) delegates to it, so there is exactly one
encoder for the format, and the files it produces are read back by
``pyxdf`` — a third-party parser — as a round-trip check.

The format is the XDF 1.0 *chunk* layout (the one LabRecorder writes)::

    "XDF:"                       magic
    [varlen length][u16 tag][u32 stream id?][payload]

with the tags ``FILEHEADER=1``, ``STREAMHEADER=2``, ``SAMPLES=3``,
``CLOCK=4`` and ``BOUNDARY=5``. A *samples* chunk carries, per sample, one
flag byte (0 = deduce the timestamp from the previous one) followed by the
eight-byte timestamp when the flag is set, then the raw channel values.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import numpy as np

__all__ = [
    "XDF_BOUNDARY_SIGNATURE",
    "XDF_CHUNK_TAGS",
    "XDF_FILEHEADER_XML",
    "XDF_MAGIC",
    "write_varlen_int",
    "write_xdf",
    "write_xdf_chunk",
]

XDF_MAGIC = b"XDF:"
XDF_CHUNK_TAGS = {
    "FILEHEADER": 1,
    "STREAMHEADER": 2,
    "SAMPLES": 3,
    "CLOCK": 4,
    "BOUNDARY": 5,
}

# XDF 1.0 boundary-chunk signature (enables forward-scan recovery).
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

# Samples per samples-chunk.
BLOCK_SIZE = 10000


def write_varlen_int(f: Any, value: int) -> None:
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


def write_xdf_chunk(f: Any, tag: int, data: bytes, stream_id: int | None = None) -> None:
    """Write a single XDF 1.0 chunk: ``[varlen len][u16 tag][u32 stream id?][data]``."""
    header = struct.pack("<H", tag)
    if stream_id is not None:
        header += struct.pack("<I", stream_id)
    write_varlen_int(f, len(header) + len(data))
    f.write(header)
    f.write(data)


def _escape_xml(value: str) -> str:
    """Escape the characters that are not allowed in XML text nodes."""
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _stream_header_xml(stream: dict[str, Any]) -> bytes:
    """Build the ``<info>`` XML of a stream header chunk.

    Besides the mandatory fields, the device, the acquisition (with the tier)
    and the free-form metadata are written when the caller provides them: a
    recording that does not say which pod and which tier produced it is
    much harder to interpret later.
    """
    channels = stream.get("channel_names") or []
    units = stream.get("channel_units") or []
    channel_format = _escape_xml(str(stream.get("channel_format", "float32")))
    parts = [
        '<?xml version="1.0"?>',
        "<info>",
        f"<name>{_escape_xml(str(stream['name']))}</name>",
        f"<type>{_escape_xml(str(stream.get('type', 'Other')))}</type>",
        f"<channel_count>{int(stream.get('channel_count', 1))}</channel_count>",
        f"<nominal_srate>{float(stream.get('sampling_rate', 0.0))}</nominal_srate>",
        f"<channel_format>{channel_format}</channel_format>",
        f"<source_id>{_escape_xml(str(stream.get('source_id', 'synapse24')))}</source_id>",
    ]
    if channels:
        parts.append("<channels>")
        for index, label in enumerate(channels):
            unit = units[index] if index < len(units) else ""
            parts.append("<channel>")
            parts.append(f"<label>{_escape_xml(str(label))}</label>")
            parts.append(f"<unit>{_escape_xml(str(unit))}</unit>")
            parts.append(f"<type>{_escape_xml(str(stream.get('type', 'Other')))}</type>")
            parts.append("</channel>")
        parts.append("</channels>")
    device = stream.get("device")
    if device:
        parts.append("<device>")
        for key, value in device.items():
            parts.append(f"<{key}>{_escape_xml(str(value))}</{key}>")
        parts.append("</device>")
    acquisition = stream.get("acquisition")
    if acquisition:
        parts.append("<acquisition>")
        for key, value in acquisition.items():
            parts.append(f"<{key}>{_escape_xml(str(value))}</{key}>")
        parts.append("</acquisition>")
    metadata = stream.get("metadata")
    if metadata:
        parts.append("<metadata>")
        for key, value in metadata.items():
            parts.append(f"<{key}>{_escape_xml(str(value))}</{key}>")
        parts.append("</metadata>")
    parts.append("</info>")
    return "".join(parts).encode("utf-8")


def write_xdf(
    output_path: Path | str,
    streams: list[dict[str, Any]],
    file_metadata: dict[str, Any] | None = None,
) -> None:
    """Write multiple streams to an XDF 1.0 file.

    Args:
        output_path: Destination ``.xdf`` file.
        streams: One dictionary per stream with the keys ``name``, ``type``,
            ``channel_count``, ``sampling_rate``, ``channel_format`` (optional),
            ``channel_names`` / ``channel_units`` (optional), ``source_id``
            (optional), ``header_xml`` (optional, verbatim stream header
            XML), ``data`` (``(n_samples, n_channels)``) and ``timestamps``
            (``(n_samples,)`` seconds).
        file_metadata: Optional file-level metadata (written in the file
            header chunk).

    """
    del file_metadata  # the XDF 1.0 file header carries only the version
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "wb") as f:
        f.write(XDF_MAGIC)
        write_xdf_chunk(f, XDF_CHUNK_TAGS["FILEHEADER"], XDF_FILEHEADER_XML)

        for stream_id, stream in enumerate(streams, start=1):
            data = np.asarray(stream["data"])
            timestamps = np.asarray(stream["timestamps"], dtype=np.float64)
            if data.ndim == 1:
                data = data.reshape(-1, 1)
            n_samples, n_channels = data.shape
            if len(timestamps) != n_samples:
                raise ValueError(
                    f"Timestamp count ({len(timestamps)}) != sample count ({n_samples})"
                )
            channel_count = int(stream.get("channel_count", n_channels))
            if channel_count != n_channels:
                raise ValueError(
                    f"Channel count mismatch: header={channel_count}, data={n_channels}"
                )

            header = dict(stream)
            header["channel_count"] = channel_count
            header_xml = stream.get("header_xml")
            if header_xml:
                payload = header_xml if isinstance(header_xml, bytes) else bytes(header_xml)
            else:
                payload = _stream_header_xml(header)
            write_xdf_chunk(f, XDF_CHUNK_TAGS["STREAMHEADER"], payload, stream_id)

            is_string = data.dtype.kind in ("U", "S", "O")
            for block_start in range(0, n_samples, BLOCK_SIZE):
                block_end = min(block_start + BLOCK_SIZE, n_samples)
                block_samples = data[block_start:block_end]
                block_timestamps = timestamps[block_start:block_end]

                samples = bytearray()
                write_varlen_int(samples, block_end - block_start)
                if is_string:
                    for i in range(block_end - block_start):
                        samples.extend(b"\x01")
                        samples.extend(struct.pack("<d", float(block_timestamps[i])))
                        for j in range(n_channels):
                            encoded = str(block_samples[i, j]).encode("utf-8")
                            write_varlen_int(samples, len(encoded))
                            samples.extend(encoded)
                else:
                    rows = np.ascontiguousarray(block_samples, dtype=np.float32)
                    for i in range(block_end - block_start):
                        samples.extend(b"\x01")
                        samples.extend(struct.pack("<d", float(block_timestamps[i])))
                        samples.extend(rows[i].tobytes())

                write_xdf_chunk(f, XDF_CHUNK_TAGS["SAMPLES"], bytes(samples), stream_id)

        write_xdf_chunk(f, XDF_CHUNK_TAGS["BOUNDARY"], XDF_BOUNDARY_SIGNATURE)
