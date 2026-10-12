#!/usr/bin/env python3
"""ESP-IDF toolchain verification for SYNAPSE-24 firmware.

Verifies that the host can build the canonical ``esp32_band`` firmware before
any code is written. Run this first: a firmware that has never been compiled is
not an asset, it is a liability.

Usage:
    uv run python tools/check_toolchain.py
    uv run python tools/check_toolchain.py --quiet
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ESP-IDF 5.2+ is the floor documented in HARDWARE_BRINGUP.md and
# firmware/esp32_band/README.md. The S3 PSRAM/TFLM toolchain needs 5.x.
IDF_MIN_MAJOR = 5
IDF_MIN_MINOR = 2
IDF_MAX_MAJOR = 5


@dataclass(frozen=True)
class Check:
    """A single toolchain requirement and its outcome."""

    name: str
    ok: bool
    detail: str
    blocking: bool = True


def _run(cmd: list[str], timeout_s: int = 60) -> tuple[int, str]:
    """Run a command, returning (returncode, combined output)."""
    try:
        proc = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def check_esp_idf() -> list[Check]:
    """Verify ESP-IDF is installed, activated and >= 5.2."""
    import os

    checks: list[Check] = []

    idf_path = os.environ.get("IDF_PATH")
    if not idf_path:
        checks.append(
            Check(
                name="ESP-IDF environment",
                ok=False,
                detail=(
                    "IDF_PATH is not set. Activate the ESP-IDF environment "
                    "first: %USERPROFILE%\\esp\\esp-idf\\export.bat (Windows) or "
                    "source ~/esp/esp-idf/export.sh (Linux/macOS)."
                ),
            )
        )
        return checks

    idf_dir = Path(idf_path)
    if not idf_dir.is_dir():
        checks.append(
            Check(
                name="ESP-IDF install",
                ok=False,
                detail=f"IDF_PATH points at a missing directory: {idf_dir}",
            )
        )
        return checks

    version_file = idf_dir / "version.txt"
    version = (
        version_file.read_text(encoding="utf-8").strip() if version_file.is_file() else "unknown"
    )
    major = minor = 0
    try:
        major, minor = (int(p) for p in version.split(".")[:2])
    except ValueError:
        pass

    in_range = IDF_MIN_MAJOR <= major <= IDF_MAX_MAJOR and (
        major > IDF_MIN_MAJOR or minor >= IDF_MIN_MINOR
    )
    checks.append(
        Check(
            name="ESP-IDF version",
            ok=in_range,
            detail=(
                f"v{version} (need >={IDF_MIN_MAJOR}.{IDF_MIN_MINOR}, "
                f"<={IDF_MAX_MAJOR}.x) at {idf_dir}"
            ),
        )
    )
    return checks


def check_python() -> list[Check]:
    """Verify a compatible Python interpreter is available for ESP-IDF.

    ESP-IDF 5.x requires Python 3.8+; SYNAPSE-24 pins the host to 3.11 in
    pyproject.toml, so that is the interpreter the toolchain must run under.
    """
    checks: list[Check] = []
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    checks.append(
        Check(
            name="Python interpreter",
            ok=sys.version_info[:2] == (3, 11),
            blocking=False,
            detail=f"{version} (recommended 3.11 for ESP-IDF + this project)",
        )
    )
    rc, out = _run([sys.executable, "-c", "import sys; print(sys.executable)"])
    checks.append(Check(name="Python executable", ok=rc == 0, detail=out.strip() or "n/a"))
    return checks


def check_build_tools() -> list[Check]:
    """Verify cmake, ninja, git and the esptool are on PATH."""
    tools = {
        "cmake": "CMake (ESP-IDF build generator)",
        "ninja": "Ninja (build backend)",
        "git": "Git (component manager / versioning)",
        "idf.py": "ESP-IDF build entry point",
    }
    checks: list[Check] = []
    for exe, label in tools.items():
        path = shutil.which(exe)
        checks.append(
            Check(
                name=label,
                ok=path is not None,
                detail=path or f"'{exe}' not found on PATH",
                blocking=exe in {"idf.py", "cmake", "ninja"},
            )
        )
    return checks


def check_serial_driver() -> list[Check]:
    """Verify esptool can see the USB-serial bridge (non-blocking, no board needed)."""
    try:
        import esptool  # noqa: F401
    except ImportError:
        return [
            Check(
                name="esptool",
                ok=False,
                detail="esptool not installed — install with: uv sync --group hardware",
                blocking=False,
            )
        ]
    rc, out = _run([sys.executable, "-m", "esptool", "version"], timeout_s=60)
    return [
        Check(
            name="esptool", ok=rc == 0, detail=out.strip().splitlines()[0] if out.strip() else "ok"
        )
    ]


def check_host_compile_gates() -> list[Check]:
    """Verify a C/C++ compiler is reachable for the firmware host gates.

    ``tests/firmware_host`` compiles the raw-frame codec and the Band research
    service against ESP-IDF stubs, so the wire format and the firmware API stay
    pinned without ESP-IDF. That gate needs *some* C compiler; ``ziglang`` is
    the pip-installable last resort, so it is checked here too.
    """
    import importlib.util

    checks: list[Check] = []

    compiler = next(
        (p for p in map(shutil.which, ["gcc", "cc", "clang", "g++", "c++", "clang++"]) if p),
        None,
    )
    checks.append(
        Check(
            name="C/C++ compiler",
            ok=compiler is not None,
            detail=compiler or "none on PATH (the ziglang fallback below still applies)",
        )
    )

    try:
        importlib.import_module("ziglang")
        detail = "ziglang importable - provides `python -m ziglang cc`"
        ok = True
    except ImportError:
        ok = False
        detail = "ziglang not importable; install with: uv sync --group dev"

    checks.append(
        Check(
            name="ziglang (host compile-gate fallback)",
            ok=ok,
            blocking=False,
            detail=detail,
        )
    )

    # A compiler or ziglang: at least one must exist for the gate to mean anything.
    if compiler is None and not ok:
        checks.append(
            Check(
                name="Firmware host compile gate",
                ok=False,
                detail=(
                    "no C compiler on PATH and no ziglang: tests/firmware_host cannot run. "
                    "Install gcc/clang, or run `uv sync --group dev` for the ziglang fallback."
                ),
            )
        )
    return checks


def check_firmware_projects() -> list[Check]:
    """Verify the canonical firmware project and its component tree exist."""
    checks: list[Check] = []
    band = ROOT / "firmware" / "esp32_band"
    required = [
        band / "CMakeLists.txt",
        band / "main" / "CMakeLists.txt",
        band / "main" / "app_main.c",
        band / "sdkconfig.defaults",
        band / "partitions.csv",
    ]
    missing = [str(p.relative_to(ROOT)) for p in required if not p.is_file()]
    checks.append(
        Check(
            name="Canonical firmware project (esp32_band)",
            ok=not missing,
            detail="all required files present" if not missing else f"missing: {missing}",
        )
    )

    sdkconfig = band / "sdkconfig.defaults"
    if sdkconfig.is_file():
        text = sdkconfig.read_text(encoding="utf-8")
        checks.append(
            Check(
                name="PSRAM enabled (TFLM arena)",
                ok="CONFIG_SPIRAM=y" in text,
                detail="CONFIG_SPIRAM=y present"
                if "CONFIG_SPIRAM=y" in text
                else "PSRAM not enabled",
            )
        )
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="only print failures")
    args = parser.parse_args()

    all_checks: list[Check] = []
    for group in (
        check_esp_idf(),
        check_python(),
        check_build_tools(),
        check_serial_driver(),
        check_host_compile_gates(),
        check_firmware_projects(),
    ):
        all_checks.extend(group)

    blocking_failures = [c for c in all_checks if c.blocking and not c.ok]
    warnings = [c for c in all_checks if not c.blocking and not c.ok]

    if not args.quiet:
        print("=" * 78)
        print("SYNAPSE-24 firmware toolchain check")
        print("=" * 78)
        for c in all_checks:
            mark = "OK  " if c.ok else ("FAIL" if c.blocking else "WARN")
            print(f"[{mark}] {c.name}: {c.detail}")
        print("-" * 78)

    if blocking_failures:
        print(f"BLOCKED: {len(blocking_failures)} required check(s) failed.")
        print("See docs/hardware/ESP-IDF_SETUP.md for the setup procedure.")
        return 1

    if warnings:
        print(f"READY with {len(warnings)} warning(s) — firmware build is unblocked.")
    else:
        print("READY: all toolchain checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
