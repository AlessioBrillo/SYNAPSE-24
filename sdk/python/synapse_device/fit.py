# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""FIT file writer and reader for Synapse developer data.

This is the **canonical** FIT implementation of the project: the host
pipeline (:mod:`synapse24.export`), the export script
(``scripts/export_session_fit.py``) and the stateless worker all use it, so
there is exactly one encoder and one decoder.

The files it produces are standard FIT files: definition messages use the
``0x40`` record header, every definition message carries the global message
number and the field sizes, developer fields are declared with the developer
flag of the record header, and the file carries the header CRC plus the
trailing file CRC. Third-party parsers (``fitdecode``, Garmin Connect) read
them without any Synapse-specific knowledge.

Structure of a Synapse FIT file::

    file_id            (global 0)   device and session identity
    developer_data_id  (global 207) application id and developer data index
    field_description  (global 206) one message per Synapse developer field
    record             (global 20)  timestamp + heart rate + developer fields

The developer values travel **inside the record message**: the record
definition declares them as developer fields, and the record data message
carries their values after the standard ones. That is what the FIT profile
prescribes, and what a standard parser expects.

Developer data layout (flat scalar fields only, per the FIT profile):

===============  =======  ==================================================
field id         type     meaning
===============  =======  ==================================================
0                uint8    ``synapse_stress`` (0=baseline 1=stress 2=artifact 3=unknown)
1                uint8    ``synapse_sqi`` (0-255)
2                uint8    ``synapse_map`` (0-255, motion artifact probability)
3                uint8    ``synapse_ecg_quality`` (0-255)
4                uint8    ``synapse_sleep_stage`` (0=wake 1=light 2=deep 3=rem 4=unknown)
5                uint16   ``synapse_rmssd`` (ms, 0xFFFF = invalid)
6                uint16   ``synapse_sdnn`` (ms, 0xFFFF = invalid)
7                uint16   ``synapse_lf_hf`` (ratio x100, 0xFFFF = invalid)
===============  =======  ==================================================

The contract is ``contracts/fit_developer_fields.json`` in the repository root;
the field ids are immutable once a consumer (Averyn) imports files.

Wire-level note: the FIT *field definition number* of a developer field is the
contract id plus one (``1..8``), because field definition number ``0`` is
reserved by the standard fields of the record message. The contract ids
(``0..7``) are what the C firmware, the export script and the worker exchange.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "DEV_FIELDS",
    "FIT_EPOCH",
    "DevField",
    "FitFile",
    "FitRecord",
    "FitWriter",
    "fit_crc16",
    "fit_timestamp",
    "read_fit",
    "read_fit_records",
]

# ---------------------------------------------------------------------------
# FIT constants
# ---------------------------------------------------------------------------

FIT_FILE_HEADER_SIZE = 14
FIT_CRC_SIZE = 2
FIT_PROFILE_VERSION = 0x0815
FIT_PROTOCOL_VERSION = 0x10

# Base types (FIT profile).
FIT_BASE_TYPE_ENUM = 0x00
FIT_BASE_TYPE_UINT8 = 0x02
FIT_BASE_TYPE_BYTE = 0x0D
FIT_BASE_TYPE_STRING = 0x07
FIT_BASE_TYPE_UINT16 = 0x84
FIT_BASE_TYPE_UINT32 = 0x86
FIT_BASE_TYPE_UINT32Z = 0x8C

# Global message numbers (FIT profile).
FIT_MESG_FILE_ID = 0
FIT_MESG_SESSION = 18
FIT_MESG_RECORD = 20
FIT_MESG_DEVICE_INFO = 23
FIT_MESG_FIELD_DESCRIPTION = 206
FIT_MESG_DEVELOPER_DATA_ID = 207

# Record header bits.
FIT_HEADER_DEFINITION = 0x40
FIT_HEADER_DEVELOPER_DATA = 0x20
FIT_HEADER_LOCAL_TYPE_MASK = 0x0F

# Field numbers of the messages this module writes.
FIT_FIELD_FILE_ID_TYPE = 0
FIT_FIELD_FILE_ID_MANUFACTURER = 1
FIT_FIELD_FILE_ID_PRODUCT = 2
FIT_FIELD_FILE_ID_SERIAL = 3
FIT_FIELD_FILE_ID_TIME_CREATED = 4

FIT_FIELD_DEVICE_INFO_DEVICE_INDEX = 0
FIT_FIELD_DEVICE_INFO_MANUFACTURER = 1
FIT_FIELD_DEVICE_INFO_SERIAL = 3
FIT_FIELD_DEVICE_INFO_PRODUCT = 4
FIT_FIELD_DEVICE_INFO_SOFTWARE = 5
FIT_FIELD_DEVICE_INFO_BATTERY = 32
FIT_FIELD_DEVICE_INFO_TIMESTAMP = 253

FIT_FIELD_SESSION_EVENT = 0
FIT_FIELD_SESSION_EVENT_TYPE = 1
FIT_FIELD_SESSION_START_TIME = 2
FIT_FIELD_SESSION_SPORT = 5
FIT_FIELD_SESSION_ELAPSED = 7
FIT_FIELD_SESSION_TIMER = 8

FIT_FIELD_RECORD_TIMESTAMP = 253
FIT_FIELD_RECORD_HEART_RATE = 3

FIT_FIELD_DESCRIPTION_INDEX = 0
FIT_FIELD_DESCRIPTION_NUMBER = 1
FIT_FIELD_DESCRIPTION_BASE_TYPE = 2
FIT_FIELD_DESCRIPTION_NAME = 3
FIT_FIELD_DESCRIPTION_UNITS = 8

FIT_FIELD_DEV_ID_DEVELOPER_ID = 0
FIT_FIELD_DEV_ID_APPLICATION_ID = 1
FIT_FIELD_DEV_ID_MANUFACTURER = 2
FIT_FIELD_DEV_ID_INDEX = 3
FIT_FIELD_DEV_ID_APPLICATION_VERSION = 4

INVALID_UINT8 = 255
INVALID_UINT16 = 0xFFFF
INVALID_UINT32 = 0xFFFFFFFF

# FIT epoch (1989-12-31 00:00:00 UTC).
FIT_EPOCH = datetime(1989, 12, 31, tzinfo=UTC)

# Manufacturer id used in file_id / device_info: 255 == "development".
FIT_MANUFACTURER_DEVELOPMENT = 255

# Application id written in the developer_data_id message (16 bytes).
APPLICATION_ID = b"SYNAPSE".ljust(16, b"\x00")
APPLICATION_VERSION = 0x53594E41  # "SYNA"
DEVELOPER_DATA_INDEX = 0

# Fixed string sizes, so one definition message can carry every field
# description (FIT fixes the size of a string field in the definition).
FIELD_NAME_SIZE = 32
FIELD_UNITS_SIZE = 16


@dataclass(frozen=True)
class DevField:
    """One Synapse developer field of the canonical FIT contract."""

    field_id: int
    name: str
    base_type: int
    invalid: int
    unit: str = ""

    @property
    def size(self) -> int:
        """Byte size of the field on the wire."""
        return 2 if self.base_type == FIT_BASE_TYPE_UINT16 else 1

    @property
    def definition_number(self) -> int:
        """FIT field definition number used in the record definition."""
        return self.field_id + 1


DEV_FIELDS: tuple[DevField, ...] = (
    DevField(0, "synapse_stress", FIT_BASE_TYPE_ENUM, INVALID_UINT8, "enum"),
    DevField(1, "synapse_sqi", FIT_BASE_TYPE_UINT8, INVALID_UINT8, "index"),
    DevField(2, "synapse_map", FIT_BASE_TYPE_UINT8, INVALID_UINT8, "probability"),
    DevField(3, "synapse_ecg_quality", FIT_BASE_TYPE_UINT8, INVALID_UINT8, "confidence"),
    DevField(4, "synapse_sleep_stage", FIT_BASE_TYPE_ENUM, INVALID_UINT8, "epoch label"),
    DevField(5, "synapse_rmssd", FIT_BASE_TYPE_UINT16, INVALID_UINT16, "ms"),
    DevField(6, "synapse_sdnn", FIT_BASE_TYPE_UINT16, INVALID_UINT16, "ms"),
    DevField(7, "synapse_lf_hf", FIT_BASE_TYPE_UINT16, INVALID_UINT16, "ratio_x100"),
)

DEV_FIELD_BY_ID: dict[int, DevField] = {f.field_id: f for f in DEV_FIELDS}
DEV_FIELD_BY_NAME: dict[str, DevField] = {f.name: f for f in DEV_FIELDS}


def fit_timestamp(dt: datetime) -> int:
    """Convert a datetime to a FIT timestamp (seconds since the FIT epoch)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int((dt - FIT_EPOCH).total_seconds())


# ---------------------------------------------------------------------------
# CRC
# ---------------------------------------------------------------------------

# The FIT CRC-16 (nibble table, as used by the Garmin FIT SDK and by every
# standard FIT parser). A different polynomial silently produces files that
# standard parsers reject, so the table is pinned here.
_FIT_CRC_TABLE: tuple[int, ...] = (
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


def fit_crc16(data: bytes, crc: int = 0) -> int:
    """Compute the FIT CRC-16 of ``data``."""
    for byte in data:
        tmp = _FIT_CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _FIT_CRC_TABLE[byte & 0xF]
        tmp = _FIT_CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _FIT_CRC_TABLE[(byte >> 4) & 0xF]
    return crc & 0xFFFF


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


@dataclass
class FitRecord:
    """One record: the standard fields plus the Synapse developer values."""

    timestamp: int
    heart_rate: int = INVALID_UINT8
    dev: dict[str, int] | None = None

    def dev_value(self, name: str) -> int:
        """Return the developer value for ``name`` (invalid sentinel if unset)."""
        if not self.dev:
            return DEV_FIELD_BY_NAME[name].invalid
        value = self.dev.get(name)
        if value is None:
            return DEV_FIELD_BY_NAME[name].invalid
        return value


class FitWriter:
    """Builds a standard FIT file.

    The writer is explicit: every message goes through a dedicated method and
    the definition messages are emitted once. Data messages reuse the local
    message type of their definition.
    """

    def __init__(
        self,
        manufacturer: int = FIT_MANUFACTURER_DEVELOPMENT,
        product: int = 0xFFFF,
    ) -> None:
        self._buf = bytearray()
        self._manufacturer = manufacturer
        self._product = product
        self._definitions: set[int] = set()
        self._header_written = False

    # -- low level ---------------------------------------------------------
    @property
    def buffer(self) -> bytes:
        """Raw bytes written so far."""
        return bytes(self._buf)

    def _write(self, data: bytes) -> None:
        self._buf += data

    def _u8(self, value: int) -> None:
        self._write(struct.pack("<B", value & 0xFF))

    def _u16(self, value: int) -> None:
        self._write(struct.pack("<H", value & 0xFFFF))

    def _u32(self, value: int) -> None:
        self._write(struct.pack("<I", value & 0xFFFFFFFF))

    def _string(self, value: str, size: int) -> bytes:
        """Encode a fixed-size FIT string field (UTF-8, NUL padded)."""
        raw = value.encode("utf-8")[: size - 1]
        return raw.ljust(size, b"\x00")

    def _definition(
        self,
        local_type: int,
        global_num: int,
        fields: list[tuple[int, int, int]],
        dev_fields: list[tuple[int, int, int]] | None = None,
    ) -> None:
        """Write a definition message.

        ``fields`` are the standard field definitions ``(number, size, base
        type)``; ``dev_fields`` are the developer field definitions
        ``(field definition number, size, developer data index)``. When
        developer fields are present, the record header carries the developer
        data flag and the number of developer fields precedes them.
        """
        header = FIT_HEADER_DEFINITION
        if dev_fields:
            header |= FIT_HEADER_DEVELOPER_DATA
        self._write(struct.pack("<B", header | (local_type & FIT_HEADER_LOCAL_TYPE_MASK)))
        self._u8(0)  # reserved
        self._u8(0)  # architecture: little endian
        self._u16(global_num)
        self._u8(len(fields))
        for field_num, size, base_type in fields:
            self._u8(field_num)
            self._u8(size)
            self._u8(base_type)
        if dev_fields:
            self._u8(len(dev_fields))
            for field_num, size, dev_index in dev_fields:
                self._u8(field_num)
                self._u8(size)
                self._u8(dev_index)
        self._definitions.add(local_type)

    def _data_header(self, local_type: int, developer_data: bool = False) -> None:
        header = local_type & FIT_HEADER_LOCAL_TYPE_MASK
        if developer_data:
            header |= FIT_HEADER_DEVELOPER_DATA
        self._write(struct.pack("<B", header))

    # -- messages ----------------------------------------------------------
    def write_file_header(self) -> None:
        """Write the 14-byte file header (CRC patched in :meth:`finalize`)."""
        if self._header_written:
            return
        self._u8(FIT_FILE_HEADER_SIZE)
        self._u8(FIT_PROTOCOL_VERSION)
        self._u16(FIT_PROFILE_VERSION)
        self._u32(0)  # data size, patched in finalize()
        self._write(b".FIT")
        self._u16(0)  # header CRC, patched in finalize()
        self._header_written = True

    def write_file_id(self, serial_number: int, time_created: int | None = None) -> None:
        """Write the file_id message (global 0)."""
        local = 0
        self._definition(
            local,
            FIT_MESG_FILE_ID,
            [
                (FIT_FIELD_FILE_ID_TYPE, 1, FIT_BASE_TYPE_ENUM),
                (FIT_FIELD_FILE_ID_MANUFACTURER, 2, FIT_BASE_TYPE_UINT16),
                (FIT_FIELD_FILE_ID_PRODUCT, 2, FIT_BASE_TYPE_UINT16),
                (FIT_FIELD_FILE_ID_SERIAL, 4, FIT_BASE_TYPE_UINT32Z),
                (FIT_FIELD_FILE_ID_TIME_CREATED, 4, FIT_BASE_TYPE_UINT32),
            ],
        )
        self._data_header(local)
        self._u8(4)  # file type: activity
        self._u16(self._manufacturer)
        self._u16(self._product)
        self._u32(serial_number)
        self._u32(time_created if time_created is not None else 0)

    def write_device_info(
        self,
        timestamp: int,
        serial_number: int,
        battery_level: int = INVALID_UINT8,
        software_version: int = 0,
    ) -> None:
        """Write the device_info message (global 23)."""
        local = 1
        self._definition(
            local,
            FIT_MESG_DEVICE_INFO,
            [
                (FIT_FIELD_DEVICE_INFO_DEVICE_INDEX, 1, FIT_BASE_TYPE_UINT8),
                (FIT_FIELD_DEVICE_INFO_MANUFACTURER, 2, FIT_BASE_TYPE_UINT16),
                (FIT_FIELD_DEVICE_INFO_SERIAL, 4, FIT_BASE_TYPE_UINT32Z),
                (FIT_FIELD_DEVICE_INFO_PRODUCT, 2, FIT_BASE_TYPE_UINT16),
                (FIT_FIELD_DEVICE_INFO_SOFTWARE, 2, FIT_BASE_TYPE_UINT16),
                (FIT_FIELD_DEVICE_INFO_BATTERY, 1, FIT_BASE_TYPE_UINT8),
                (FIT_FIELD_DEVICE_INFO_TIMESTAMP, 4, FIT_BASE_TYPE_UINT32),
            ],
        )
        self._data_header(local)
        self._u8(0)  # device index
        self._u16(self._manufacturer)
        self._u32(serial_number)
        self._u16(self._product)
        self._u16(software_version)
        self._u8(battery_level)
        self._u32(timestamp)

    def write_session(self, start_time: int, elapsed_s: int, sport: int = 0) -> None:
        """Write the session message (global 18)."""
        local = 2
        self._definition(
            local,
            FIT_MESG_SESSION,
            [
                (FIT_FIELD_SESSION_EVENT, 1, FIT_BASE_TYPE_ENUM),
                (FIT_FIELD_SESSION_EVENT_TYPE, 1, FIT_BASE_TYPE_ENUM),
                (FIT_FIELD_SESSION_START_TIME, 4, FIT_BASE_TYPE_UINT32),
                (FIT_FIELD_SESSION_SPORT, 1, FIT_BASE_TYPE_ENUM),
                (FIT_FIELD_SESSION_ELAPSED, 4, FIT_BASE_TYPE_UINT32),
                (FIT_FIELD_SESSION_TIMER, 4, FIT_BASE_TYPE_UINT32),
            ],
        )
        self._data_header(local)
        self._u8(0)  # event: timer
        self._u8(0)  # event type: stop
        self._u32(start_time)
        self._u8(sport)
        self._u32(elapsed_s)
        self._u32(elapsed_s)

    def write_developer_data_id(self) -> None:
        """Declare the developer data index (global 207).

        Must be written before the field descriptions: a parser resolves the
        developer data index from this message.
        """
        local = 3
        self._definition(
            local,
            FIT_MESG_DEVELOPER_DATA_ID,
            [
                (FIT_FIELD_DEV_ID_DEVELOPER_ID, 16, FIT_BASE_TYPE_BYTE),
                (FIT_FIELD_DEV_ID_APPLICATION_ID, 16, FIT_BASE_TYPE_BYTE),
                (FIT_FIELD_DEV_ID_MANUFACTURER, 2, FIT_BASE_TYPE_UINT16),
                (FIT_FIELD_DEV_ID_INDEX, 1, FIT_BASE_TYPE_UINT8),
                (FIT_FIELD_DEV_ID_APPLICATION_VERSION, 4, FIT_BASE_TYPE_UINT32),
            ],
        )
        self._data_header(local)
        self._write(b"\x00" * 16)  # developer id (unused)
        self._write(APPLICATION_ID)
        self._u16(self._manufacturer)
        self._u8(DEVELOPER_DATA_INDEX)
        self._u32(APPLICATION_VERSION)

    def write_field_descriptions(self) -> None:
        """Describe every developer field (global 206)."""
        local = 4
        self._definition(
            local,
            FIT_MESG_FIELD_DESCRIPTION,
            [
                (FIT_FIELD_DESCRIPTION_INDEX, 1, FIT_BASE_TYPE_UINT8),
                (FIT_FIELD_DESCRIPTION_NUMBER, 1, FIT_BASE_TYPE_UINT8),
                (FIT_FIELD_DESCRIPTION_BASE_TYPE, 1, FIT_BASE_TYPE_UINT8),
                (FIT_FIELD_DESCRIPTION_NAME, FIELD_NAME_SIZE, FIT_BASE_TYPE_STRING),
                (FIT_FIELD_DESCRIPTION_UNITS, FIELD_UNITS_SIZE, FIT_BASE_TYPE_STRING),
            ],
        )
        for dev_field in DEV_FIELDS:
            self._data_header(local)
            self._u8(DEVELOPER_DATA_INDEX)
            self._u8(dev_field.definition_number)
            self._u8(dev_field.base_type)
            self._write(self._string(dev_field.name, FIELD_NAME_SIZE))
            self._write(self._string(dev_field.unit, FIELD_UNITS_SIZE))

    def _write_record_definition(self) -> None:
        if 5 in self._definitions:
            return
        self._definition(
            5,
            FIT_MESG_RECORD,
            [
                (FIT_FIELD_RECORD_TIMESTAMP, 4, FIT_BASE_TYPE_UINT32),
                (FIT_FIELD_RECORD_HEART_RATE, 1, FIT_BASE_TYPE_UINT8),
            ],
            [(f.definition_number, f.size, DEVELOPER_DATA_INDEX) for f in DEV_FIELDS],
        )

    def write_record(self, record: FitRecord) -> None:
        """Write a record: timestamp, heart rate and the Synapse developer values."""
        self._write_record_definition()
        self._data_header(5, developer_data=True)
        self._u32(record.timestamp)
        self._u8(record.heart_rate)
        for dev_field in DEV_FIELDS:
            value = record.dev_value(dev_field.name)
            if dev_field.base_type == FIT_BASE_TYPE_UINT16:
                self._u16(value)
            else:
                self._u8(value)

    # -- finalisation ------------------------------------------------------
    def finalize(self, out_path: Path | str) -> int:
        """Patch the header CRC and data size, append the file CRC, write the file."""
        if not self._header_written:
            self.write_file_header()
        data_size = len(self._buf) - FIT_FILE_HEADER_SIZE
        self._buf[4:8] = struct.pack("<I", data_size)
        header_crc = fit_crc16(bytes(self._buf[:12]))
        self._buf[12:14] = struct.pack("<H", header_crc)
        # The file CRC covers every byte of the file except the two CRC bytes
        # themselves (header included).
        file_crc = fit_crc16(bytes(self._buf))
        self._buf += struct.pack("<H", file_crc)
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(bytes(self._buf))
        return len(self._buf)


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


@dataclass
class FitFile:
    """The content of a FIT file, as far as this project needs it."""

    path: Path
    records: list[dict[str, Any]] = field(default_factory=list)
    developer_values: list[dict[str, int]] = field(default_factory=list)
    field_names: dict[int, str] = field(default_factory=dict)
    field_types: dict[int, int] = field(default_factory=dict)
    n_records: int = 0


@dataclass
class _LocalDef:
    """A parsed definition message."""

    global_num: int
    fields: list[tuple[int, int, int]]
    dev_fields: list[tuple[int, int, int]]


def _parse_definition(data: bytes, pos: int, has_dev: bool) -> tuple[_LocalDef, int]:
    """Parse a definition message body; returns the definition and the new position."""
    _reserved, _arch, global_num, n_fields = struct.unpack_from("<BBHB", data, pos)
    pos += 5
    fields: list[tuple[int, int, int]] = []
    for _ in range(n_fields):
        field_num, size, base_type = struct.unpack_from("<BBB", data, pos)
        pos += 3
        fields.append((field_num, size, base_type))
    dev_fields: list[tuple[int, int, int]] = []
    if has_dev:
        n_dev = data[pos]
        pos += 1
        for _ in range(n_dev):
            field_num, size, dev_index = struct.unpack_from("<BBB", data, pos)
            pos += 3
            dev_fields.append((field_num, size, dev_index))
    return _LocalDef(global_num, fields, dev_fields), pos


def _read_values(
    data: bytes, pos: int, fields: list[tuple[int, int, int]]
) -> tuple[list[int], dict[int, str], int]:
    """Read the values of a data message.

    Returns the integer values, the string values (keyed by field position)
    and the new read position.
    """
    values: list[int] = []
    strings: dict[int, str] = {}
    for position, (_field_num, size, base_type) in enumerate(fields):
        if base_type in (FIT_BASE_TYPE_UINT8, FIT_BASE_TYPE_ENUM):
            values.append(data[pos])
            pos += 1
        elif base_type == FIT_BASE_TYPE_UINT16:
            values.append(struct.unpack_from("<H", data, pos)[0])
            pos += 2
        elif base_type in (FIT_BASE_TYPE_UINT32, FIT_BASE_TYPE_UINT32Z):
            values.append(struct.unpack_from("<I", data, pos)[0])
            pos += 4
        elif base_type == FIT_BASE_TYPE_STRING:
            strings[position] = (
                data[pos : pos + size].split(b"\x00", 1)[0].decode("utf-8", "replace")
            )
            values.append(0)
            pos += size
        else:  # byte arrays and anything else: skipped by its declared size
            values.append(0)
            pos += size
    return values, strings, pos


def _read_dev_values(
    data: bytes,
    pos: int,
    dev_fields: list[tuple[int, int, int]],
    field_types: dict[int, int],
) -> tuple[list[int], int]:
    """Read the developer field values of a data message.

    The base type of a developer field is declared in the field description
    message, not in the record definition (which only carries the size and
    the developer data index).
    """
    values: list[int] = []
    for field_num, size, _dev_index in dev_fields:
        base_type = field_types.get(field_num)
        if base_type is None:
            base_type = FIT_BASE_TYPE_UINT16 if size == 2 else FIT_BASE_TYPE_UINT8
        if base_type == FIT_BASE_TYPE_UINT16:
            values.append(struct.unpack_from("<H", data, pos)[0])
            pos += 2
        elif base_type in (FIT_BASE_TYPE_UINT32, FIT_BASE_TYPE_UINT32Z):
            values.append(struct.unpack_from("<I", data, pos)[0])
            pos += 4
        else:
            values.append(data[pos])
            pos += 1
    return values, pos


def read_fit(path: Path | str) -> FitFile:
    """Read a FIT file written by this project (records + developer values).

    The reader is strict about the structure and tolerant about the messages
    it does not know: unknown global messages are skipped using the sizes
    declared in their definition.
    """
    p = Path(path)
    data = p.read_bytes()
    if len(data) < FIT_FILE_HEADER_SIZE + FIT_CRC_SIZE or data[8:12] != b".FIT":
        raise ValueError(f"not a FIT file: {p}")

    result = FitFile(path=p)
    definitions: dict[int, _LocalDef] = {}

    pos = FIT_FILE_HEADER_SIZE
    end = len(data) - FIT_CRC_SIZE  # the trailing CRC is not a message
    while pos < end:
        header = data[pos]
        pos += 1
        is_definition = bool(header & FIT_HEADER_DEFINITION)
        has_dev = bool(header & FIT_HEADER_DEVELOPER_DATA)
        local_type = header & FIT_HEADER_LOCAL_TYPE_MASK
        if is_definition:
            definition, pos = _parse_definition(data, pos, has_dev)
            definitions[local_type] = definition
            continue

        data_def = definitions.get(local_type)
        if data_def is None:
            raise ValueError(f"data message without definition (local type {local_type}) in {p}")
        values, strings, pos = _read_values(data, pos, data_def.fields)

        if data_def.global_num == FIT_MESG_FIELD_DESCRIPTION:
            number = values[1] if len(values) > 1 else 0
            result.field_names[number] = strings.get(3, f"field_{number}")
            if len(values) > 2:
                result.field_types[number] = values[2]
        elif data_def.global_num == FIT_MESG_RECORD:
            dev_values, pos = _read_dev_values(data, pos, data_def.dev_fields, result.field_types)
            mapping: dict[str, int] = {}
            for (field_num, _size, _dev_index), value in zip(data_def.dev_fields, dev_values):
                mapping[result.field_names.get(field_num, f"field_{field_num}")] = value
            result.records.append(
                {
                    "timestamp": values[0] if values else 0,
                    "heart_rate": values[1] if len(values) > 1 else 0,
                    "dev": mapping,
                }
            )
            result.developer_values.append(mapping)
            result.n_records += 1

    return result


def read_fit_records(path: Path | str) -> list[dict[str, Any]]:
    """Return the records of a FIT file as ``{"timestamp", "heart_rate", "dev"}`` dicts."""
    return read_fit(path).records
