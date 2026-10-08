"""Host-side build helper for firmware/common C/C++ sources (no ESP-IDF)."""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "firmware" / "common"
STUBS = Path(__file__).resolve().parent / "stubs"


def _find(*names: str) -> str | None:
    return next((p for p in map(shutil.which, names) if p), None)


def build(out_dir: Path, c_sources: list[Path], cpp_sources: list[Path] | None = None) -> Path:
    """Compile sources into one shared lib with -Wall -Wextra (-Werror for C).

    Skips when no compiler is available, but fails when the CI env var is set so the
    gate cannot silently disappear on GitHub Actions.
    """
    cpp_sources = cpp_sources or []
    cc = _find("gcc", "cc", "clang")
    cxx = _find("g++", "c++", "clang++")
    if cc is None or (cpp_sources and cxx is None):
        if os.environ.get("CI"):
            pytest.fail("no C/C++ compiler on CI; firmware host gate cannot run")
        pytest.skip("no C/C++ compiler available")
    lib = out_dir / ("fw.dll" if os.name == "nt" else "fw.so")
    inc = [f"-I{STUBS}", f"-I{COMMON / 'sensors'}", f"-I{COMMON / 'triage'}"]
    objs: list[str] = []
    jobs = [(cc, "-std=c99", ["-Werror"], src) for src in c_sources]
    jobs += [(cxx, "-std=c++17", ["-Wno-unused-variable"], src) for src in cpp_sources]
    for i, (driver, std, extra, src) in enumerate(jobs):
        obj = str(out_dir / f"{i}_{src.stem}.o")
        subprocess.run(  # noqa: S603
            [
                str(driver),
                std,
                "-O2",
                "-Wall",
                "-Wextra",
                *extra,
                "-fPIC",
                *inc,
                "-c",
                "-o",
                obj,
                str(src),
            ],
            check=True,
        )
        objs.append(obj)
    linker = cxx if cpp_sources else cc
    subprocess.run([str(linker), "-shared", "-o", str(lib), *objs, "-lm"], check=True)  # noqa: S603
    return lib


def load(lib: Path) -> ctypes.CDLL:
    """Load the built library; OS policy blocks (e.g. Windows App Control) skip locally, fail on CI."""
    try:
        return ctypes.CDLL(str(lib))
    except OSError as exc:
        if os.environ.get("CI"):
            raise
        pytest.skip(f"cannot load freshly built library on this host: {exc}")
