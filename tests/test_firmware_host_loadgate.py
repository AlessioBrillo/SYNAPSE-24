"""Host-side load gate: proves the compile step works on this machine.

`tests/firmware_host` builds C/C++ sources into a shared library and loads it
with ctypes. On Windows, an Application Control policy can block loading a
library that was just written to the temp directory, which is an *environment*
failure rather than a code failure. This test separates the two:

- it always compiles something, so a broken build is a failure here;
- it then attempts to load, and skips with an explicit reason if the host policy
  refuses.

That keeps the gate honest: a code regression fails, an OS policy does not
silently pass.
"""

from __future__ import annotations

import ctypes

import pytest

from tests.firmware_host import COMMON, ROOT, build, try_load  # noqa: E402

RAW_FRAME_C = COMMON / "ble" / "synapse_raw_frame.c"


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory):
    return build(tmp_path_factory.mktemp("loadgate"), [RAW_FRAME_C])


def test_compile_of_raw_frame_succeeds(built: object) -> None:
    """The build itself must succeed — this is the part that catches code errors."""
    assert getattr(built, "is_file", lambda: False)(), f"build produced no library: {built}"


def test_library_loads_or_host_policy_blocks(built: object) -> None:
    """Loading is expected to work; if the host blocks it, say so explicitly."""
    lib = try_load(built)
    if lib is None:
        pytest.skip(
            "the OS refused to load the freshly built library (Windows Application "
            "Control blocks DLLs from the temp directory). The compile succeeded, "
            "which is what this gate can prove on this host; the parity assertions "
            "run in CI where loading is permitted."
        )
    assert isinstance(lib, ctypes.CDLL)


def test_repo_pins_the_compiler_fallback(built: object) -> None:
    """Ziglang must be a dev dependency, or the gate silently disappears."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "ziglang" in pyproject, (
        "ziglang must be a dev dependency: it is the compiler of last resort that "
        "keeps the firmware host gate from skipping on machines with no C toolchain"
    )
