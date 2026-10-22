#!/usr/bin/env python3
"""Run the test suite in the CI image (Linux), like the server does.

The local suite runs on Windows; a test that waits for something the CI
runner does not provide (the LSL multicast, an external tool) hangs there
and not here. This probe runs the same tests in the CI image.

Usage:
    uv run python scripts/ci_suite_probe.py [pytest args...]
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "ghcr.io/astral-sh/uv:python3.11-bookworm"

DEFAULT_ARGS = ["tests", "sdk/python/tests", "-q", "--no-cov"]


def main() -> int:
    """Run pytest inside the CI image with the repository mounted."""
    args = sys.argv[1:] or DEFAULT_ARGS
    script = (
        "set -e; "
        # The mounted tree carries the Windows venv: the container needs its own.
        "export UV_PROJECT_ENVIRONMENT=/tmp/venv; "
        "uv sync --dev --frozen; "
        f"uv run pytest {' '.join(args)}"
    )
    proc = subprocess.run(  # noqa: S603 - fixed image and script
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{ROOT}:/src",
            "-v",
            "ci-uv:/root/.cache/uv",
            "-w",
            "/src",
            IMAGE,
            "bash",
            "-c",
            script,
        ],
        check=False,
    )
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
