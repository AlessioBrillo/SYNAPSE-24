# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""FIT file writer/reader for Synapse developer data.

Produces FIT files readable by Garmin Connect, Strava and Averyn's
SynapseFitImporter. Flat scalar developer fields only, per the FIT spec
(no structs), matching the canonical contract in
``contracts/fit_developer_fields.json``:

    synapse_stress   uint8   0=baseline, 1=stress, 2=artifact, 3=unknown
    synapse_sqi      uint8   0-255 (PPG signal quality index)
    synapse_map      uint8   0-255 (motion artifact probability)
    synapse_rmssd    uint16  ms, RMSSD over the window (0xFFFF if invalid)
    synapse_sdnn     uint16  ms, SDNN over the window (0xFFFF if invalid)
    synapse_lf_hf    uint16  x100, LF/HF ratio (0xFFFF if invalid)

Developer field IDs are fixed (0-7) and immutable once a consumer imports them.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

FIT_FILE_HEADER_SIZE = 14
FIT_CRC_SIZE = 2

FIT_BASE_TYPE_UINT8 = 2
FIT_BASE_TYPE_UINT16 = 132
FIT_BASE_TYPE_UINT32 = 134
FIT_BASE_TYPE_ENUM = 0
FIT_BASE_TYPE_STRING = 7

FIT_MESG_FILE_ID = 0
FIT_MESG_DEVICE_INFO = 23
FIT_MESG_RECORD = 20
FIT_MESG_DEVELOPER_DATA_ID = 206
FIT_MESG_FIELD_DESCRIPTION = 207
FIT_MESG_DEVELOPER_DATA = 209

# Developer field IDs (immutable, see contracts/fit_developer_fields.json).
SYNAPSE_DEV_FIELD_STRESS = 0
SYNAPSE_DEV_FIELD_SQI = 1
SYNAPSE_DEV_FIELD_MAP = 2
SYNAPSE_DEV_FIELD_ECG_QUALITY = 3
SYNAPSE_DEV_FIELD_RMSSD = 4
SYNAPSE_DEV_FIELD_SDNN = 5
SYNAPSE_DEV_FIELD_LF_HF = 6

DEV_FIELD_TYPE: dict[int, int] = {
    SYNAPSE_DEV_FIELD_STRESS: FIT_BASE_TYPE_UINT8,
    SYNAPSE_DEV_FIELD_SQI: FIT_BASE_TYPE_UINT8,
    SYNAPSE_DEV_FIELD_MAP: FIT_BASE_TYPE_UINT8,
    SYNAPSE_DEV_FIELD_ECG_QUALITY: FIT_BASE_TYPE_UINT8,
    SYNAPSE_DEV_FIELD_RMSSD: FIT_BASE_TYPE_UINT16,
    SYNAPSE_DEV_FIELD_SDNN: FIT_BASE_TYPE_UINT16,
    SYNAPSE_DEV_FIELD_LF_HF: FIT_BASE_TYPE_UINT16,
}
INVALID_UINT8 = 255
INVALID_UINT16 = 0xFFFF


@dataclass
class FitRecord:
    """One FIT record with Synapse developer data."""

    timestamp: int
    heart_rate: int = 0
    rr_interval_ms: int = 0
    stress_level: int = INVALID_UINT8
    ppg_sqi: int = INVALID_UINT8
    ppg_map: int = INVALID_UINT8
    ecg_quality: int = INVALID_UINT8
    rmssd_ms: int = INVALID_UINT16
    sdnn_ms: int = INVALID_UINT16
    lf_hf: int = INVALID_UINT16


def _crc16(data: bytes, crc: int = 0) -> int:
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA066 if crc & 1 else crc >> 1
    return crc


class FitWriter:
    """Builds a FIT file in memory and writes it to disk."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self._running_crc = 0

    def _write(self, data: bytes) -> None:
        self.buf += data
        self._running_crc = _crc16(data, self._running_crc)

    def _write_u8(self, v: int) -> None:
        self._write(struct.pack("<B", v & 0xFF))

    def _write_u16(self, v: int) -> None:
        self._write(struct.pack("<H", v & 0xFFFF))

    def _write_u32(self, v: int) -> None:
        self._write(struct.pack("<I", v & 0xFFFFFFFF))

    def _header(self, msg_type: int, local_msg_type: int) -> None:
        # definition or data header: bit7=1 definition, bit6=1 data, bits 0-5 local type
        self._write_u8((0xC0 if msg_type == 0 else 0x80) | local_msg_type)

    def write_file_header(self, profile_version: int = 0x0815) -> None:
        self._write_u8(FIT_FILE_HEADER_SIZE)
        self._write_u8(0x10)  # protocol version 1.0
        self._write_u16(profile_version)
        self._write_u32(0)  # data size placeholder
        self._write(b".FIT")
        self._write_u16(0)  # header CRC placeholder

    def write_file_id(self, serial_number: int, manufacturer: int = 0xFFFF) -> None:
        self._header(FIT_MESG_FILE_ID, 0)
        # definition: field 4=serial(u32), 1=manufacturer(u16)
        self._write_u8(5)  # header size
        self._write_u8(0x00)  # arch: little-endian
        self._write_u8(2)  # field count
        # field defs
        for field_num, base_type in ((4, FIT_BASE_TYPE_UINT32), (1, FIT_BASE_TYPE_UINT16)):
            self._write_u8(field_num)
            self._write_u8(base_type)
        self._write_u8(0)  # end of def
        # data
        self._write_u32(serial_number)
        self._write_u16(manufacturer)

    def write_device_info(
        self,
        timestamp: int,
        serial_number: int,
        product: int = 0xFFFF,
    ) -> None:
        self._header(FIT_MESG_DEVICE_INFO, 1)
        self._write_u8(8)  # header size
        self._write_u8(0x00)
        self._write_u8(4)  # field count
        for field_num, base_type in (
            (3, FIT_BASE_TYPE_UINT32),  # timestamp
            (4, FIT_BASE_TYPE_UINT32),  # serial_number
            (2, FIT_BASE_TYPE_UINT16),  # product
            (10, FIT_BASE_TYPE_UINT8),  # battery_level
        ):
            self._write_u8(field_num)
            self._write_u8(base_type)
        self._write_u8(0)
        self._write_u32(timestamp)
        self._write_u32(serial_number)
        self._write_u16(product)
        self._write_u8(100)  # battery 100%

    def write_developer_data_id(self) -> None:
        """Register developer data field IDs (message 206, local msg type 0)."""
        self._header(FIT_MESG_DEVELOPER_DATA_ID, 0)
        self._write_u8(13)  # header size
        self._write_u8(0x00)
        self._write_u8(3)  # field count
        for field_num, base_type in (
            (0, FIT_BASE_TYPE_UINT8),  # developer_data_index
            (1, FIT_BASE_TYPE_UINT16),  # fit_base_type_id
            (3, FIT_BASE_TYPE_UINT8),  # field_definition_number
        ):
            self._write_u8(field_num)
            self._write_u8(base_type)
        self._write_u8(0)

    def write_field_description(self) -> None:
        """Write field description messages (207) for each developer field."""
        names = {
            SYNAPSE_DEV_FIELD_STRESS: b"synapse_stress",
            SYNAPSE_DEV_FIELD_SQI: b"synapse_sqi",
            SYNAPSE_DEV_FIELD_MAP: b"synapse_map",
            SYNAPSE_DEV_FIELD_ECG_QUALITY: b"synapse_ecg_quality",
            SYNAPSE_DEV_FIELD_RMSSD: b"synapse_rmssd",
            SYNAPSE_DEV_FIELD_SDNN: b"synapse_sdnn",
            SYNAPSE_DEV_FIELD_LF_HF: b"synapse_lf_hf",
        }
        for field_id in range(7):  # 0..6
            field_def_number = field_id + 1
            name = names[field_id].ljust(16, b"\x00")[:16]
            base_type = DEV_FIELD_TYPE[field_id]
            self._header(FIT_MESG_FIELD_DESCRIPTION, 1)
            self._write_u8(26)  # header size
            self._write_u8(0x00)
            self._write_u8(4)  # field count
            for field_num, base_type2 in (
                (0, FIT_BASE_TYPE_UINT8),  # developer_data_index
                (1, FIT_BASE_TYPE_UINT16),  # fit_base_type_id
                (2, FIT_BASE_TYPE_STRING),  # field_name
                (3, FIT_BASE_TYPE_UINT8),  # field_definition_number
            ):
                self._write_u8(field_num)
                self._write_u8(base_type2)
            self._write_u8(0)
            self._write_u8(0)  # developer_data_index 0
            self._write_u16(base_type)
            self._write(name)
            self._write_u8(field_def_number)

    def write_record(self, rec: FitRecord) -> None:
        """Write one data record (standard fields + Synapse developer block)."""
        # Standard record: local msg type 0
        self._header(FIT_MESG_RECORD, 0)
        self._write_u8(13)  # header size
        self._write_u8(0x00)
        self._write_u8(4)  # field count
        for field_num, base_type in (
            (253, FIT_BASE_TYPE_UINT32),  # timestamp (dev convenience)
            (0, FIT_BASE_TYPE_UINT8),  # heart_rate
            (1, FIT_BASE_TYPE_UINT16),  # rr_interval_ms
            (254, FIT_BASE_TYPE_UINT8),  # battery_level
        ):
            self._write_u8(field_num)
            self._write_u8(base_type)
        self._write_u8(0)
        self._write_u32(rec.timestamp)
        self._write_u8(rec.heart_rate)
        self._write_u16(rec.rr_interval_ms)
        self._write_u8(100)

        # Developer data: local msg type 1
        self._header(FIT_MESG_RECORD, 1)
        self._write_u8(13)
        self._write_u8(0x00)
        self._write_u8(4)
        for field_num, base_type in (
            (0, FIT_BASE_TYPE_UINT8),  # developer_data_index
            (253, FIT_BASE_TYPE_UINT32),  # timestamp
            (1, FIT_BASE_TYPE_UINT8),  # padding / reserved
            (2, FIT_BASE_TYPE_UINT8),  # first dev field placeholder
        ):
            self._write_u8(field_num)
            self._write_u8(base_type)
        self._write_u8(0)
        self._write_u8(0)  # developer_data_index 0
        self._write_u32(rec.timestamp)
        self._write_u8(0)  # reserved
        # Developer field values in fixed order 0..6
        self._write_dev_field(rec.stress_level, SYNAPSE_DEV_FIELD_STRESS)
        self._write_dev_field(rec.ppg_sqi, SYNAPSE_DEV_FIELD_SQI)
        self._write_dev_field(rec.ppg_map, SYNAPSE_DEV_FIELD_MAP)
        self._write_dev_field(rec.ecg_quality, SYNAPSE_DEV_FIELD_ECG_QUALITY)
        self._write_dev_field(rec.rmssd_ms, SYNAPSE_DEV_FIELD_RMSSD)
        self._write_dev_field(rec.sdnn_ms, SYNAPSE_DEV_FIELD_SDNN)
        self._write_dev_field(rec.lf_hf, SYNAPSE_DEV_FIELD_LF_HF)

    def _write_dev_field(self, value: int, field_id: int) -> None:
        base_type = DEV_FIELD_TYPE[field_id]
        if base_type == FIT_BASE_TYPE_UINT16:
            self._write_u16(value if value != INVALID_UINT16 else INVALID_UINT16)
        else:
            self._write_u8(value if value != INVALID_UINT8 else INVALID_UINT8)

    def finalize(self, out_path: Path | str) -> int:
        """Write header CRC + data size, then emit the file. Returns byte size."""
        total = len(self.buf)
        # data size = total - header(14) - crc(2)
        data_size = total - FIT_FILE_HEADER_SIZE - FIT_CRC_SIZE
        # patch bytes 4..7
        self.buf[4:8] = struct.pack("<I", data_size)
        # header CRC over bytes 0..11
        hcrc = _crc16(bytes(self.buf[:12]))
        # append data CRC over bytes 12..end
        dcrc = _crc16(bytes(self.buf[12:]))
        self._write_u16(hcrc)
        self._write_u16(dcrc)
        out = Path(out_path)
        out.write_bytes(bytes(self.buf))
        return len(self.buf)
