"""Utility modules for SYNAPSE-24."""

from .xdf import (
    create_stream_info,
    generate_synthetic_timestamps,
    validate_xdf,
    write_xdf,
)

__all__ = [
    "create_stream_info",
    "write_xdf",
    "generate_synthetic_timestamps",
    "validate_xdf",
]
