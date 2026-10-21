"""The sample spool must not lose, reorder or duplicate a single sample.

The live validator spools the samples to disk because a 6-hour session at
500 Hz does not fit in memory (and certainly not in Python lists). The
dataset is the product: a spool that loses a sample silently would corrupt
every downstream analysis without any error.
"""

from __future__ import annotations

import numpy as np
import pytest

from synapse24.acquisition.stream_spool import DEFAULT_TAIL_SAMPLES, SpoolManager


def test_every_sample_is_recovered_in_order(tmp_path) -> None:
    manager = SpoolManager(tmp_path)
    expected = np.arange(5000, dtype=np.float64)
    for index, value in enumerate(expected):
        manager.append("ECG", [value, value + 0.5], 1000.0 + index)

    rows, stamps = manager.read_all("ECG")
    assert rows.shape == (5000, 2)
    np.testing.assert_array_equal(rows[:, 0], expected)
    np.testing.assert_array_equal(rows[:, 1], expected + 0.5)
    assert len(stamps) == 5000
    assert stamps[0] == 1000.0
    assert stamps[-1] == 1000.0 + 4999
    assert np.all(np.diff(stamps) > 0)
    manager.cleanup()


def test_the_tail_is_bounded(tmp_path) -> None:
    """Only a bounded window stays in memory."""
    manager = SpoolManager(tmp_path)
    for index in range(DEFAULT_TAIL_SAMPLES + 500):
        manager.append("ECG", [float(index)], float(index))
    rows, stamps = manager.tail("ECG")
    assert len(rows) == DEFAULT_TAIL_SAMPLES
    assert len(stamps) == DEFAULT_TAIL_SAMPLES
    assert rows[-1][0] == float(DEFAULT_TAIL_SAMPLES + 499)
    manager.cleanup()


def test_the_data_survives_the_close(tmp_path) -> None:
    """The report reads the samples back after the handles are released."""
    manager = SpoolManager(tmp_path)
    manager.append("PPG", [1.0, 2.0], 10.0)
    manager.close()
    rows, stamps = manager.read_all("PPG")
    assert rows.shape == (1, 2)
    assert stamps[0] == 10.0
    manager.cleanup()


def test_cleanup_removes_the_files(tmp_path) -> None:
    manager = SpoolManager(tmp_path)
    manager.append("ECG", [1.0], 0.0)
    path = manager.spool("ECG")._data_path  # noqa: SLF001 - the file under test
    assert path.exists()
    manager.cleanup()
    assert not path.exists()


def test_a_new_session_does_not_inherit_the_previous_one(tmp_path) -> None:
    manager = SpoolManager(tmp_path)
    manager.append("ECG", [1.0], 0.0)
    manager.reset()
    manager.append("ECG", [2.0], 1.0)
    rows, _stamps = manager.read_all("ECG")
    assert rows.shape == (1, 1)
    assert rows[0][0] == 2.0
    manager.cleanup()


def test_an_empty_stream_reads_back_empty(tmp_path) -> None:
    manager = SpoolManager(tmp_path)
    rows, stamps = manager.read_all("NEVER_WRITTEN")
    assert rows.size == 0
    assert stamps.size == 0
    manager.cleanup()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
