#!/usr/bin/env python3
"""Compile the firmware host-gate sources with gcc, in the CI image.

The local host gate uses zig cc (clang): a source that only clang accepts
would pass locally and fail on the server, where gcc is the compiler. This
probe runs the same compile inside the CI image, so the difference is caught
before the push.

Usage:
    uv run python scripts/gcc_probe.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "ghcr.io/astral-sh/uv:python3.11-bookworm"

SOURCES = [
    (
        "firmware/esp32_band/fit/fit_writer.c",
        [
            "-Itests/firmware_host/stubs",
            "-Ifirmware/esp32_band/fit",
            "-Ifirmware/esp32_band/ble",
            '-DSYNAPSE_FIT_BASE_PATH=\\"/tmp/fit\\"',
            "-DCONFIG_SYNAPSE_FEATURE_FIT_STORAGE=1",
        ],
    ),
    (
        "firmware/common/ble/synapse_raw_frame.c",
        ["-Itests/firmware_host/stubs", "-Ifirmware/common/ble"],
    ),
    (
        "firmware/esp32_band/ble/ble_research_service.c",
        [
            "-Itests/firmware_host/stubs",
            "-Ifirmware/common/ble",
            "-Ifirmware/esp32_band/ble",
        ],
    ),
]


def main() -> int:
    """Compile every host-gate source with gcc inside the CI image."""
    script = "\n".join(
        [
            "set -e",
            *[
                f"gcc -std=c99 -O2 -Wall -Wextra -Werror {' '.join(flags)} "
                f"-c {source} -o /tmp/$(basename {source}).o"
                for source, flags in SOURCES
            ],
            "echo 'gcc: all the host-gate sources compile'",
        ]
    )
    proc = subprocess.run(  # noqa: S603 - fixed image and script
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{ROOT}:/src",
            "-w",
            "/src",
            IMAGE,
            "bash",
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
