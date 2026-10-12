"""Host-side build helper for firmware C/C++ sources (no ESP-IDF required).

Compiles the raw-frame codec and the Synapse Band research service on the CI
host so the wire format stays byte-for-byte pinned between firmware and Python,
and so a firmware typo is caught without installing a toolchain.

Compiler resolution is done here, in Python, rather than through a shell shim:
a ``.cmd``/``.sh`` wrapper was tried and abandoned because cmd.exe mangled the
forwarded arguments, which made a healthy compile exit non-zero for reasons
unrelated to the code. Resolution order:

1. ``gcc`` / ``cc`` / ``clang`` (C) and ``g++`` / ``c++`` / ``clang++`` (C++) on PATH
2. ``python -m ziglang cc`` for any interpreter that can ``import ziglang``
   (a pip package: ``uv sync --group dev``)

If neither is available the caller gets an assertion failure, not a skip: a
compile gate that skips is a gate that cannot fail.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "firmware" / "common"
STUBS = Path(__file__).resolve().parent / "stubs"


def _find(*names: str) -> list[str] | None:
    """Locate a C/C++ compiler as a command list.

    Returns a list rather than a path because the ziglang fallback is a module
    invocation (``python -m ziglang cc``) rather than a binary on PATH.
    """
    found = next((p for p in map(shutil.which, names) if p), None)
    if found:
        return [found]

    zig = _python_with_ziglang()
    if zig is not None:
        return [zig, "-m", "ziglang", "cc"]

    return None


def _python_with_ziglang() -> str | None:
    """Return the path of an interpreter that can ``import ziglang``."""
    candidates = [sys.executable, "python", "python3"]
    for exe in candidates:
        if not exe:
            continue
        try:
            proc = subprocess.run(  # noqa: S603
                [exe, "-c", "import ziglang"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0:
            return exe
    return None


def run_compiler(cmd: list[str]) -> None:
    """Run the compiler, raising CalledProcessError with stderr on failure."""
    try:
        proc = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - defensive
        raise subprocess.CalledProcessError(124, cmd, stderr=str(exc)) from exc

    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr
        )


# Retained under the old private name so existing callers keep working.
_run_compiler = run_compiler


def build(out_dir: Path, c_sources: list[Path], cpp_sources: list[Path] | None = None) -> Path:
    """Compile sources into one shared library with -Wall -Wextra (-Werror for C).

    Never silently skips: resolution falls back to a pip-installable compiler,
    and an unavailable compiler raises rather than skipping, because a compile
    gate that skips cannot fail.
    """
    cpp_sources = cpp_sources or []
    cc = _find("gcc", "cc", "clang")
    cxx = _find("g++", "c++", "clang++")
    assert cc is not None, (
        "no C compiler available (install gcc/clang, or `uv sync --group dev` for ziglang)"
    )
    if cpp_sources:
        assert cxx is not None, "no C++ compiler available (install g++/clang++, or ziglang)"

    lib = out_dir / ("fw.dll" if os.name == "nt" else "fw.so")
    inc = [f"-I{STUBS}", f"-I{COMMON / 'sensors'}", f"-I{COMMON / 'triage'}"]
    objs: list[str] = []
    jobs = [(cc, "-std=c99", ["-Werror"], src) for src in c_sources]
    jobs += [(cxx, "-std=c++17", ["-Wno-unused-variable"], src) for src in cpp_sources]

    for i, (driver, std, extra, src) in enumerate(jobs):
        obj = str(out_dir / f"{i}_{src.stem}.o")
        run_compiler(
            [
                *driver,
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
            ]
        )
        objs.append(obj)

    linker = cxx if cpp_sources else cc
    run_compiler([*linker, "-shared", "-o", str(lib), *objs])
    if not lib.is_file():
        # Some toolchains (notably MSVC-link) reject shared-library output.
        raise AssertionError(f"linker produced no library at {lib}")
    return lib


def load(lib: Path) -> ctypes.CDLL:
    """Load the built library, or raise a clear error.

    A previous version skipped here, which let a broken build look like a pass
    on machines where the OS refuses to load freshly built libraries (Windows
    Application Control is one such case). It now raises: the caller can decide
    whether that is fatal, but it is never silent.
    """
    try:
        return ctypes.CDLL(str(lib))
    except OSError as exc:
        raise OSError(f"cannot load freshly built library {lib}: {exc}") from exc


def try_load(lib: Path) -> ctypes.CDLL | None:
    """Load the library, returning None when the host policy blocks it.

    Used by tests that must distinguish "the code is wrong" from "this OS will
    not run a library we just built" (e.g. Windows Application Control). The
    test then skips with a reason instead of failing on an unrelated cause, and
    still fails when the *build* itself is what is broken.
    """
    try:
        return ctypes.CDLL(str(lib))
    except OSError:
        return None
