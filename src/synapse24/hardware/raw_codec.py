"""SYNAPSE-24 raw sample frame codec (host side) — shim over the SDK.

Decision Q7: there is exactly ONE Python implementation of the raw frame
format, and it lives in the device SDK (``synapse_device.codec``, itself a
byte-for-byte mirror of ``firmware/common/ble/synapse_raw_frame.c`` verified
by ``tests/test_raw_frame_parity.py``). This module re-exports it so the
pipeline keeps its import paths. A re-export, not a copy: two codecs mean
two interpretations of the same bytes, which is how a dataset gets
silently corrupt while every test on the ASCII copy goes green.
"""

from __future__ import annotations

from synapse_device.codec import (
    FLAG_TS_EPOCH_WRAPPED,
    FRAME_VERSION,
    HEADER_SIZE,
    MAX_DROPPED,
    MAX_PAYLOAD,
    STREAM_CHANNELS,
    RawCodecError,
    RawFormat,
    RawHeader,
    RawStream,
    decode_frame,
    encode_frame,
    format_size,
    max_samples,
)

__all__ = [
    "FLAG_TS_EPOCH_WRAPPED",
    "FRAME_VERSION",
    "HEADER_SIZE",
    "MAX_DROPPED",
    "MAX_PAYLOAD",
    "RawCodecError",
    "RawFormat",
    "RawHeader",
    "RawStream",
    "STREAM_CHANNELS",
    "decode_frame",
    "encode_frame",
    "format_size",
    "max_samples",
]
