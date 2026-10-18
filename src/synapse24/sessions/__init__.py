"""SYNAPSE-24 session catalog (read-side of the Averyn data plane).

Scans a directory of stored XDF / FIT sessions and builds an idempotent,
JSON-serializable index with stable session ids, device serials, time
windows, per-stream metadata and validation outcomes. No hardware dependency.

Public API:
- index_directory(root) -> SessionCatalog
- index_xdf(path) / index_fit(path) -> SessionRecord
- save_catalog(catalog, output) / load_catalog(output) -> SessionCatalog
- main() -> CLI (`python -m synapse24.sessions index|list|show`)
"""

from __future__ import annotations

from .catalog import (
    SessionCatalog,
    SessionRecord,
    StreamInfo,
    index_directory,
    index_fit,
    index_xdf,
    load_catalog,
    main,
    save_catalog,
)

__all__ = [
    "SessionCatalog",
    "SessionRecord",
    "StreamInfo",
    "index_directory",
    "index_fit",
    "index_xdf",
    "load_catalog",
    "main",
    "save_catalog",
]
