"""CLI entry point: ``python -m synapse24.sessions index|list|show``."""

from __future__ import annotations

import sys

from .catalog import main

if __name__ == "__main__":
    sys.exit(main())
