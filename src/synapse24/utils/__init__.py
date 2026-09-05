"""Utility modules for SYNAPSE-24."""

from .xdf import (
    LSLStreamManager,
    StreamConfig,
    create_marker_stream,
    create_quality_metadata_stream,
    create_stream_info,
    create_stream_info_from_dict,
    generate_synthetic_timestamps,
    validate_xdf,
    verify_xdf_roundtrip,
    write_xdf,
)

__all__ = [
    "StreamConfig",
    "create_stream_info",
    "create_stream_info_from_dict",
    "generate_synthetic_timestamps",
    "validate_xdf",
    "verify_xdf_roundtrip",
    "write_xdf",
    "LSLStreamManager",
    "create_quality_metadata_stream",
    "create_marker_stream",
]
