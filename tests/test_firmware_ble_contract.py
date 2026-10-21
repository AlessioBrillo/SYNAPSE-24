"""Static contract gate for the Synapse Band firmware BLE/FIT layers.

The firmware under ``firmware/esp32_band`` can only be fully built by
ESP-IDF, so the defects this gate covers were invisible until the first
``idf.py build`` on real hardware:

* the Synapse Research Service was registered AFTER the NimBLE host task
  started, which makes ``ble_gatts_add_svcs()`` fail with BLE_HS_EBUSY and
  costs the dataset path;
* the advertising payload was 39 bytes, over the 31-byte legacy limit, and
  the return codes of ``ble_gap_adv_set_fields()`` / ``ble_gap_adv_start()``
  were ignored;
* the Heart Rate characteristic announced an RR interval it did not send and
  leaked an uninitialised byte;
* the band-core build (FIT / provisioning / OTA off) still compiled and ran
  all three subsystems;
* the FIT developer data messages were malformed, so no standard parser could
  read the session file.

The assertions are static (no compiler needed): they scan the sources for the
structures the fixes introduced, so a regression cannot slip back in between
two ESP-IDF builds.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BAND = ROOT / "firmware" / "esp32_band"
BLE_DIR = BAND / "ble"
MAIN_DIR = BAND / "main"
FIT_DIR = BAND / "fit"

GATT_C = BLE_DIR / "ble_gatt_server.c"
GATT_H = BLE_DIR / "ble_gatt_server.h"
RESEARCH_C = BLE_DIR / "ble_research_service.c"
APP_MAIN = MAIN_DIR / "app_main.c"
MAIN_CMAKE = MAIN_DIR / "CMakeLists.txt"
FIT_C = FIT_DIR / "fit_writer.c"
FIT_H = FIT_DIR / "fit_writer.h"
KCONFIG = BAND / "config" / "Kconfig"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing firmware file: {path}"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Macro helpers: the advertising budget is computed from the macros the
# firmware uses, not from a copy of the numbers.
# ---------------------------------------------------------------------------
def _macros(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"\s*#define\s+([A-Za-z_][A-Za-z0-9_]*)(\([^)]*\))?\s+(.+?)\s*$", line)
        if match:
            name = match.group(1)
            args = match.group(2)
            body = match.group(3)
            if args:
                # Function-like macro: keep the parameter list so the
                # expansion can substitute the arguments.
                found[name] = args + " " + body
            else:
                found[name] = body
    return found


def _expand(text: str, macros: dict[str, str], env: dict[str, int]) -> str:
    """Expand object-like and function-like macros (bounded recursion)."""
    for _ in range(32):
        call = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\(([^()]*)\)", text)
        if call and call.group(1) in macros:
            definition = macros[call.group(1)]
            params = ""
            body = definition
            params_match = re.match(r"(\([^)]*\))\s*(.*)$", definition, re.DOTALL)
            if params_match:
                params = params_match.group(1)
                body = params_match.group(2)
            arg_names = [a.strip() for a in params.strip("()").split(",") if a.strip()]
            values = [a.strip() for a in call.group(2).split(",")]
            for arg_name, value in zip(arg_names, values):
                body = re.sub(
                    rf"\b{re.escape(arg_name)}\b",
                    str(env.get(value, 0)),
                    body,
                )
            text = text[: call.start()] + body + text[call.end() :]
            continue
        name = re.search(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", text)
        if name and name.group(1) in macros:
            definition = macros[name.group(1)]
            params_match = re.match(r"(\([^)]*\))\s*(.*)$", definition, re.DOTALL)
            if params_match:
                definition = params_match.group(2)
            text = text[: name.start()] + "(" + definition + ")" + text[name.end() :]
            continue
        break
    return text


def _value(expression: str, macros: dict[str, str], env: dict[str, int] | None = None) -> int:
    expanded = _expand(expression, macros, env or {})
    expanded = re.sub(r"(\d)[uU]\b", r"\1", expanded)
    return int(eval(expanded, {"__builtins__": {}}, {}))  # noqa: S307


# ---------------------------------------------------------------------------
# T1: the research service is registered before the NimBLE host task starts.
# ---------------------------------------------------------------------------
def test_research_service_is_registered_before_the_host_task_starts() -> None:
    """NimBLE starts the GATT server with the host: later add_svcs() = EBUSY."""
    source = _read(GATT_C)

    init = source.index("esp_err_t synapse_ble_gatt_init(void)")
    body = source[init:]
    research = body.index("synapse_research_service_init()")
    host = body.index("nimble_port_freertos_init(")

    assert research < host, (
        "the research service must be registered before nimble_port_freertos_init(): "
        "NimBLE calls ble_gatts_start() when the host starts and ble_gatts_add_svcs() "
        "fails with BLE_HS_EBUSY afterwards"
    )
    assert "esp_err_to_name" in body[research:host], (
        "a research service registration failure must be logged clearly"
    )
    assert re.search(
        r"synapse_research_service_init\(\);\s*\n\s*if\s*\([^)]*\)\s*\{[^}]*return ESP_FAIL",
        body[research:host],
    ), "a failed research service registration must fail the boot loudly"


def test_app_main_does_not_register_the_research_service_anymore() -> None:
    """The registration moved into the BLE init: it must not be duplicated."""
    source = _read(APP_MAIN)
    assert "synapse_research_service_init()" not in source, (
        "the research service is registered by synapse_ble_gatt_init(): a second "
        "registration would fail with BLE_HS_EBUSY"
    )
    # The submission path (the dataset itself) must stay.
    assert "synapse_research_service_submit(" in source


# ---------------------------------------------------------------------------
# T2: the advertising payload fits the 31-byte legacy limit, and the NimBLE
# return codes are checked.
# ---------------------------------------------------------------------------
def test_advertising_payload_fits_the_legacy_limit() -> None:
    """The computed sizes must stay within the 31-byte advertising budget."""
    header = _read(GATT_H)
    macros = _macros(header)

    max_bytes = _value(macros["SYNAPSE_BLE_ADV_PAYLOAD_MAX"], macros)
    assert max_bytes == 31, "the legacy advertising budget is 31 bytes"

    flags = _value(macros["SYNAPSE_BLE_ADV_FLAGS_BYTES"], macros)
    uuid128 = _value(macros["SYNAPSE_BLE_ADV_UUID128_BYTES"], macros)
    uuid16 = _value(macros["SYNAPSE_BLE_ADV_UUID16_BYTES"], macros)

    name = "SYNAPSE-BAND"
    name_bytes = _value(macros["SYNAPSE_BLE_ADV_NAME_OVERHEAD_BYTES"], macros) + len(name)
    adv = flags + uuid128
    scan_response = name_bytes + uuid16

    assert adv <= max_bytes, f"the advertising packet is {adv} bytes (max {max_bytes})"
    assert scan_response <= max_bytes, (
        f"the scan response is {scan_response} bytes (max {max_bytes})"
    )
    # The old layout was flags + name + 16-bit UUID + 128-bit UUID = 39 bytes.
    old_layout = flags + name_bytes + uuid16 + uuid128
    assert old_layout > max_bytes, "the old advertising layout fitted: the test is stale"


def test_the_advertising_sizes_are_computed_from_the_macros() -> None:
    """The helpers the firmware exposes must use the budget macros."""
    source = _read(GATT_C)
    assert "SYNAPSE_BLE_ADV_FLAGS_BYTES + SYNAPSE_BLE_ADV_UUID128_BYTES" in source
    assert "SYNAPSE_BLE_ADV_NAME_BYTES" in source
    assert "SYNAPSE_BLE_ADV_UUID16_BYTES" in source
    header = _read(GATT_H)
    assert "synapse_ble_gatt_adv_payload_size" in header
    assert "synapse_ble_gatt_scan_response_size" in header


def test_the_name_is_in_the_scan_response_and_the_return_codes_are_checked() -> None:
    """The name goes in the scan response, and every NimBLE call is checked.

    The name does not fit in the advertising packet: the flags and the
    128-bit UUID already use 21 of the 31 bytes.
    """
    source = _read(GATT_C)

    adv = source.index("static void start_advertising_impl(")
    body = source[adv : source.index("static void on_sync(void)")]

    # The complete name is set on the scan response, not on the packet.
    assert "ble_gap_adv_rsp_set_fields(" in body, "the scan response is never set"
    rsp = body.index("struct ble_hs_adv_fields rsp")
    rsp_block = body[rsp : body.index("ble_gap_adv_rsp_set_fields(")]
    assert "rsp.name" in rsp_block
    assert "rsp.name_is_complete" in rsp_block
    assert "fields.name" not in body, (  # noqa: PT018
        "the name must not be in the advertising packet: it does not fit"
    )

    # The return codes must be checked.
    for call in ("ble_gap_adv_set_fields(", "ble_gap_adv_rsp_set_fields(", "ble_gap_adv_start("):
        index = body.index(call)
        assert re.search(r"rc\s*=\s*" + re.escape(call), body[index - 40 : index + 80]), (
            f"the return code of {call} is ignored"
        )
    assert body.count("ESP_LOGE") >= 3, "every advertising failure must be logged"


def test_on_sync_does_not_call_the_advertising_helper_twice() -> None:
    """The provisioning and normal branches did the same thing twice."""
    source = _read(GATT_C)
    sync = source.index("static void on_sync(void)")
    body = source[sync : source.index("static void on_reset(")]
    assert body.count("start_advertising_impl(") == 1, (
        "on_sync must start the advertising once (the two modes advertise the same identity)"
    )


# ---------------------------------------------------------------------------
# T3: the Heart Rate characteristic tells the truth.
# ---------------------------------------------------------------------------
def test_the_heart_rate_read_path_sends_flags_0x01_and_three_bytes() -> None:
    """Flags 0x01 (16-bit HR) + HR16 = 3 bytes, and no phantom byte."""
    source = _read(GATT_C)
    start = source.index("// Heart Rate Measurement (0x2A37)")
    body = source[start : source.index("static int cfg_access(")]

    assert "uint8_t flags = 0x01;" in body, (
        "the read path must set bit0 (16-bit Heart Rate Measurement Value)"
    )
    assert "uint8_t n = 3;" in body, "without an RR interval the value is 3 bytes"
    assert "os_mbuf_append(ctxt->om, val, n)" in body, (
        "the number of bytes sent must be the number of bytes built"
    )
    assert "sizeof(val)" not in body, (
        "sizeof(val) sends the whole buffer: the uninitialised byte would reach the client"
    )
    assert "0x10" in body, "the RR-present bit must be set only when an RR exists"
    # The BLE Heart Rate Service flag semantics must be documented.
    assert "Heart Rate Service" in body
    assert "0x2A37" in body


def test_the_feature_frame_does_not_transmit_fabricated_values() -> None:
    """Invalid heart rate, quality and motion are sent as "not available"."""
    source = _read(APP_MAIN)
    # The call site (the header comment mentions the function too).
    start = source.rindex("synapse_ble_gatt_push_feature(")
    block = source[max(0, start - 1500) : start + 600]

    assert re.search(r"synapse_ble_gatt_push_feature\(\s*hr_bpm\s*,", block), (
        "the pushed heart rate must be the validated variable, not a cast of "
        "the raw measurement: an invalid reading would be transmitted as 0 bpm"
    )
    assert "hr_bpm = 0xFFFF" in block, "an invalid heart rate must be 0xFFFF"
    assert "ppg_sqi = 0xFF" in block, "a missing SQI must be 0xFF (not available)"
    assert "ppg_map = 0xFF" in block, "a missing MAP must be 0xFF (not available)"
    assert "ecg_quality = 0xFF" in block, (
        "the ECG quality has no validated value: it must be 0xFF (not available)"
    )
    assert re.search(r"0xFF,\s*//\s*motion_level", block), (
        "the motion level placeholder must be 0xFF (not available)"
    )
    assert "(uint16_t)ppg.hr_bpm) " not in block, (
        "the heart rate must not be cast unconditionally: an invalid reading "
        "would be transmitted as 0 bpm"
    )


# ---------------------------------------------------------------------------
# T4: the band-core build really is a subset.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("flag", "init_call"),
    [
        ("CONFIG_SYNAPSE_FEATURE_FIT_STORAGE", "synapse_fit_init("),
        ("CONFIG_SYNAPSE_FEATURE_PROVISIONING", "synapse_provisioning_init("),
        ("CONFIG_SYNAPSE_FEATURE_OTA", "synapse_ota_init("),
    ],
)
def test_app_main_gates_the_optional_subsystems(flag: str, init_call: str) -> None:
    """Each optional subsystem must be behind its Kconfig flag."""
    source = _read(APP_MAIN)
    index = source.index(init_call)
    # Find the #if that opens the block containing the call.
    before = source[:index]
    guards = [m.start() for m in re.finditer(r"#if\s+defined\(" + re.escape(flag) + r"\)", before)]
    assert guards, f"{init_call} is not guarded by {flag}"
    last_guard = guards[-1]
    # The guard must still be open when the call is reached (no #endif in
    # between).
    between = before[last_guard:]
    assert "#endif" not in between, f"{init_call} is outside its {flag} guard"


def test_the_optional_subsystems_do_not_abort_the_boot() -> None:
    """A FIT mount failure must not kill a device whose dataset path is BLE."""
    source = _read(APP_MAIN)
    start = source.index("synapse_fit_init()")
    block = source[start : source.index("synapse_ble_gatt_init()")]
    assert "return;" not in block, "a FIT init failure must not abort app_main"
    assert "ESP_LOGW" in block, "a FIT init failure must be logged as a warning"


def test_the_main_component_requires_the_optional_components_conditionally() -> None:
    """CMake must follow the same Kconfig flags as the code."""
    source = _read(MAIN_CMAKE)
    for flag, component in (
        ("CONFIG_SYNAPSE_FEATURE_FIT_STORAGE", "fit"),
        ("CONFIG_SYNAPSE_FEATURE_PROVISIONING", "provisioning"),
        ("CONFIG_SYNAPSE_FEATURE_OTA", "ota"),
    ):
        assert f"if({flag})" in source, f"{component} is not required conditionally"
        block = source.split(f"if({flag})", 1)[1].split("endif()", 1)[0]
        assert component in block, f"the {component} component is not required when {flag} is on"


def test_the_kconfig_help_documents_the_gating() -> None:
    """The flags must say what they now do."""
    source = _read(KCONFIG)
    for flag in (
        "SYNAPSE_FEATURE_FIT_STORAGE",
        "SYNAPSE_FEATURE_PROVISIONING",
        "SYNAPSE_FEATURE_OTA",
    ):
        start = source.index(f"config {flag}")
        block = source[start : source.index("config ", start + 10)]
        assert "gates the code" in block or "not linked" in block, (
            f"the help text of {flag} does not say that the flag gates the code"
        )


# ---------------------------------------------------------------------------
# T5: the FIT developer data messages follow the FIT profile.
# ---------------------------------------------------------------------------
def test_the_fit_global_message_numbers_follow_the_profile() -> None:
    """206 is the field description, 207 the developer data id."""
    header = _read(FIT_H)
    macros = _macros(header)
    assert _value(macros["FIT_MESG_FIELD_DESCRIPTION"], macros) == 206
    assert _value(macros["FIT_MESG_DEVELOPER_DATA_ID"], macros) == 207


def test_the_fit_definition_messages_use_the_0x40_header() -> None:
    """A definition message sets bit6; a data message clears it."""
    header = _read(FIT_H)
    macros = _macros(header)
    assert _value(macros["FIT_HEADER_DEFINITION"], macros) == 0x40
    assert _value(macros["FIT_HEADER_DEVELOPER_DATA"], macros) == 0x20

    source = _read(FIT_C)
    assert "FIT_HEADER_DEFINITION" in source, "the definition header byte is not used"
    # The record definition must carry the developer-data flag, otherwise the
    # developer field definitions that follow it are not read.
    assert re.search(
        r"FIT_HEADER_DEFINITION\s*\|\s*FIT_HEADER_DEVELOPER_DATA",
        source,
    ), "the record definition must set the developer-data flag"


def test_the_fit_developer_messages_use_the_profile_field_numbers() -> None:
    """The field numbers of messages 206 and 207 are part of the contract."""
    header = _read(FIT_H)
    macros = _macros(header)

    expected_dev_id = {
        "FIT_FIELD_DEV_ID_DEVELOPER_ID": 0,
        "FIT_FIELD_DEV_ID_APPLICATION_ID": 1,
        "FIT_FIELD_DEV_ID_MANUFACTURER": 2,
        "FIT_FIELD_DEV_ID_INDEX": 3,
        "FIT_FIELD_DEV_ID_APPLICATION_VERSION": 4,
    }
    for name, value in expected_dev_id.items():
        assert _value(macros[name], macros) == value, f"{name} must be field {value}"

    expected_description = {
        "FIT_FIELD_DESCRIPTION_INDEX": 0,
        "FIT_FIELD_DESCRIPTION_NUMBER": 1,
        "FIT_FIELD_DESCRIPTION_BASE_TYPE": 2,
        "FIT_FIELD_DESCRIPTION_NAME": 3,
    }
    for name, value in expected_description.items():
        assert _value(macros[name], macros) == value, f"{name} must be field {value}"


def test_the_fit_writer_uses_the_fit_crc_table() -> None:
    """The CRC-16 must be the FIT one (nibble table), not another polynomial."""
    source = _read(FIT_C)
    for constant in (
        "0x0000",
        "0xCC01",
        "0xD801",
        "0x1400",
        "0xF001",
        "0x3C00",
        "0x2800",
        "0xE401",
        "0xA001",
        "0x6C00",
        "0x7800",
        "0xB401",
        "0x5000",
        "0x9C01",
        "0x8801",
        "0x4400",
    ):
        assert constant in source, f"the FIT CRC table is missing {constant}"


def test_the_fit_record_declares_the_developer_fields() -> None:
    """The developer fields must be declared with their number and size.

    The declaration carries the field definition number, the size and the
    developer data index.
    """
    source = _read(FIT_C)
    assert "FIT_DEVELOPER_DATA_INDEX" in source
    assert "SYNAPSE_DEV_FIELD_DEFINITION_NUM" in source, (
        "the contract ids must be mapped to the FIT field definition numbers"
    )
    header = _read(FIT_H)
    macros = _macros(header)
    # The contract ids are 0..7 and the wire definition numbers are 1..8.
    for contract_id in range(8):
        name = f"SYNAPSE_DEV_FIELD_{contract_id}"
        assert name not in macros  # the contract ids are named, not numbered
    assert "SYNAPSE_DEV_FIELD_STRESS" in header
    assert "SYNAPSE_NUM_DEV_FIELDS 8" in header


def test_the_fit_developer_field_names_match_the_contract() -> None:
    """The names are immutable for the consumers."""
    import json

    contract = json.loads(
        (ROOT / "contracts" / "fit_developer_fields.json").read_text(encoding="utf-8")
    )
    source = _read(FIT_C)
    for field in contract["fields"]:
        assert f'"{field["name"]}"' in source, (
            f"the developer field {field['name']} is missing from the writer"
        )


# ---------------------------------------------------------------------------
# T6: the loss is visible to the host.
# ---------------------------------------------------------------------------
def test_the_sequence_is_consumed_when_the_frame_is_built() -> None:
    """A frame the stack refuses must leave a hole in the sequence."""
    source = _read(RESEARCH_C)
    start = source.index("static void flush_stream(")
    body = source[start : source.index("void synapse_research_service_submit(")]
    assert "uint16_t sequence = stream->sequence++;" in body, (
        "the sequence must be consumed when the frame is built, not when the "
        "notification is accepted"
    )
    # The sequence must not be incremented a second time on success.
    assert body.count("stream->sequence++") == 1, (
        "the sequence must be incremented exactly once per frame"
    )


def test_the_dropped_counter_is_carried_by_the_header() -> None:
    """The loss magnitude travels in the frame header."""
    source = _read(RESEARCH_C)
    assert "SYNAPSE_RAW_DROPPED_MAX" in source, "the loss counter must saturate at the header limit"
    assert "research_note_drop(" in source, "the lost samples must be accounted for"
    frame = ROOT / "firmware" / "common" / "ble" / "synapse_raw_frame.h"
    header = _read(frame)
    assert "uint8_t dropped;" in header, "the header must carry the loss counter"
    assert "reserved" not in header.split("typedef struct {", 1)[1].split("}", 1)[0], (
        "the reserved byte became the loss counter"
    )
