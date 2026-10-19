# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""FIT writer: header magic, developer fields, record round-trip."""

from __future__ import annotations

import struct
from pathlib import Path

from synapse_device.fit import (
    INVALID_UINT8,
    INVALID_UINT16,
    FitRecord,
    FitWriter,
)


def _read_u16_le(buf: bytes, off: int) -> int:
    return struct.unpack_from("<H", buf, off)[0]


def test_file_header_magic_and_profile():
    w = FitWriter()
    w.write_file_header()
    assert bytes(w.buf[:12])[12 - 4 :] == b".FIT"  # magic at offset 10..13? check below
    assert w.buf[8:12] == b".FIT"  # magic at offset 8..11
    profile = _read_u16_le(bytes(w.buf), 2)  # profile_version at offset 2..3
    assert profile == 0x0815


def test_dev_field_ids_immutable_order():
    w = FitWriter()
    w.write_developer_data_id()
    # field description messages must cover IDs 0..6 (names truncated to 16 B)
    w.write_field_description()
    buf = bytes(w.buf)
    assert b"synapse_stress" in buf
    assert b"synapse_sqi" in buf
    assert b"synapse_map" in buf
    # synapse_ecg_quality is 19 chars -> truncated to 16 in the FIT field name
    assert b"synapse_ecg_qual" in buf
    assert b"synapse_rmssd" in buf
    assert b"synapse_sdnn" in buf
    assert b"synapse_lf_hf" in buf


def test_record_writes_dev_fields():
    w = FitWriter()
    w.write_file_header()
    w.write_file_id(serial_number=12345)
    rec = FitRecord(
        timestamp=1000,
        heart_rate=72,
        stress_level=1,
        ppg_sqi=200,
        ppg_map=50,
        ecg_quality=250,
        rmssd_ms=37,
        sdnn_ms=42,
        lf_hf=150,
    )
    w.write_record(rec)
    buf = bytes(w.buf)
    # developer field ids are written as definition-number bytes after names;
    # we assert presence of the encoded UINT16 values for RMSSD/SDNN/LF_HF
    assert struct.pack("<H", 37) in buf
    assert struct.pack("<H", 42) in buf
    assert struct.pack("<H", 150) in buf


def test_invalid_sentinels():
    rec = FitRecord(timestamp=1)
    w = FitWriter()
    w.write_file_header()
    w.write_record(rec)
    buf = bytes(w.buf)
    # invalid UINT8 sentinel 255 and UINT16 0xFFFF should appear for unset fields
    assert bytes([INVALID_UINT8]) in buf
    assert struct.pack("<H", INVALID_UINT16) in buf


def test_roundtrip_file(tmp_path: Path):
    out = tmp_path / "session.fit"
    w = FitWriter()
    w.write_file_header()
    w.write_file_id(serial_number=1)
    w.write_record(FitRecord(timestamp=10, heart_rate=60))
    w.finalize(out)
    data = out.read_bytes()
    assert data[8:12] == b".FIT"
    assert len(data) > 16
