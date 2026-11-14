"""The session catalog: what it reads from a FIT/XDF file, and what it reports it could not read."""

from __future__ import annotations

import json
import struct

import numpy as np
import pytest
from synapse_device.xdf_writer import write_xdf

from synapse24.sessions.catalog import (
    _parse_fit_file_id,
    index_directory,
    index_xdf,
    load_catalog,
    save_catalog,
)

FIT_EPOCH_OFFSET = 631_065_600  # seconds from the Unix epoch to the FIT epoch (1989-12-31)


def _header() -> bytes:
    # 14-byte header: size, protocol, profile (2), data size (4, patched later), ".FIT", crc (2)
    return (
        bytes([14, 0x20]) + struct.pack("<H", 2132) + struct.pack("<I", 0) + b".FIT" + b"\x00\x00"
    )


def _definition(local: int, global_num: int, fields, *, big: bool = False, dev=()) -> bytes:
    header = 0x40 | local | (0x20 if dev else 0)
    arch = 1 if big else 0
    out = bytes([header, 0, arch]) + struct.pack(">H" if big else "<H", global_num)
    out += bytes([len(fields)]) + b"".join(bytes(f) for f in fields)
    if dev:
        out += bytes([len(dev)]) + b"".join(bytes(d) for d in dev)
    return out


def _fit(*messages: bytes) -> bytes:
    body = b"".join(messages)
    return _header() + body + b"\x00\x00"


def _file_id(serial: int, created_fit: int, *, big: bool = False) -> bytes:
    fields = [(3, 4, 0x8C), (4, 4, 0x86)]  # serial_number, time_created
    fmt = ">II" if big else "<II"
    return (
        _definition(0, 0, fields, big=big) + bytes([0x00]) + struct.pack(fmt, serial, created_fit)
    )


def _write(tmp_path, name: str, data: bytes):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_file_id_is_found_after_other_messages_and_developer_fields(tmp_path) -> None:
    """The walker used to read the bytes of any other data message as headers."""
    record_def = _definition(1, 20, [(253, 4, 0x86)], dev=[(0, 2, 0)])  # timestamp + dev field
    record = bytes([0x01]) + struct.pack("<I", 1000) + b"\xaa\xbb"  # data: 4 + 2 developer bytes
    compressed = bytes([0x80 | (1 << 5) | 3]) + struct.pack("<I", 1002) + b"\xcc\xdd"
    path = _write(
        tmp_path,
        "late_file_id.fit",
        _fit(record_def, record, compressed, _file_id(123456, 1_000_000)),
    )
    serial, created = _parse_fit_file_id(path)
    assert serial == 123456
    assert created == 1_000_000 + FIT_EPOCH_OFFSET


def test_a_big_endian_file_id_is_read(tmp_path) -> None:
    path = _write(tmp_path, "big.fit", _fit(_file_id(7777, 2_000_000, big=True)))
    assert _parse_fit_file_id(path) == (7777, 2_000_000 + FIT_EPOCH_OFFSET)


def test_a_truncated_or_headerless_file_gives_nothing_instead_of_raising(tmp_path) -> None:
    assert _parse_fit_file_id(_write(tmp_path, "empty.fit", b"")) == (None, None)
    cut = _fit(_file_id(1, 1))[: 14 + 6]
    assert _parse_fit_file_id(_write(tmp_path, "cut.fit", cut)) == (None, None)


def _xdf(path, t0: float) -> None:
    n = 200
    write_xdf(
        path,
        [
            {
                "name": "ECG",
                "type": "ECG",
                "channel_count": 1,
                "sampling_rate": 100.0,
                "channel_format": "float32",
                "channel_names": ["ecg_uv"],
                "channel_units": ["uV"],
                "source_id": "t",
                "data": np.zeros((n, 1)),
                "timestamps": t0 + np.arange(n) / 100.0,
            }
        ],
    )


def test_an_xdf_on_the_wall_clock_gets_a_date_and_a_local_lsl_clock_does_not(tmp_path) -> None:
    _xdf(tmp_path / "wall.xdf", 1_735_689_600.0)  # 2025-01-01T00:00:00Z
    _xdf(tmp_path / "lsl.xdf", 12_345.0)
    wall = index_xdf(tmp_path / "wall.xdf")
    assert wall.started_at is not None
    assert wall.started_at.startswith("2025-01-01T00:00:00")
    assert wall.ended_at is not None
    assert wall.ended_at > wall.started_at
    lsl = index_xdf(tmp_path / "lsl.xdf")
    assert lsl.started_at is None, "a local clock is not a date"
    assert lsl.ended_at is None


def test_an_unreadable_file_is_listed_not_swallowed(tmp_path) -> None:
    (tmp_path / "broken.xdf").write_bytes(b"not an xdf at all")
    _xdf(tmp_path / "good.xdf", 1_735_689_600.0)

    catalog = index_directory(tmp_path)

    assert len(catalog.sessions) == 1
    assert list(catalog.skipped) == ["broken.xdf"], "relative to the root, and not lost"
    assert catalog.skipped["broken.xdf"]  # with the reason
    assert str(tmp_path) not in catalog.skipped["broken.xdf"], "no host path in the catalog"


def test_two_files_with_the_same_bytes_do_not_silently_replace_each_other(tmp_path) -> None:
    _xdf(tmp_path / "a.xdf", 1_735_689_600.0)
    (tmp_path / "b.xdf").write_bytes((tmp_path / "a.xdf").read_bytes())

    catalog = index_directory(tmp_path)

    assert len(catalog.sessions) == 1
    assert next(iter(catalog.sessions.values())).path.endswith("a.xdf"), "the first one is kept"
    assert any(
        path.endswith("b.xdf") and "same content" in why for path, why in catalog.skipped.items()
    )


def test_the_skipped_list_survives_the_round_trip_and_old_catalogs_still_load(tmp_path) -> None:
    (tmp_path / "broken.xdf").write_bytes(b"x")
    catalog = index_directory(tmp_path)
    out = tmp_path / "catalog.json"
    save_catalog(catalog, out)
    assert load_catalog(out).skipped == catalog.skipped

    legacy = json.loads(out.read_text(encoding="utf-8"))
    del legacy["skipped"]  # a catalog written before this field existed
    out.write_text(json.dumps(legacy), encoding="utf-8")
    assert load_catalog(out).skipped == {}


def test_the_session_id_follows_the_content_not_the_path(tmp_path) -> None:
    _xdf(tmp_path / "a.xdf", 1_735_689_600.0)
    sub = tmp_path / "elsewhere"
    sub.mkdir()
    (sub / "renamed.xdf").write_bytes((tmp_path / "a.xdf").read_bytes())
    assert index_xdf(tmp_path / "a.xdf").session_id == index_xdf(sub / "renamed.xdf").session_id


@pytest.mark.parametrize("name", ["a", "b"])
def test_stream_units_are_part_of_the_record(tmp_path, name: str) -> None:
    _xdf(tmp_path / f"{name}.xdf", 1_735_689_600.0)
    assert index_xdf(tmp_path / f"{name}.xdf").streams[0].channel_units == ["uV"]


def test_one_local_clock_stream_does_not_take_the_date_away_from_a_wall_clock_one(tmp_path) -> None:
    n = 100

    def stream(name, t0):
        return {
            "name": name,
            "type": "ECG" if name == "ECG" else "Markers",
            "channel_count": 1,
            "sampling_rate": 100.0,
            "channel_format": "float32",
            "channel_names": ["c"],
            "channel_units": ["uV"],
            "source_id": name,
            "data": np.zeros((n, 1)),
            "timestamps": t0 + np.arange(n) / 100.0,
        }

    write_xdf(tmp_path / "mixed.xdf", [stream("ECG", 1_735_689_600.0), stream("MARK", 42.0)])
    record = index_xdf(tmp_path / "mixed.xdf")
    assert record.started_at is not None
    assert record.started_at.startswith("2025-01-01T00:00:00")


def test_a_non_monotonic_stream_never_ends_before_it_starts(tmp_path) -> None:
    n = 100
    t = 1_735_689_600.0 + np.arange(n) / 100.0
    t[10], t[11] = t[11], t[10]  # two samples out of order
    t[-1] = t[0] - 1.0  # and the last one earlier than the first
    write_xdf(
        tmp_path / "jitter.xdf",
        [
            {
                "name": "ECG",
                "type": "ECG",
                "channel_count": 1,
                "sampling_rate": 100.0,
                "channel_format": "float32",
                "channel_names": ["c"],
                "channel_units": ["uV"],
                "source_id": "j",
                "data": np.zeros((n, 1)),
                "timestamps": t,
            }
        ],
    )
    record = index_xdf(tmp_path / "jitter.xdf")
    assert record.started_at is not None
    assert record.ended_at is not None
    assert record.ended_at >= record.started_at


def test_a_12_byte_fit_header_is_honoured(tmp_path) -> None:
    body = _file_id(4242, 3_000_000)
    header = bytes([12, 0x10]) + struct.pack("<H", 100) + struct.pack("<I", len(body)) + b".FIT"
    path = tmp_path / "old_header.fit"
    path.write_bytes(header + body + b"\x00\x00")
    assert _parse_fit_file_id(path)[0] == 4242
