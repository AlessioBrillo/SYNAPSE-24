#!/usr/bin/env python3
"""Build the band firmware in the ESP-IDF image (the server's compiler).

The local host gate compiles the firmware sources with zig cc; the ESP-IDF
build is the one that proves the real target compiles (the generated contract
header, the Kconfig gating, the component requirements). The server runs it
on every push; this probe runs the same build locally, in the same image.

Usage:
    uv run python scripts/espidf_build_probe.py [--variant core|full]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "espressif/idf:v5.5.5"
# The whole firmware/ tree is mounted: the components include each other
# with relative paths (../../common/...).
FIRMWARE = ROOT / "firmware"


def main() -> int:
    """Build the band firmware (band-core by default) inside the ESP-IDF image."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=["core", "full"], default="core")
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=4,
        help="parallel compile jobs (fewer jobs = less memory: the Docker VM "
        "runs out of memory on this host with the default parallelism)",
    )
    args = parser.parse_args()

    if args.variant == "core":
        script = (
            "set -e; "
            "rm -rf build; "
            "idf.py set-target esp32s3; "
            f'export IDF_BUILD_PARALLEL_LEVEL={args.jobs}; '
            'SDKCONFIG_DEFAULTS="sdkconfig.defaults.core;sdkconfig.defaults" idf.py build'
        )
    else:
        script = (
            "set -e; rm -rf build; idf.py set-target esp32s3; "
            f"export IDF_BUILD_PARALLEL_LEVEL={args.jobs}; idf.py build"
        )

    proc = subprocess.run(  # noqa: S603 - fixed image and script
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{FIRMWARE}:/fw",
            "-w",
            "/fw/esp32_band",
            IMAGE,
            "bash",
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout[-8000:])
        sys.stderr.write(proc.stderr[-4000:])
    else:
        tail = "\n".join(proc.stdout.splitlines()[-25:])
        print(tail)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
