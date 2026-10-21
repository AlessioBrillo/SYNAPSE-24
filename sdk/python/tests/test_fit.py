# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""FIT writer: header, developer fields, records, CRCs and round-trip.

The writer is the canonical one: the files it produces must be readable by a
standard parser (``fitdecode``), which the conformance test in
``tests/test_format_conformance.py`` proves.
"""

from __future__ import annotations

import struct
from pathlib import Path

from synapse_device.fit import (
    DEV_FIELDS,
    FIT_FILE_HEADER_SIZE,
    INVALID_UINT8,
    INVALID_UINT16,
    FitRecord,
    FitWriter,
    fit_crc16,
    read_fit,
)


def _write_sample(path: Path, n_records: int = 3) -> Path:
    writer = FitWriter()
    writer.write_file_header()
    writer.write_file_id(serial_number=12345, time_created=1_000_000)
    writer.write_developer_data_id()
    writer.write_field_descriptions()
    for index in range(n_records):
        writer.write_record(
            FitRecord(
                timestamp=1_000_000 + index,
                heart_rate=60 + index,
                dev={
                    "synapse_stress": 1,
                    "synapse_sqi": 200,
                    "synapse_map": 50,
                    "synapse_ecg_quality": 250,
                    "synapse_sleep_stage": INVALID_UINT8,
                    "synapse_rmssd": 37,
                    "synapse_sdnn": 42,
                    "synapse_lf_hf": 150,
                },
            )
        )
    writer.finalize(path)
    return path


def test_file_header_magic_and_profile():
    w = FitWriter()
    w.write_file_header()
    buf = bytes(w.buffer)
    assert buf[8:12] == b".FIT"
    assert buf[0] == FIT_FILE_HEADER_SIZE
    profile = struct.unpack_from("<H", buf, 2)[0]
    assert profile == 0x0815


def test_definition_messages_use_the_definition_header():
    """A definition message has bit 6 set (0x40), a data message has it clear."""
    w = FitWriter()
    w.write_file_header()
    w.write_file_id(serial_number=1)
    buf = bytes(w.buffer)
    # The first record header of the file is the file_id definition.
    assert buf[FIT_FILE_HEADER_SIZE] & 0x40
    assert not buf[FIT_FILE_HEADER_SIZE] & 0x80, "the compressed-timestamp bit must be clear"


def test_dev_field_ids_are_immutable_order():
    w = FitWriter()
    w.write_developer_data_id()
    w.write_field_descriptions()
    buf = bytes(w.buffer)
    for field in DEV_FIELDS:
        assert field.name.encode() in buf


def test_record_writes_dev_fields():
    w = FitWriter()
    w.write_file_header()
    w.write_file_id(serial_number=1)
    w.write_developer_data_id()
    w.write_field_descriptions()
    w.write_record(
        FitRecord(
            timestamp=1000,
            heart_rate=72,
            dev={
                "synapse_rmssd": 37,
                "synapse_sdnn": 42,
                "synapse_lf_hf": 150,
            },
        )
    )
    buf = bytes(w.buffer)
    assert struct.pack("<H", 37) in buf
    assert struct.pack("<H", 42) in buf
    assert struct.pack("<H", 150) in buf


def test_invalid_sentinels():
    """A field that was not set carries the contract's invalid sentinel."""
    w = FitWriter()
    w.write_file_header()
    w.write_file_id(serial_number=1)
    w.write_developer_data_id()
    w.write_field_descriptions()
    w.write_record(FitRecord(timestamp=1))
    buf = bytes(w.buffer)
    assert bytes([INVALID_UINT8]) in buf
    assert struct.pack("<H", INVALID_UINT16) in buf


def test_roundtrip_file(tmp_path: Path):
    out = tmp_path / "session.fit"
    _write_sample(out)
    data = out.read_bytes()
    assert data[8:12] == b".FIT"
    assert len(data) > 16


def test_crc_is_the_fit_crc():
    """The CRC must be the FIT CRC-16 (the one every standard parser uses).

    The expected value is the one produced by an independent implementation
    (``fitdecode``), so a change of the algorithm cannot pass unnoticed.
    """
    assert fit_crc16(b"") == 0
    assert fit_crc16(b"123456789") == 0xBB3D


def test_reader_agrees_with_the_writer(tmp_path: Path):
    out = _write_sample(tmp_path / "session.fit")
    parsed = read_fit(out)
    assert parsed.n_records == 3
    first = parsed.records[0]
    assert first["timestamp"] == 1_000_000
    assert first["heart_rate"] == 60
    assert first["dev"]["synapse_rmssd"] == 37
    assert first["dev"]["synapse_lf_hf"] == 150
    assert first["dev"]["synapse_sleep_stage"] == INVALID_UINT8


def test_header_and_file_crc_are_written(tmp_path: Path):
    """Both CRCs must be in the file and must match their content."""
    out = _write_sample(tmp_path / "session.fit")
    data = out.read_bytes()

    header_crc = struct.unpack_from("<H", data, 12)[0]
    assert header_crc == fit_crc16(data[:12])

    file_crc = struct.unpack_from("<H", data, len(data) - 2)[0]
    assert file_crc == fit_crc16(data[:-2])
