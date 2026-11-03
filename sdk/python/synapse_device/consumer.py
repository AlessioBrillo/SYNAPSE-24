"""Consumer contract v2: decode/encode the feature, status, metadata and control frames.

The layouts mirror ``contracts/gatt_v2.json`` (contract 2.1, generated from
``config/hardware.yaml``). ``tests/test_consumer_v2_parity.py`` checks this table
against that JSON and the firmware encoder byte for byte, so the three cannot
drift apart silently.

A field equal to its "not available" sentinel decodes to ``None``: it is never a
measurement, and consumers must not turn it into 0.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final

__all__ = [
    "FEATURE_BYTES",
    "FEATURE_FIELDS",
    "FEATURE_VERSION",
    "SET_TIME_MIN_EPOCH_MS",
    "STATUS_BYTES",
    "STATUS_FIELDS",
    "STATUS_VERSION",
    "ConsumerCodecError",
    "StreamMeta",
    "decode_feature",
    "decode_status",
    "decode_stream_metadata",
    "encode_control",
    "encode_feature",
    "encode_status",
]


class ConsumerCodecError(ValueError):
    """The bytes are not a valid frame of this contract."""


@dataclass(frozen=True)
class _Field:
    name: str
    fmt: str  # struct code, little-endian: B H I h
    offset: int
    na: int | None = None
    unit: str | None = None  # physical value = wire integer * scale, in this unit
    scale: float = 1.0


_FRACTION: Final = 1 / 254  # 0..254 = 0.0..1.0 (255 is the sentinel)

FEATURE_VERSION: Final = 2
FEATURE_BYTES: Final = 27
STATUS_VERSION: Final = 1
STATUS_BYTES: Final = 40
SET_TIME_MIN_EPOCH_MS: Final = 1_577_836_800_000  # 2020-01-01T00:00:00Z

FEATURE_FIELDS: Final[tuple[_Field, ...]] = (
    _Field("version", "B", 0),
    _Field("flags", "B", 1),
    _Field("sequence", "H", 2),
    _Field("t_dev_ms", "I", 4, None, "ms", 1),
    _Field("heart_rate_bpm", "H", 8, 0xFFFF, "bpm", 1),
    _Field("rr_last", "H", 10, 0xFFFF, "s", 1 / 1024),
    _Field("rr_count", "B", 12, 0xFF),
    _Field("ppg_sqi", "B", 13, 0xFF, "fraction", _FRACTION),
    _Field("ecg_quality", "B", 14, 0xFF, "fraction", _FRACTION),
    _Field("motion_level", "B", 15, 0xFF, "fraction", _FRACTION),
    _Field("stress_level", "B", 16, 0xFF),
    _Field("battery_pct", "B", 17, 0xFF, "percent", 1),
    _Field("temperature_c100", "h", 18, 0x7FFF, "degC", 0.01),
    _Field("rmssd_ms", "H", 20, 0xFFFF, "ms", 1),
    _Field("sdnn_ms", "H", 22, 0xFFFF, "ms", 1),
    _Field("sport_type", "B", 24, 0xFF),
    _Field("sensor_health", "B", 25),
    _Field("motion_artifact", "B", 26, 0xFF, "fraction", _FRACTION),
)

STATUS_FIELDS: Final[tuple[_Field, ...]] = (
    _Field("version", "B", 0),
    _Field("tier", "B", 1),
    _Field("sensor_health", "B", 2),
    _Field("flags", "B", 3),
    _Field("mtu", "H", 4),
    _Field("uptime_s", "I", 6, None, "s", 1),
    _Field("epoch_wraps", "H", 10),
    _Field("ring_overflow", "I", 12),
    _Field("stale_samples", "I", 16),
    _Field("dropped_frames", "I", 20),
    _Field("read_errors", "I", 24),
    _Field("fw_build", "I", 28),
    _Field("ppg_skipped", "I", 32),
    _Field("power_dropped", "I", 36),
)

_META_ENTRY: Final = struct.Struct("<BBBBHH")  # stream_id format channels unit rate_hz unit_scale
_UNITS: Final = {0: "count", 1: "uV", 2: "mg", 3: "mrad_s", 4: "cut", 5: "milli"}


def _decode(
    frame: bytes, fields: tuple[_Field, ...], size: int, version: int, what: str
) -> dict[str, int | None]:
    if len(frame) != size:
        raise ConsumerCodecError(f"{what}: expected {size} bytes, got {len(frame)}")
    if frame[0] != version:
        raise ConsumerCodecError(f"{what}: unknown version {frame[0]} (this codec reads {version})")
    out: dict[str, int | None] = {}
    for field in fields:
        (value,) = struct.unpack_from("<" + field.fmt, frame, field.offset)
        # struct reads 'h' as signed, and the sentinels are given in the same domain
        out[field.name] = None if field.na is not None and value == field.na else int(value)
    return out


def decode_feature(frame: bytes) -> dict[str, int | None]:
    """Decode a feature frame; unavailable fields are ``None``."""
    return _decode(bytes(frame), FEATURE_FIELDS, FEATURE_BYTES, FEATURE_VERSION, "feature")


def decode_status(frame: bytes) -> dict[str, int | None]:
    """Decode a status record."""
    return _decode(bytes(frame), STATUS_FIELDS, STATUS_BYTES, STATUS_VERSION, "status")


_RANGE: Final = {
    "B": (0, 0xFF),
    "H": (0, 0xFFFF),
    "I": (0, 0xFFFFFFFF),
    "h": (-0x8000, 0x7FFF),
}
FEATURE_RATE_MAX_HZ: Final = 5


def _encode(
    values: dict[str, int | None],
    fields: tuple[_Field, ...],
    size: int,
    version: int,
    what: str,
) -> bytes:
    known = {f.name: f for f in fields}
    unknown = sorted(set(values) - set(known))
    if unknown:
        raise ConsumerCodecError(f"{what}: unknown field(s) {unknown}")
    if values.get("version") not in (None, version):
        raise ConsumerCodecError(f"{what}: this codec writes version {version} only")
    for name, value in values.items():
        if value is None or name == "version":
            continue
        field = known[name]
        low, high = _RANGE[field.fmt]
        if not low <= value <= high:
            raise ConsumerCodecError(f"{what}: {name}={value} is outside {low}..{high}")
        if field.na is not None and value == field.na:
            raise ConsumerCodecError(
                f"{what}: {name}={value} is the 'not available' sentinel; pass None"
            )
    buf = bytearray(size)
    for field in fields:
        value = values.get(field.name)
        if field.name == "version":
            value = version
        elif value is None:
            value = 0 if field.na is None else field.na
        struct.pack_into("<" + field.fmt, buf, field.offset, value)
    return bytes(buf)


def encode_feature(values: dict[str, int | None]) -> bytes:
    """Encode a feature record; ``None`` (or a missing optional field) becomes its sentinel.

    Raises ``ConsumerCodecError`` for an unknown field, a value outside its type, or a real
    value equal to the field's "not available" sentinel (it would decode back as ``None``).
    """
    return _encode(values, FEATURE_FIELDS, FEATURE_BYTES, FEATURE_VERSION, "feature")


def encode_status(values: dict[str, int | None]) -> bytes:
    """Encode a status record (same rules as :func:`encode_feature`; missing counters are 0)."""
    return _encode(values, STATUS_FIELDS, STATUS_BYTES, STATUS_VERSION, "status")


@dataclass(frozen=True)
class StreamMeta:
    """One row of the Stream Metadata characteristic."""

    stream_id: int
    sample_format: int
    channels: int
    unit: str
    rate_hz: int
    unit_scale: int


def decode_stream_metadata(blob: bytes) -> list[StreamMeta]:
    """Decode ``[count u8] + count * entry``."""
    blob = bytes(blob)
    if not blob:
        raise ConsumerCodecError("stream metadata: empty")
    count, body = blob[0], blob[1:]
    if len(body) != count * _META_ENTRY.size:
        raise ConsumerCodecError(
            f"stream metadata: {count} entries need {count * _META_ENTRY.size} bytes, "
            f"got {len(body)}"
        )
    rows: list[StreamMeta] = []
    for i in range(count):
        sid, fmt, channels, unit, rate, scale = _META_ENTRY.unpack_from(body, i * _META_ENTRY.size)
        if unit not in _UNITS:
            raise ConsumerCodecError(f"stream metadata: unknown unit code {unit}")
        rows.append(StreamMeta(sid, fmt, channels, _UNITS[unit], rate, scale))
    return rows


_OPCODES: Final = {"start": 1, "stop": 2, "set_feature_rate": 3, "set_time": 4}


def encode_control(op: str, arg: int | None = None) -> bytes:
    """Build a control write.

    ``start``, ``stop``, ``set_feature_rate`` (``arg`` = Hz) or ``set_time`` (``arg`` = Unix
    epoch in milliseconds).
    """
    if op not in _OPCODES:
        raise ConsumerCodecError(f"unknown control op {op!r}")
    if op == "set_time":
        if arg is None:
            raise ConsumerCodecError("set_time needs the Unix epoch in milliseconds")
        if not SET_TIME_MIN_EPOCH_MS <= arg < 1 << 64:
            raise ConsumerCodecError(
                f"set_time: {arg} ms is outside {SET_TIME_MIN_EPOCH_MS}..2^64-1 (before 2020?)"
            )
        return bytes((_OPCODES[op],)) + arg.to_bytes(8, "little")
    if op == "set_feature_rate":
        if arg is None:
            raise ConsumerCodecError("set_feature_rate needs a rate in Hz")
        if not 1 <= arg <= FEATURE_RATE_MAX_HZ:
            raise ConsumerCodecError(
                f"set_feature_rate: {arg} Hz is outside 1..{FEATURE_RATE_MAX_HZ}"
            )
        return bytes((_OPCODES[op], arg))
    if arg is not None:
        raise ConsumerCodecError(f"{op} takes no argument")
    return bytes((_OPCODES[op],))
