"""Host-side syntax and logic gate for the Synapse Research Service.

firmware/esp32_band/ble/ble_research_service.c can only be fully built by
ESP-IDF, but leaving it unverified until the first `idf.py build` means the
toolchain installation and the first syntax check happen at the same moment,
with no way to tell a driver bug from a typo.

This test compiles the file against the NimBLE/ESP-IDF stubs in
tests/firmware_host/stubs with `-Wall -Wextra -Werror`, and links it with the
raw-frame codec. It has already earned its keep: it caught an implicit
int -> uint8_t truncation of the unit-scale constants (1000 -> 232) that would
have silently corrupted every quantised sample, and a missing <stdbool.h>.

Skips (rather than fails) when no C compiler is available locally; on CI the
absence of a compiler is a failure, so the gate cannot silently disappear.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess

import pytest

from tests.firmware_host import COMMON, ROOT, run_compiler, try_load  # noqa: E402

BAND_BLE = ROOT / "firmware" / "esp32_band" / "ble"
GLUE = ROOT / "tests" / "firmware_host" / "host_glue_research.c"
STUBS = ROOT / "tests" / "firmware_host" / "stubs"

# Compiled with -Wall -Wextra -Werror, so a warning in this translation unit
# is a test failure.
SOURCES = [
    BAND_BLE / "ble_research_service.c",
    COMMON / "ble" / "synapse_raw_frame.c",
    GLUE,
]

INCLUDES = [
    "-Itests/firmware_host/stubs",
    "-Ifirmware/esp32_band/ble",
    "-Ifirmware/common/ble",
]

REQUIRED_STUB_HEADERS = [
    "esp_err.h",
    "esp_log.h",
    "esp_timer.h",
    "sensor_scheduler.h",
    "host/ble_hs.h",
    "host/ble_gatt.h",
    "host/ble_uuid.h",
    "host/ble_hs_mbuf.h",
    "services/gap/ble_svc_gap.h",
    "services/gatt/ble_svc_gatt.h",
]

# Header set the research service includes directly. A stub that is not in this
# list is dead weight, and an include without a stub is a silently skipped gate.
SERVICE_INCLUDES = {
    "esp_err.h",
    "esp_log.h",
    "esp_timer.h",
    "sensor_scheduler.h",
    "host/ble_hs.h",
    "host/ble_gatt.h",
    "host/ble_uuid.h",
    "host/ble_hs_mbuf.h",
    "services/gap/ble_svc_gap.h",
    "services/gatt/ble_svc_gatt.h",
    "ble_research_service.h",
    "synapse_raw_frame.h",
}


def _compiler() -> list[str]:
    """Locate a C compiler as a command list, or fail the gate.

    The gate must not silently disappear, so this never returns None: the
    fallback is `python -m ziglang cc` for an interpreter that has ziglang
    installed (`uv sync --group dev`). A compile gate that skips is not a gate.
    """
    from tests.firmware_host import _find

    found = _find("gcc", "cc", "clang")
    if found is None:
        pytest.fail("no C compiler available (install gcc/clang, or `uv sync --group dev`)")
        return []
    return found


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory):
    cc = _compiler()
    assert cc, "no C compiler available"

    out = tmp_path_factory.mktemp("research_svc")
    objs = []
    for idx, src in enumerate(SOURCES):
        obj = out / f"{idx}_{src.stem}.o"
        run_compiler(
            [
                *cc,
                "-std=c99",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-fPIC",
                *INCLUDES,
                "-c",
                str(src),
                "-o",
                str(obj),
            ]
        )
        objs.append(str(obj))

    lib = out / ("research.dll" if os.name == "nt" else "research.so")
    run_compiler([*cc, "-shared", "-o", str(lib), *objs])
    assert lib.is_file(), f"linker produced no library at {lib}"
    lib_handle = try_load(lib)
    if lib_handle is None:
        pytest.skip(
            "OS policy blocked loading the freshly built library; the compile "
            "succeeded, which is what this gate proves here"
        )
    return lib_handle


def test_research_service_compiles_against_stubs(service: ctypes.CDLL) -> None:
    service.synapse_host_smoke_main.restype = ctypes.c_int
    service.synapse_host_smoke_main.argtypes = []
    assert service.synapse_host_smoke_main() == 0


def test_research_service_uses_the_shared_codec(service: ctypes.CDLL) -> None:
    """The service must not carry a second, diverging frame implementation."""
    service.synapse_raw_max_samples.restype = ctypes.c_uint16
    service.synapse_raw_max_samples.argtypes = [ctypes.c_uint8]
    assert int(service.synapse_raw_max_samples(0x02)) == (244 - 12) // 2


def test_submit_before_init_is_safe(service: ctypes.CDLL) -> None:
    """The API is called from a FreeRTOS task; NULL must be a no-op."""
    service.synapse_research_service_submit.restype = None
    service.synapse_research_service_submit.argtypes = [ctypes.c_void_p]
    service.synapse_research_service_submit(None)


def test_stubs_cover_the_includes_the_service_uses() -> None:
    """Every include must have a stub, and every stub must still be needed."""
    source = (BAND_BLE / "ble_research_service.c").read_text(encoding="utf-8")
    header = (BAND_BLE / "ble_research_service.h").read_text(encoding="utf-8")

    for include in SERVICE_INCLUDES:
        if include in ("ble_research_service.h", "synapse_raw_frame.h"):
            continue
        included = f'"{include}"' in source or f'"{include}"' in header
        assert included, f"the research service no longer includes {include}"

    for stub in REQUIRED_STUB_HEADERS:
        assert (STUBS / stub).is_file(), f"stub missing: {stub}"

    # Guards against editing the header and forgetting the stub it needs.
    assert "sensor_sample_t" in header, "the header must forward the sample type"


def test_unit_scale_constants_fit_their_field() -> None:
    """unit_scale is uint16_t because the scale reaches 1000.

    Regression guard: this was uint8_t, which truncated 1000 to 232 and
    silently corrupted every quantised ECG/IMU sample.
    """
    header = (BAND_BLE / "ble_research_service.h").read_text(encoding="utf-8")
    assert "uint16_t unit_scale" in header
    source = (BAND_BLE / "ble_research_service.c").read_text(encoding="utf-8")
    assert "synapse_research_unit_t" in header  # enum must exist on both sides
    for constant in (
        "SYNAPSE_RESEARCH_UNIT_UV",
        "SYNAPSE_RESEARCH_UNIT_MG",
        "SYNAPSE_RESEARCH_UNIT_MRAD_S",
        "SYNAPSE_RESEARCH_UNIT_CUT",
    ):
        assert constant in header, f"{constant} must be declared"
        assert constant in source, f"{constant} must be used by the stream table"
