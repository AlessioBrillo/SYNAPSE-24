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
from .xdf_correction import (
    CorrectionResult,
    correct_xdf_from_sync,
    correct_xdf_timestamps,
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
    "CorrectionResult",
    "correct_xdf_timestamps",
    "correct_xdf_from_sync",
]
