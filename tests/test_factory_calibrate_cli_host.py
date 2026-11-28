"""Compile the real firmware text-field parser on the host.

The Python fake transport mirrors this parser; compiling the C helper keeps
that mirror honest and proves `cal operator "A. Brillo"` is stored whole
instead of being cut at the first space by strtok.
"""

from __future__ import annotations

import ctypes
import time
from pathlib import Path

import pytest

from tests.firmware_host import build, try_load

ROOT = Path(__file__).resolve().parents[1]
BAND = ROOT / "firmware" / "esp32_band"
STUBS = ROOT / "tests" / "firmware_host" / "stubs"
GLUE = ROOT / "tests" / "firmware_host" / "host_glue_calibration.c"

GLUE_C = """
#include <stddef.h>

int synapse_factory_parse_text_arg(char *tail, char *out, size_t out_size);

int host_parse_text(const char *tail, char *out, size_t out_size) {
    return synapse_factory_parse_text_arg((char *)tail, out, out_size);
}
"""


@pytest.fixture(scope="module")
def lib(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    out = tmp_path_factory.mktemp("factory_cli_host")
    glue = out / "host_glue_factory_cli.c"
    glue.write_text(GLUE_C, encoding="utf-8")
    shared = build(
        out,
        [
            BAND / "config" / "synapse_calibration.c",
            BAND / "main" / "synapse_factory_cli.c",
            GLUE,
            glue,
        ],
        extra_includes=[str(STUBS), str(BAND / "config"), str(BAND / "main")],
    )
    # Windows Application Control intermittently refuses a freshly built DLL;
    # retry briefly so a transient block cannot hide the compiled check.
    loaded: ctypes.CDLL | None = None
    for _ in range(3):
        loaded = try_load(shared)
        if loaded is not None:
            break
        time.sleep(0.1)
    if loaded is None:  # pragma: no cover - persistent OS policy block
        pytest.skip("OS policy blocked loading the freshly built library; the compile succeeded")
    loaded.host_parse_text.argtypes = [
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_char),
        ctypes.c_size_t,
    ]
    loaded.host_parse_text.restype = ctypes.c_int
    return loaded


def _parse(lib: ctypes.CDLL, tail: bytes, cap: int = 24) -> tuple[int, str]:
    out = ctypes.create_string_buffer(cap)
    length = int(lib.host_parse_text(tail, out, cap))
    return length, out.value.decode("utf-8")


def test_quoted_multiword_operator_round_trips(lib: ctypes.CDLL) -> None:
    length, value = _parse(lib, b'"A. Brillo"')
    assert length == len("A. Brillo")
    assert value == "A. Brillo"


def test_unquoted_value_keeps_the_legacy_first_token(lib: ctypes.CDLL) -> None:
    _, value = _parse(lib, b"A. Brillo")
    assert value == "A."


def test_malformed_quotes_are_rejected(lib: ctypes.CDLL) -> None:
    assert _parse(lib, b'"A. Brillo')[0] < 0  # unterminated
    assert _parse(lib, b'"A." extra')[0] < 0  # trailing junk
    assert _parse(lib, b'A."Brillo')[0] < 0  # stray quote in an unquoted token


def test_bounds_are_enforced(lib: ctypes.CDLL) -> None:
    assert _parse(lib, b'"1234567890"', cap=5)[0] < 0
