"""Tests for the SYNAPSE-24 session catalog (synapse24.sessions).

Uses synthetic XDF / FIT fixtures so the catalog is tested without any
hardware or dataset download.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse24.sessions import (  # noqa: E402
    index_directory,
    index_fit,
    index_xdf,
    load_catalog,
    save_catalog,
)
from synapse24.utils import write_xdf  # noqa: E402


def _make_xdf(
    path: Path, name: str = "ECG", stype: str = "EEG", fs: float = 500.0, n: int = 1000
) -> None:
    t = np.arange(n) / fs
    write_xdf(
        path,
        streams=[
            {
                "data": np.random.randn(n, 1).astype(np.float64),
                "timestamps": t,
                "info": {
                    "name": name,
                    "type": stype,
                    "nominal_srate": fs,
                    "channel_count": 1,
                    "channel_format": "float32",
                    "source_id": "syn-test",
                },
            }
        ],
    )


def _make_fit(path: Path) -> None:
    """Write a real FIT file with the canonical writer."""
    from synapse_device.fit import DEV_FIELDS, FitRecord, FitWriter

    writer = FitWriter()
    writer.write_file_header()
    writer.write_file_id(serial_number=1)
    writer.write_developer_data_id()
    writer.write_field_descriptions()
    dev = {field.name: field.invalid for field in DEV_FIELDS}
    writer.write_record(FitRecord(timestamp=1000, heart_rate=72, dev=dev))
    writer.finalize(path)


def test_index_xdf_records_stream_metadata(tmp_path: Path) -> None:
    xdf = tmp_path / "session_001.xdf"
    _make_xdf(xdf, name="ECG", stype="EEG", fs=500.0, n=1000)
    rec = index_xdf(xdf)
    assert rec.format == "xdf"
    assert rec.session_id.startswith("syn-")
    assert len(rec.session_id) == 28  # syn- + 24 hex
    assert rec.xdf_valid is True
    assert rec.n_samples_total == 1000
    assert len(rec.streams) == 1
    s = rec.streams[0]
    assert s.name == "ECG"
    assert s.type == "EEG"
    assert s.sampling_rate_hz > 0.0
    assert s.n_samples == 1000
    assert rec.started_at is None  # validate_xdf non fornisce orario di inizio assoluto
    assert rec.ended_at is None
    assert rec.duration_s is not None
    assert rec.duration_s > 0


def test_index_fit_is_indexed_lightly(tmp_path: Path) -> None:
    fit = tmp_path / "run_001.fit"
    _make_fit(fit)
    rec = index_fit(fit)
    assert rec.format == "fit"
    assert rec.fit_valid is True
    assert rec.xdf_valid is None
    assert rec.n_samples_total == 0
    assert rec.session_id.startswith("syn-")


def test_index_fit_rejects_a_file_that_only_looks_like_fit(tmp_path: Path) -> None:
    """A file with the magic but a broken CRC is not a usable session.

    The catalog used to accept any file that started with the FIT magic, so
    the files the project used to write (which no standard parser could read)
    were catalogued as valid sessions.
    """
    fake = tmp_path / "fake.fit"
    header = bytearray(14)
    header[8:12] = b".FIT"
    fake.write_bytes(bytes(header) + b"\x00\x00")

    rec = index_fit(fake)
    assert rec.format == "fit"
    assert rec.fit_valid is False


def test_index_directory_idempotent_and_mixed(tmp_path: Path) -> None:
    _make_xdf(tmp_path / "a.xdf")
    _make_fit(tmp_path / "b.fit")
    (tmp_path / "notes.txt").write_text("not a session")

    cat1 = index_directory(tmp_path)
    cat2 = index_directory(tmp_path)

    assert len(cat1.sessions) == 2
    assert set(cat1.sessions) == set(cat2.sessions)
    # Idempotent: same stable ids across re-scans.
    for sid in cat1.sessions:
        assert cat1.sessions[sid].session_id == cat2.sessions[sid].session_id
        assert cat1.sessions[sid].sha256 == cat2.sessions[sid].sha256
    formats = {rec.format for rec in cat1.sessions.values()}
    assert formats == {"xdf", "fit"}


def test_catalog_save_and_load_roundtrip(tmp_path: Path) -> None:
    _make_xdf(tmp_path / "a.xdf")
    cat = index_directory(tmp_path)
    out = tmp_path / "catalog.json"
    save_catalog(cat, out)
    loaded = load_catalog(out)
    assert len(loaded.sessions) == len(cat.sessions)
    for sid, rec in cat.sessions.items():
        lr = loaded.sessions[sid]
        assert lr.session_id == rec.session_id
        assert lr.format == rec.format
        assert lr.device_serial == rec.device_serial
        assert [(s.name, s.type, s.sampling_rate_hz, s.n_samples) for s in lr.streams] == [
            (s.name, s.type, s.sampling_rate_hz, s.n_samples) for s in rec.streams
        ]


def test_cli_index_list_show(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_xdf(tmp_path / "a.xdf")
    _make_fit(tmp_path / "b.fit")

    from synapse24.sessions import main  # noqa: E402,PLC0415

    # index -> write catalog json
    catalog_file = tmp_path / "catalog.json"
    old_argv = sys.argv
    sys.argv = ["synapse-sessions", "index", str(tmp_path), "--out", str(catalog_file)]
    try:
        rc = main()
    finally:
        sys.argv = old_argv
    assert rc == 0
    assert catalog_file.exists()

    # list
    sys.argv = ["synapse-sessions", "list", str(catalog_file)]
    try:
        rc = main()
    finally:
        sys.argv = old_argv
    assert rc == 0

    # show one session
    cat = load_catalog(catalog_file)
    sid = next(iter(cat.sessions))
    sys.argv = ["synapse-sessions", "show", str(catalog_file), sid]
    try:
        rc = main()
    finally:
        sys.argv = old_argv
    assert rc == 0

    # show unknown -> non-zero
    sys.argv = ["synapse-sessions", "show", str(catalog_file), "syn-nonexistent"]
    try:
        rc = main()
    finally:
        sys.argv = old_argv
    assert rc == 1


def test_module_entrypoint_loads() -> None:
    """Python -m synapse24.sessions deve importare senza dipendenze hardware."""
    from synapse24.sessions import index_directory, main  # noqa: F401

    assert callable(main)
    assert callable(index_directory)


def test_index_json_contains_expected_keys(tmp_path: Path) -> None:
    _make_xdf(tmp_path / "a.xdf")
    cat = index_directory(tmp_path)
    d = cat.to_dict()
    assert d["n_sessions"] == 1
    assert "sessions" in d
    rec = next(iter(d["sessions"].values()))
    for key in (
        "session_id",
        "device_serial",
        "format",
        "size_bytes",
        "started_at",
        "ended_at",
        "duration_s",
        "streams",
        "n_samples_total",
        "xdf_valid",
        "fit_valid",
        "indexed_at",
    ):
        assert key in rec, f"missing key {key}"
