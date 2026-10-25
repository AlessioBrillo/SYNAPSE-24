"""Tests per l'indicizzazione FIT del catalogo (P0-4).

Il catalogo deve leggere un file FIT scritto dal progetto e restituire:
seriale del dispositivo, time_created, primi/ultimi record, durata,
numero di campioni.  Prima dell'audit restituiva sempre stream vuoti,
durata 0 e seriale = nome del file.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from synapse24.sessions.catalog import index_fit

fitdecode = pytest.importorskip("fitdecode", reason="fitdecode is a dev dependency")
from synapse_device.fit import FitRecord, FitWriter, fit_timestamp
from synapse_device.xdf_writer import write_xdf


def _write_fit_with_records(path, n_records: int = 3, serial: int = 12345):
    utc0 = datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC)
    writer = FitWriter()
    writer.write_file_header()
    writer.write_file_id(serial_number=serial, time_created=fit_timestamp(utc0))
    writer.write_developer_data_id()
    writer.write_field_descriptions()
    base = fit_timestamp(utc0)
    for i in range(n_records):
        writer.write_record(
            FitRecord(
                timestamp=base + i * 30,
                heart_rate=70 + i,
                dev={"synapse_rmssd": 500},
            )
        )
    writer.finalize(path)


def test_index_fit_populates_metadata(tmp_path):
    """Un FIT con record produce serial, orari, durata, stream e campioni."""
    fit_path = tmp_path / "session.fit"
    _write_fit_with_records(fit_path, n_records=3, serial=12345)

    rec = index_fit(fit_path)

    assert rec.device_serial == "SYN-0012345"
    assert rec.format == "fit"
    assert rec.fit_valid is True
    assert rec.n_samples_total == 3
    assert len(rec.streams) == 1
    assert rec.streams[0].name == "record"
    assert rec.streams[0].n_samples == 3
    assert rec.started_at is not None
    assert rec.ended_at is not None
    assert rec.duration_s is not None
    assert rec.duration_s >= 59.0  # 3 record a 30 s → 60 s
    # started_at deve essere il 2025-01-15T10:00:00Z (file_id time_created).
    assert rec.started_at.startswith("2025-01-15T10:00:00")


def test_index_fit_falls_back_to_filename_stem(tmp_path):
    """Senza serial leggibile, il device serial deriva dallo stem del file."""
    # Scrive un file che non è FIT valido: l'indexing deve degradare con grazia.
    fit_path = tmp_path / "MY-POD-001.fit"
    fit_path.write_bytes(b"not a fit file")
    rec = index_fit(fit_path)
    assert rec.device_serial == "MY-POD-001"
    assert rec.fit_valid is False


def test_index_fit_empty_records(tmp_path):
    """FIT senza record: durata 0, campioni 0, orari None."""
    fit_path = tmp_path / "empty.fit"
    writer = FitWriter()
    writer.write_file_header()
    writer.write_file_id(serial_number=999, time_created=None)
    writer.write_developer_data_id()
    writer.write_field_descriptions()
    writer.finalize(fit_path)

    rec = index_fit(fit_path)
    assert rec.device_serial == "SYN-000999"
    assert rec.n_samples_total == 0
    assert rec.duration_s is None or rec.duration_s == 0.0
