"""Host gate for the firmware FIT writer.

``firmware/esp32_band/fit/fit_writer.c`` writes the session file that Averyn
imports. It could only be built by ESP-IDF, so nothing checked that the
messages it emits are the ones a FIT parser expects: the developer data
messages were malformed (swapped global message numbers, wrong field layout,
undeclared developer fields) and the file CRC covered the wrong region, which
made the files unreadable by a standard parser.

This gate compiles the writer on the host (zig cc / gcc, no ESP-IDF) and
parses the file it produces with:

* the project's canonical reader (``synapse_device.fit.read_fit``), so the
  developer values must resolve BY NAME and not by position;
* an independent CRC implementation, so the header CRC and the file CRC are
  verified byte-for-byte;
* ``fitdecode`` (a standard third-party parser) when it is installed, so the
  file is proven readable by something that knows nothing about Synapse.

The writer is compiled with ``-DSYNAPSE_FIT_BASE_PATH=<tmpdir>``: the SPIFFS
mount point does not exist on the build host.
"""

from __future__ import annotations

import os
import struct
import subprocess
from pathlib import Path

import pytest

from tests.firmware_host import ROOT, run_compiler  # noqa: E402

FIT_DIR = ROOT / "firmware" / "esp32_band" / "fit"
BLE_DIR = ROOT / "firmware" / "esp32_band" / "ble"
GLUE = ROOT / "tests" / "firmware_host" / "host_glue_fit.c"

SOURCES = [
    FIT_DIR / "fit_writer.c",
    GLUE,
]

INCLUDES = [
    f"-I{ROOT / 'tests' / 'firmware_host' / 'stubs'}",
    f"-I{FIT_DIR}",
    f"-I{BLE_DIR}",
]


# ---------------------------------------------------------------------------
# FIT constants, written independently of the firmware and of the SDK so the
# test cannot inherit a mistake from either of them.
# ---------------------------------------------------------------------------
FIT_HEADER_SIZE = 14
FIT_CRC_SIZE = 2
MESG_RECORD = 20
MESG_FIELD_DESCRIPTION = 206
MESG_DEVELOPER_DATA_ID = 207

CRC_TABLE = (
    0x0000,
    0xCC01,
    0xD801,
    0x1400,
    0xF001,
    0x3C00,
    0x2800,
    0xE401,
    0xA001,
    0x6C00,
    0x7800,
    0xB401,
    0x5000,
    0x9C01,
    0x8801,
    0x4400,
)

DEV_FIELD_NAMES = (
    "synapse_stress",
    "synapse_sqi",
    "synapse_map",
    "synapse_ecg_quality",
    "synapse_sleep_stage",
    "synapse_rmssd",
    "synapse_sdnn",
    "synapse_lf_hf",
)


def fit_crc16(data: bytes, crc: int = 0) -> int:
    """The FIT CRC-16 (nibble table), written independently of the firmware."""
    for byte in data:
        tmp = CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ CRC_TABLE[byte & 0xF]
        tmp = CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ CRC_TABLE[(byte >> 4) & 0xF]
    return crc & 0xFFFF


def parse_fit(data: bytes) -> list[dict]:
    """Walk a FIT stream and describe every message.

    A deliberately independent implementation (the firmware and the SDK both
    have their own): it is the yardstick the firmware output is measured
    against.
    """
    definitions: dict[int, tuple[int, list, list]] = {}
    messages: list[dict] = []
    pos = FIT_HEADER_SIZE
    end = len(data) - FIT_CRC_SIZE
    while pos < end:
        header = data[pos]
        pos += 1
        local = header & 0x0F
        if header & 0x40:  # definition message
            # reserved (1) + architecture (1) + global message number (2)
            global_num = struct.unpack_from("<H", data, pos + 2)[0]
            n_fields = data[pos + 4]
            pos += 5
            fields = []
            for _ in range(n_fields):
                fields.append(struct.unpack_from("<BBB", data, pos))
                pos += 3
            devs: list[tuple[int, int, int]] = []
            if header & 0x20:  # developer-data flag
                n_dev = data[pos]
                pos += 1
                for _ in range(n_dev):
                    devs.append(struct.unpack_from("<BBB", data, pos))
                    pos += 3
            definitions[local] = (global_num, fields, devs)
            messages.append(
                {
                    "kind": "definition",
                    "global": global_num,
                    "fields": fields,
                    "dev_fields": devs,
                }
            )
        else:  # data message
            global_num, fields, devs = definitions[local]
            size = sum(f[1] for f in fields) + sum(d[1] for d in devs)
            messages.append(
                {
                    "kind": "data",
                    "global": global_num,
                    "fields": fields,
                    "dev_fields": devs,
                    "payload": data[pos : pos + size],
                }
            )
            pos += size
    return messages


def _compiler() -> list[str]:
    from tests.firmware_host import _find

    found = _find("gcc", "cc", "clang")
    if found is None:
        pytest.fail("no C compiler available (install gcc/clang, or `uv sync --group dev`)")
        return []
    return found


@pytest.fixture(scope="module")
def fit_out_dir() -> Path:
    """Directory the firmware writer writes into (its SPIFFS mount point).

    The firmware keeps the session path in a 64-byte buffer, so the mount
    point has to stay short: a pytest tmp_path is far too long for it.
    """
    import shutil
    import tempfile

    # A unique directory per session: a name derived from the PID is shared
    # by concurrent runs, and the teardown of one wiped the other's.
    path = Path(tempfile.mkdtemp(prefix="synapse-fit-"))
    path.mkdir(parents=True, exist_ok=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="module")
def fit_writer(fit_out_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Compile the firmware FIT writer into a standalone program and run it.

    The build host refuses to load freshly built DLLs (Windows Application
    Control), so the writer is driven through its own entry point and the
    file it leaves behind is inspected.
    """
    cc = _compiler()
    out = tmp_path_factory.mktemp("fit_writer")
    # The macro value must be a C string literal; forward slashes keep the
    # path valid inside a C string on every platform.
    base_path = fit_out_dir.as_posix()
    obj = out / "fit_writer.o"
    run_compiler(
        [
            *cc,
            "-std=c99",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            *INCLUDES,
            f'-DSYNAPSE_FIT_BASE_PATH="{base_path}"',
            "-DCONFIG_SYNAPSE_FEATURE_FIT_STORAGE=1",
            "-c",
            str(SOURCES[0]),
            "-o",
            str(obj),
        ]
    )
    exe = out / ("fit_writer.exe" if os.name == "nt" else "fit_writer")
    run_compiler(
        [
            *cc,
            "-std=c99",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            *INCLUDES,
            f'-DSYNAPSE_FIT_BASE_PATH="{base_path}"',
            "-DCONFIG_SYNAPSE_FEATURE_FIT_STORAGE=1",
            str(SOURCES[0]),
            str(SOURCES[1]),
            "-o",
            str(exe),
        ]
    )
    assert exe.is_file(), f"the linker produced no program at {exe}"

    try:
        proc = subprocess.run(  # noqa: S603
            [str(exe)],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except OSError as exc:
        # The host policy can block a freshly built program (Windows
        # Application Control). The compile succeeded, which is what this
        # gate can prove on such a host; CI runs the program.
        pytest.skip(
            f"the host policy blocked the freshly built program ({exc}); the "
            "compile succeeded, which is what this gate proves here"
        )
    assert proc.returncode == 0, (
        f"the firmware FIT writer failed (exit {proc.returncode}): {proc.stderr}"
    )
    return exe


@pytest.fixture(scope="module")
def fit_file(fit_writer: Path, fit_out_dir: Path) -> Path:
    """The FIT file the firmware writer produced."""
    files = sorted(fit_out_dir.glob("*.fit"))
    assert len(files) == 1, f"expected exactly one FIT file, got {files}"
    return files[0]


def test_header_is_a_valid_fit_header(fit_file: Path) -> None:
    """14-byte header, .FIT magic, data size and header CRC."""
    data = fit_file.read_bytes()
    assert data[0] == FIT_HEADER_SIZE, "header size must be 14"
    assert data[1] == 0x10, "protocol version must be 1.0"
    assert data[8:12] == b".FIT", "the .FIT magic is missing"

    data_size = struct.unpack_from("<I", data, 4)[0]
    assert data_size == len(data) - FIT_HEADER_SIZE - FIT_CRC_SIZE, (
        "the data size must cover the records only (not the header, not the trailing CRC)"
    )

    header_crc = struct.unpack_from("<H", data, 12)[0]
    assert header_crc == fit_crc16(data[:12]), "the header CRC is wrong"


def test_file_crc_covers_every_preceding_byte(fit_file: Path) -> None:
    """The trailing CRC is the CRC-16 of the whole file except those 2 bytes."""
    data = fit_file.read_bytes()
    stored = struct.unpack_from("<H", data, len(data) - FIT_CRC_SIZE)[0]
    assert stored == fit_crc16(data[:-FIT_CRC_SIZE]), (
        "the file CRC must cover every byte before it (header included)"
    )


def test_the_canonical_reader_resolves_the_developer_fields(fit_file: Path) -> None:
    """The project's own reader must resolve the values BY NAME."""
    from synapse_device.fit import read_fit

    parsed = read_fit(fit_file)
    assert parsed.n_records == 3, f"expected 3 records, got {parsed.n_records}"
    for record in parsed.records:
        assert record["heart_rate"] in (60, 61, 62)
        dev = record["dev"]
        assert set(dev) == set(DEV_FIELD_NAMES), (
            f"the developer fields did not resolve by name: {sorted(dev)}"
        )
        assert dev["synapse_sqi"] == 200
        assert dev["synapse_map"] == 50
        assert dev["synapse_ecg_quality"] == 220
        assert dev["synapse_stress"] == 1
        assert dev["synapse_rmssd"] == 37
        assert dev["synapse_sdnn"] == 42
        # The feature stream carries no sleep stage: the contract's invalid
        # sentinel is written instead of a fabricated "wake".
        assert dev["synapse_sleep_stage"] == 255


def test_the_developer_messages_use_the_profile_message_numbers(fit_file: Path) -> None:
    """206 is the field description, 207 the developer data id."""
    messages = parse_fit(fit_file.read_bytes())
    seen = {m["global"] for m in messages if m["kind"] == "definition"}
    assert MESG_FIELD_DESCRIPTION in seen, "no field_description (206) message"
    assert MESG_DEVELOPER_DATA_ID in seen, "no developer_data_id (207) message"
    assert MESG_RECORD in seen, "no record (20) message"


def test_the_field_description_declares_the_eight_developer_fields(fit_file: Path) -> None:
    """One field_description message per developer field, with its name."""
    messages = parse_fit(fit_file.read_bytes())
    descriptions = [
        m for m in messages if m["kind"] == "data" and m["global"] == MESG_FIELD_DESCRIPTION
    ]
    assert len(descriptions) == 8, (
        f"expected one field description per developer field, got {len(descriptions)}"
    )

    names: dict[int, str] = {}
    for message in descriptions:
        payload = message["payload"]
        index = payload[0]
        number = payload[1]
        base_type = payload[2]
        name = payload[3 : 3 + 32].split(b"\x00", 1)[0].decode("utf-8")
        assert index == 0, "the Synapse fields use developer data index 0"
        assert base_type in (0x00, 0x02, 0x84), f"unexpected base type {base_type}"
        names[number] = name

    assert sorted(names.values()) == sorted(DEV_FIELD_NAMES), (
        f"the field descriptions do not carry the contract names: {sorted(names.values())}"
    )
    # The contract ids are 0..7; on the wire they are the FIT field
    # definition numbers 1..8 (0 is reserved by the record's standard
    # fields), exactly like the canonical SDK writer.
    assert sorted(names) == list(range(1, 9)), f"unexpected definition numbers: {sorted(names)}"


def test_the_developer_fields_are_declared_in_the_record_definition(fit_file: Path) -> None:
    """The record definition must declare the 8 developer fields."""
    messages = parse_fit(fit_file.read_bytes())
    definitions = [m for m in messages if m["kind"] == "definition" and m["global"] == MESG_RECORD]
    assert definitions, "no record definition found in the FIT file"
    devs = definitions[0]["dev_fields"]
    assert len(devs) == 8, f"expected 8 developer fields, got {len(devs)}"
    numbers = [d[0] for d in devs]
    assert numbers == list(range(1, 9)), (
        f"the developer field definition numbers must be 1..8: {numbers}"
    )
    for _field_num, size, dev_index in devs:
        assert dev_index == 0, "every Synapse field uses developer data index 0"
        assert size in (1, 2), f"unexpected developer field size {size}"


def test_the_record_carries_the_developer_values(fit_file: Path) -> None:
    """The values travel inside the record message, after the standard ones."""
    messages = parse_fit(fit_file.read_bytes())
    records = [m for m in messages if m["kind"] == "data" and m["global"] == MESG_RECORD]
    assert len(records) == 3, f"expected 3 records, got {len(records)}"
    for message in records:
        payload = message["payload"]
        # timestamp (4) + heart_rate (1) + 5 uint8 + 3 uint16 developer values
        assert len(payload) == 4 + 1 + 5 + 6
        heart_rate = payload[4]
        assert heart_rate in (60, 61, 62)
        assert payload[5] == 1  # synapse_stress
        assert payload[6] == 200  # synapse_sqi
        assert payload[7] == 50  # synapse_map
        assert payload[8] == 220  # synapse_ecg_quality
        assert payload[9] == 255  # synapse_sleep_stage: invalid sentinel
        assert struct.unpack_from("<H", payload, 10)[0] == 37  # rmssd
        assert struct.unpack_from("<H", payload, 12)[0] == 42  # sdnn


def test_a_standard_parser_reads_the_file(fit_file: Path) -> None:
    """Fitdecode (third-party, knows nothing about Synapse) must parse it."""
    fitdecode = pytest.importorskip("fitdecode")

    with fitdecode.FitReader(str(fit_file)) as reader:
        frames = list(reader)

    definitions = [f for f in frames if f.frame_type == fitdecode.FIT_FRAME_DEFINITION]
    data = [f for f in frames if f.frame_type == fitdecode.FIT_FRAME_DATA]
    assert definitions, "the parser found no definition message"
    assert data, "the parser found no data message"

    # The parser must have recognised the developer data messages.
    names = set()
    for frame in definitions:
        if frame.mesg_type is not None:
            names.add(frame.mesg_type.name)
    assert "field_description" in names, (
        f"the field_description message was not recognised: {sorted(names)}"
    )
    assert "developer_data_id" in names, (
        f"the developer_data_id message was not recognised: {sorted(names)}"
    )
