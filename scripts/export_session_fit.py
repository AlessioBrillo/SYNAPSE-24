#!/usr/bin/env python3
r"""Export a SYNAPSE-24 XDF session to a FIT file with Synapse developer data.

This script is a thin wrapper around :mod:`synapse24.export`, which owns the
canonical implementation: the analysis (which stream to analyse, R-peak
detection, per-window HRV) lives in the host pipeline, while the FIT bytes
are produced by the SDK's writer (:mod:`synapse_device.fit`) — one encoder
for the whole project.

The equivalent console script is ``synapse-export``.

Usage:
    uv run python scripts/export_session_fit.py session.xdf out.fit \\
        [--window-s SEC] [--device-id SERIAL]
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the repo packages importable when the script is run directly.
_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT / "src", _ROOT / "sdk" / "python"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from synapse24.export import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
