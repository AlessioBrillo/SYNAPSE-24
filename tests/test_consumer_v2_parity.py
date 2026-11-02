"""Consumer contract v2: C encoder <-> SDK codec <-> contracts/gatt_v2.json.

``firmware/esp32_band/ble/synapse_consumer_v2.c`` is compiled on the host and
driven through ctypes. Three things must agree, byte for byte:

* the firmware encoder (the C code, which writes through offsets generated from
  ``config/hardware.yaml``);
* the SDK codec (``synapse_device.consumer``), which decodes what the device sends;
* ``contracts/gatt_v2.json``, the generated description other clients read.

It also pins v1: ``contracts/gatt_v1.json`` is imported by Averyn by sha256, so
it must stay byte for byte what it was.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk" / "python"))

from synapse_device import consumer  # noqa: E402

from tests.firmware_host import build, try_load  # noqa: E402

BLE_DIR = ROOT / "firmware" / "esp32_band" / "ble"
GATT_V1 = ROOT / "contracts" / "gatt_v1.json"
GATT_V2 = ROOT / "contracts" / "gatt_v2.json"
VECTORS = ROOT / "contracts" / "vectors" / "consumer_v2.json"

# What Averyn pinned (docs/integrations/synapse.md there): sha256 of gatt_v1.json with LF endings.
GATT_V1_SHA256 = "0c6b9e38fccbf5510ea82741ed5b40c2b2338ce08fe47a7e5655f75a6b0688a9"

_FMT = {"u8": "B", "u16": "H", "u32": "I", "i16": "h"}


class _Feature(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint8),
        ("sequence", ctypes.c_uint16),
        ("t_dev_ms", ctypes.c_uint32),
        ("heart_rate_bpm", ctypes.c_uint16),
        ("rr_last", ctypes.c_uint16),
        ("rr_count", ctypes.c_uint8),
        ("ppg_sqi", ctypes.c_uint8),
        ("ecg_quality", ctypes.c_uint8),
        ("motion_level", ctypes.c_uint8),
        ("stress_level", ctypes.c_uint8),
        ("battery_pct", ctypes.c_uint8),
        ("temperature_c100", ctypes.c_int16),
        ("rmssd_ms", ctypes.c_uint16),
        ("sdnn_ms", ctypes.c_uint16),
        ("sport_type", ctypes.c_uint8),
        ("sensor_health", ctypes.c_uint8),
    ]


class _Status(ctypes.Structure):
    _fields_ = [
        ("tier", ctypes.c_uint8),
        ("sensor_health", ctypes.c_uint8),
        ("flags", ctypes.c_uint8),
        ("mtu", ctypes.c_uint16),
        ("uptime_s", ctypes.c_uint32),
        ("epoch_wraps", ctypes.c_uint16),
        ("ring_overflow", ctypes.c_uint32),
        ("stale_samples", ctypes.c_uint32),
        ("dropped_frames", ctypes.c_uint32),
        ("read_errors", ctypes.c_uint32),
        ("fw_build", ctypes.c_uint32),
    ]


_SET_TX = ctypes.CFUNCTYPE(None, ctypes.c_bool, ctypes.c_void_p)
_SET_RATE = ctypes.CFUNCTYPE(None, ctypes.c_uint8, ctypes.c_void_p)


class _Ops(ctypes.Structure):
    _fields_ = [
        ("set_transmitting", _SET_TX),
        ("set_feature_rate", _SET_RATE),
        ("ctx", ctypes.c_void_p),
    ]


@pytest.fixture(scope="module")
def fw(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    lib = build(
        tmp_path_factory.mktemp("consumer_v2"),
        [BLE_DIR / "synapse_consumer_v2.c"],
        extra_includes=[str(BLE_DIR)],
    )
    d = try_load(lib)
    if d is None:
        pytest.skip("OS policy blocked loading the freshly built library; the compile succeeded")
    d.synapse_feature_v2_init_na.restype = None
    d.synapse_feature_v2_init_na.argtypes = [ctypes.POINTER(_Feature)]
    d.synapse_feature_v2_encode.restype = ctypes.c_size_t
    d.synapse_feature_v2_encode.argtypes = [
        ctypes.POINTER(_Feature),
        ctypes.c_char_p,
        ctypes.c_size_t,
    ]
    d.synapse_status_v2_encode.restype = ctypes.c_size_t
    d.synapse_status_v2_encode.argtypes = [
        ctypes.POINTER(_Status),
        ctypes.c_char_p,
        ctypes.c_size_t,
    ]
    d.synapse_stream_metadata_encode.restype = ctypes.c_size_t
    d.synapse_stream_metadata_encode.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
    d.synapse_control_apply.restype = ctypes.c_int
    d.synapse_control_apply.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(_Ops)]
    return d


def _c_feature(fw: ctypes.CDLL, values: dict[str, int | None]) -> bytes:
    rec = _Feature()
    fw.synapse_feature_v2_init_na(ctypes.byref(rec))
    for name, value in values.items():
        if value is not None:
            setattr(rec, name, value)
    buf = ctypes.create_string_buffer(64)
    n = fw.synapse_feature_v2_encode(ctypes.byref(rec), buf, 64)
    return bytes(buf.raw[:n])


# --- the three descriptions of the layout agree ------------------------------------------


def test_the_sdk_layout_matches_the_generated_json() -> None:
    doc = json.loads(GATT_V2.read_text(encoding="utf-8"))
    for key, fields, size, version in (
        ("feature", consumer.FEATURE_FIELDS, consumer.FEATURE_BYTES, consumer.FEATURE_VERSION),
        ("status", consumer.STATUS_FIELDS, consumer.STATUS_BYTES, consumer.STATUS_VERSION),
    ):
        spec = doc[key]
        assert spec["bytes"] == size
        assert spec["version"] == version
        assert [(f["name"], _FMT[f["type"]], f["offset"], f.get("na")) for f in spec["fields"]] == [
            (f.name, f.fmt, f.offset, f.na) for f in fields
        ], key


def test_the_v2_characteristics_are_new_and_distinct() -> None:
    v2 = json.loads(GATT_V2.read_text(encoding="utf-8"))
    v1 = json.loads(GATT_V1.read_text(encoding="utf-8"))["services"]
    new = list(v2["characteristics"].values())
    taken = {u.lower() for u in v1["feature_stream"]["characteristics"].values()}
    taken |= {u.lower() for u in v1["research"]["characteristics"].values()}
    assert len(set(new)) == 4
    assert not set(new) & taken, "a v2 characteristic reuses a v1 UUID"
    assert v2["service"] == v1["feature_stream"]["uuid"].lower(), (
        "v2 lives on the v1 consumer service"
    )


def test_v1_is_frozen_byte_for_byte() -> None:
    data = GATT_V1.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(data).hexdigest() == GATT_V1_SHA256, (
        "contracts/gatt_v1.json changed: Averyn pins its sha256; add v2 fields, never edit v1"
    )
    assert json.loads(GATT_V2.read_text(encoding="utf-8"))["v1_frozen"]["sha256"] == GATT_V1_SHA256


# --- feature frame ---------------------------------------------------------------------------


def test_the_golden_vectors_are_the_sdk_encoding() -> None:
    doc = json.loads(VECTORS.read_text(encoding="utf-8"))
    for case in doc["feature"]:
        assert consumer.encode_feature(case["values"]).hex() == case["hex"], case["name"]


def test_the_firmware_encoder_reproduces_the_golden_vectors(fw: ctypes.CDLL) -> None:
    for case in json.loads(VECTORS.read_text(encoding="utf-8"))["feature"]:
        assert _c_feature(fw, case["values"]).hex() == case["hex"], case["name"]


def test_not_available_decodes_to_none_never_to_zero(fw: ctypes.CDLL) -> None:
    decoded = consumer.decode_feature(_c_feature(fw, {"sequence": 1}))
    for name in ("heart_rate_bpm", "rr_last", "rr_count", "ppg_sqi", "ecg_quality", "motion_level"):
        assert decoded[name] is None, name
    assert decoded["battery_pct"] is None
    assert decoded["temperature_c100"] is None
    assert decoded["stress_level"] == 3  # unknown, an enum value: the model has not run


_u8 = st.integers(0, 254)  # 255 is the sentinel of several u8 fields
_u16 = st.integers(0, 0xFFFE)


@settings(max_examples=200, deadline=None)
@given(
    flags=st.integers(0, 15),
    sequence=st.integers(0, 0xFFFF),
    t_dev_ms=st.integers(0, 0xFFFFFFFF),
    heart_rate_bpm=_u16,
    rr_last=_u16,
    rr_count=_u8,
    ppg_sqi=_u8,
    ecg_quality=_u8,
    motion_level=_u8,
    stress_level=st.integers(0, 3),
    battery_pct=st.integers(0, 100),
    temperature_c100=st.integers(-32768, 0x7FFE),
    rmssd_ms=_u16,
    sdnn_ms=_u16,
    sport_type=st.integers(0, 4),
    sensor_health=st.integers(0, 0x1F),
)
def test_c_and_sdk_agree_on_any_feature_record(fw: ctypes.CDLL, **values: int) -> None:
    raw = _c_feature(fw, dict(values))
    assert raw == consumer.encode_feature(dict(values))
    decoded = consumer.decode_feature(raw)
    assert decoded["version"] == consumer.FEATURE_VERSION
    assert {k: decoded[k] for k in values} == values


def test_a_short_buffer_is_refused_not_truncated(fw: ctypes.CDLL) -> None:
    rec = _Feature()
    fw.synapse_feature_v2_init_na(ctypes.byref(rec))
    buf = ctypes.create_string_buffer(64)
    assert fw.synapse_feature_v2_encode(ctypes.byref(rec), buf, consumer.FEATURE_BYTES - 1) == 0


def test_the_sdk_rejects_a_wrong_version_and_a_wrong_length() -> None:
    good = consumer.encode_feature({})
    with pytest.raises(consumer.ConsumerCodecError, match="version"):
        consumer.decode_feature(bytes([1]) + good[1:])
    with pytest.raises(consumer.ConsumerCodecError, match="expected"):
        consumer.decode_feature(good[:-1])


# --- status ---------------------------------------------------------------------------------------


def test_the_status_record_round_trips(fw: ctypes.CDLL) -> None:
    rec = _Status(1, 0x07, 0x06, 247, 86_400, 3, 11, 22, 33, 44, 0xDEADBEEF)
    buf = ctypes.create_string_buffer(64)
    n = fw.synapse_status_v2_encode(ctypes.byref(rec), buf, 64)
    assert n == consumer.STATUS_BYTES
    assert consumer.decode_status(bytes(buf.raw[:n])) == {
        "version": consumer.STATUS_VERSION,
        "tier": 1,
        "sensor_health": 0x07,
        "flags": 0x06,
        "mtu": 247,
        "uptime_s": 86_400,
        "epoch_wraps": 3,
        "ring_overflow": 11,
        "stale_samples": 22,
        "dropped_frames": 33,
        "read_errors": 44,
        "fw_build": 0xDEADBEEF,
    }
    assert fw.synapse_status_v2_encode(ctypes.byref(rec), buf, n - 1) == 0


# --- stream metadata ------------------------------------------------------------------------------


def test_the_stream_metadata_is_the_manifest_table(fw: ctypes.CDLL) -> None:
    buf = ctypes.create_string_buffer(128)
    n = fw.synapse_stream_metadata_encode(buf, 128)
    rows = consumer.decode_stream_metadata(bytes(buf.raw[:n]))
    expected = json.loads(GATT_V2.read_text(encoding="utf-8"))["stream_metadata"]["streams"]
    assert [
        (r.stream_id, r.sample_format, r.channels, r.unit, r.rate_hz, r.unit_scale) for r in rows
    ] == [
        (s["stream_id"], s["format"], s["channels"], s["unit"], s["rate_hz"], s["unit_scale"])
        for s in expected
    ]
    assert {r.stream_id: r.unit for r in rows}[1] == "uV"  # the ECG is in microvolts
    assert fw.synapse_stream_metadata_encode(buf, n - 1) == 0


def test_the_metadata_agrees_with_the_research_service_stream_table() -> None:
    """The units a client reads must be the scales the research service applies."""
    service = (BLE_DIR / "ble_research_service.c").read_text(encoding="utf-8")
    header = (BLE_DIR / "ble_research_service.h").read_text(encoding="utf-8")
    units = {
        "uV": "SYNAPSE_RESEARCH_UNIT_UV = ",
        "mg": "SYNAPSE_RESEARCH_UNIT_MG = ",
        "mrad_s": "SYNAPSE_RESEARCH_UNIT_MRAD_S = ",
        "cut": "SYNAPSE_RESEARCH_UNIT_CUT = ",
    }
    scale_of = {}
    for unit, needle in units.items():
        scale_of[unit] = int(header.split(needle, 1)[1].split(",", 1)[0])
    streams = json.loads(GATT_V2.read_text(encoding="utf-8"))["stream_metadata"]["streams"]
    for s in streams:
        if s["unit"] in scale_of:
            assert s["unit_scale"] == scale_of[s["unit"]], s["name"]
    assert "SYNAPSE_RAW_FORMAT_S16" in service  # the formats the table declares exist


# --- control point -------------------------------------------------------------------------------


def _apply(fw: ctypes.CDLL, payload: bytes) -> tuple[int, list[tuple[str, int]]]:
    calls: list[tuple[str, int]] = []
    ops = _Ops(
        _SET_TX(lambda on, _ctx: calls.append(("tx", int(on)))),
        _SET_RATE(lambda hz, _ctx: calls.append(("rate", int(hz)))),
        None,
    )
    return fw.synapse_control_apply(payload, len(payload), ctypes.byref(ops)), calls


def test_the_control_point_applies_valid_writes(fw: ctypes.CDLL) -> None:
    assert _apply(fw, consumer.encode_control("start")) == (0, [("tx", 1)])
    assert _apply(fw, consumer.encode_control("stop")) == (0, [("tx", 0)])
    assert _apply(fw, consumer.encode_control("set_feature_rate", 5)) == (0, [("rate", 5)])


def test_the_control_point_refuses_bad_writes_without_side_effects(fw: ctypes.CDLL) -> None:
    errors = json.loads(GATT_V2.read_text(encoding="utf-8"))["control_point"]["errors"]
    assert _apply(fw, bytes([0x7F])) == (errors["bad_opcode"], [])
    assert _apply(fw, bytes([1, 0])) == (errors["bad_length"], [])  # start takes no argument
    assert _apply(fw, bytes([3])) == (errors["bad_length"], [])  # rate needs its argument
    assert _apply(fw, bytes([3, 0])) == (errors["bad_argument"], [])  # 0 Hz
    assert _apply(fw, bytes([3, 6])) == (errors["bad_argument"], [])  # above the 5 Hz cap
    assert _apply(fw, b"")[0] == errors["bad_length"]


def test_the_v1_feature_frame_is_23_bytes_as_compiled(tmp_path: Path) -> None:
    """sizeof() from the real header, against the manifest: the 26 that was documented was wrong."""
    import yaml

    lib = try_load(
        build(
            tmp_path,
            [ROOT / "tests" / "firmware_host" / "host_glue_sizeof.c"],
            extra_includes=[
                str(BLE_DIR),
                str(ROOT / "firmware" / "esp32_band" / "provisioning"),
                str(ROOT / "firmware" / "esp32_band" / "config"),
            ],
        )
    )
    if lib is None:
        pytest.skip("OS policy blocked loading the freshly built library; the compile succeeded")
    lib.host_sizeof_feature_v1.restype = ctypes.c_size_t
    manifest = yaml.safe_load((ROOT / "config" / "hardware.yaml").read_text(encoding="utf-8"))
    assert lib.host_sizeof_feature_v1() == manifest["ble"]["feature_stream"]["payload_bytes"] == 23


# --- the SDK encoder refuses what the wire cannot carry ---------------------------------------------


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"heart_rate_bpm": 0xFFFF}, "sentinel"),  # a real value equal to NA would decode as None
        ({"ppg_sqi": 255}, "sentinel"),
        ({"sequence": 70000}, "outside"),
        ({"sequence": -1}, "outside"),
        ({"battery_pct": 300}, "outside"),
        ({"temperature_c100": 40000}, "outside"),
        ({"not_a_field": 1}, "unknown"),
        ({"version": 1}, "version"),
    ],
)
def test_the_sdk_encoder_raises_its_own_error_for_an_invalid_record(
    values: dict[str, int], message: str
) -> None:
    with pytest.raises(consumer.ConsumerCodecError, match=message):
        consumer.encode_feature(values)


def test_the_sdk_control_encoder_enforces_the_firmware_rate_range() -> None:
    for rate in (0, 6, 300):
        with pytest.raises(consumer.ConsumerCodecError, match="outside"):
            consumer.encode_control("set_feature_rate", rate)
    assert consumer.encode_control("set_feature_rate", 5) == bytes([3, 5])
    doc = json.loads(GATT_V2.read_text(encoding="utf-8"))
    assert doc["feature"]["rate_max_hz"] == consumer.FEATURE_RATE_MAX_HZ


def test_the_control_point_errors_fit_the_att_application_window() -> None:
    errors = json.loads(GATT_V2.read_text(encoding="utf-8"))["control_point"]["errors"]
    assert all(0 < code <= 0x1F for code in errors.values())  # 0x80 + code stays in 0x80..0x9F
