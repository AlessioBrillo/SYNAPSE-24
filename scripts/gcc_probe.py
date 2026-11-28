#!/usr/bin/env python3
"""Compile the firmware host-gate sources with gcc, in the CI image.

The local host gate uses zig cc (clang): a source that only clang accepts
would pass locally and fail on the server, where gcc is the compiler. This
probe runs the same compile inside the CI image, so the difference is caught
before the push. The source list mirrors the translation units that
``tests/firmware_host.build`` compiles in the host-C gate (including the
onboarding firmware: calibration, factory CLI, power monitor and I2C driver).

Usage:
    uv run python scripts/gcc_probe.py
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "ghcr.io/astral-sh/uv:python3.11-bookworm"

STUBS = "-Itests/firmware_host/stubs"
COMMON_SENSORS = "-Ifirmware/common/sensors"
BAND_CONFIG = "-Ifirmware/esp32_band/config"


@dataclass(frozen=True)
class Source:
    """One firmware translation unit and the include/define flags it needs."""

    path: str
    flags: tuple[str, ...] = ()
    cpp: bool = False


SOURCES = [
    Source(
        "firmware/esp32_band/fit/fit_writer.c",
        (
            STUBS,
            "-Ifirmware/esp32_band/fit",
            "-Ifirmware/esp32_band/ble",
            '-DSYNAPSE_FIT_BASE_PATH=\\"/tmp/fit\\"',
            "-DCONFIG_SYNAPSE_FEATURE_FIT_STORAGE=1",
        ),
    ),
    Source("firmware/common/ble/synapse_raw_frame.c", (STUBS, "-Ifirmware/common/ble")),
    Source("firmware/esp32_band/ble/synapse_consumer_v2.c", ("-Ifirmware/esp32_band/ble",)),
    Source(
        "firmware/esp32_band/signal_quality/synapse_rr.c",
        ("-Ifirmware/esp32_band/signal_quality",),
    ),
    Source(
        "firmware/esp32_band/ble/ble_research_service.c",
        (
            STUBS,
            "-Ifirmware/common/ble",
            "-Ifirmware/esp32_band/ble",
        ),
    ),
    # Host-C gate: C <-> Python parity of the 26-feature triage extractor.
    Source("firmware/common/triage/triage_dsp.c", ("-Ifirmware/common/triage",)),
    # Host-C gate: PPG/IMU DSP at the contract's 50 Hz.
    Source("firmware/common/sensors/ppg_dsp.c", (COMMON_SENSORS,)),
    # Host-C gate: PPG SQI (C++ translation unit, compiled by g++).
    Source(
        "firmware/common/sensors/ppg_sqi.cpp",
        (STUBS, COMMON_SENSORS),
        cpp=True,
    ),
    # Host-C gate: calibration record + factory CLI, compiled with the same
    # ESP-IDF stubs as tests/test_firmware_calibration_host.py.
    Source(
        "firmware/esp32_band/config/synapse_calibration.c",
        (
            STUBS,
            BAND_CONFIG,
            "-Ifirmware/esp32_band/main",
        ),
    ),
    Source(
        "firmware/esp32_band/main/synapse_factory_cli.c",
        (
            STUBS,
            BAND_CONFIG,
            "-Ifirmware/esp32_band/main",
        ),
    ),
    # Host-C gate: INA219 power monitor and the shared I2C driver it calls.
    Source(
        "firmware/esp32_band/sensors/ina219.c",
        (
            STUBS,
            "-Ifirmware/esp32_band/sensors",
            COMMON_SENSORS,
            "-Ifirmware/esp32_band/ble",
            "-Ifirmware/esp32_band/power",
            BAND_CONFIG,
        ),
    ),
    Source(
        "firmware/common/sensors/synapse_i2c.c",
        (STUBS, COMMON_SENSORS),
    ),
    # Compiled per-unit configuration JSON parser gate.
    Source(
        "tests/firmware_host/cfg_json_helpers.c",
        (STUBS, BAND_CONFIG, "-Ifirmware/esp32_band/ble"),
    ),
]


def main() -> int:
    """Compile every host-gate source with gcc inside the CI image."""
    script = ["set -e"]
    for source in SOURCES:
        compiler = "g++ -std=c++17" if source.cpp else "gcc -std=c99"
        # Mirror tests/firmware_host.build: C is compiled with -Werror, C++
        # without it (-Wno-unused-variable) because system headers warn.
        warnings = "-Wall -Wextra -Wno-unused-variable" if source.cpp else "-Wall -Wextra -Werror"
        flags = " ".join(source.flags)
        script.append(
            f"{compiler} -O2 {warnings} {flags} -fPIC -c {source.path} "
            f"-o /tmp/$(basename {source.path}).o"
        )
    script.append("echo 'gcc: all the host-gate sources compile'")
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
            "\n".join(script),
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
