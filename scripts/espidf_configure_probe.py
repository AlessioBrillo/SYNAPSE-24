#!/usr/bin/env python3
"""Configure the band firmware in the ESP-IDF image (no compile).

The component graph is decided at *configure* time: this is the fast way to
check that the feature gating resolves the same way the compiler sees it
(the header of a component that is not linked is a build error).

Usage:
    uv run python scripts/espidf_configure_probe.py [--variant core|full]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "espressif/idf:v5.5.5"
FIRMWARE = ROOT / "firmware"


def main() -> int:
    """Configure the band firmware inside the ESP-IDF image."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=["core", "full"], default="core")
    args = parser.parse_args()

    if args.variant == "core":
        script = (
            "set -e; rm -rf build; idf.py set-target esp32s3; "
            'SDKCONFIG_DEFAULTS="sdkconfig.defaults.core;sdkconfig.defaults" '
            "idf.py reconfigure"
        )
    else:
        script = "set -e; rm -rf build; idf.py set-target esp32s3; idf.py reconfigure"

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
    out = proc.stdout + proc.stderr
    for line in out.splitlines():
        if "synapse_features:" in line or "Error" in line or "error" in line.lower():
            print(line[:300])
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
