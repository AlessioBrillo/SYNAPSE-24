"""Bounded-memory sample spooling for long live recordings.

A 6-hour Tier-0 session at 500 Hz is ~11M ECG samples. Keeping them in
Python lists (one small numpy array per sample) costs more than a gigabyte
of resident memory, and the memory pressure shows up exactly when the bench
is running: the recording slows down, the process is killed, the gate is
never evaluated.

The spool writes the samples to disk as they arrive and keeps only a bounded
tail in memory — the window the quality metrics and the motion gate need.
The full session is read back once, at the end, to write the XDF.
"""

from __future__ import annotations

import struct
import tempfile
from collections import deque
from pathlib import Path

import numpy as np

__all__ = ["StreamSpool", "SpoolManager"]

# Samples kept in memory per stream (the analysis tail).
DEFAULT_TAIL_SAMPLES = 4096


class StreamSpool:
    """Append-only on-disk spool for one stream.

    The samples are stored as raw float64 rows and the timestamps as float64,
    so the whole session can be read back with ``numpy.fromfile`` without
    any per-sample Python object.
    """

    def __init__(
        self,
        directory: Path,
        name: str,
        keep_tail: int = DEFAULT_TAIL_SAMPLES,
    ) -> None:
        self.name = name
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        safe = name.replace("/", "_")
        self._data_path = self.directory / f"{safe}.data.bin"
        self._ts_path = self.directory / f"{safe}.ts.bin"
        self._data_file = open(self._data_path, "wb")  # noqa: SIM115 - long-lived, closed in close()
        self._ts_file = open(self._ts_path, "wb")  # noqa: SIM115 - long-lived, closed in close()
        self._tail: deque[tuple[np.ndarray, float]] = deque(maxlen=keep_tail)
        self.n_samples = 0
        self.n_channels = 0

    def append(self, sample: np.ndarray | list[float], timestamp: float) -> None:
        """Append one sample row and its timestamp."""
        row = np.asarray(sample, dtype=np.float64).ravel()
        self._data_file.write(row.tobytes())
        self._ts_file.write(struct.pack("<d", float(timestamp)))
        self._tail.append((row.copy(), float(timestamp)))
        self.n_samples += 1
        self.n_channels = int(row.size)

    def tail(self) -> tuple[np.ndarray, np.ndarray]:
        """Return the in-memory tail as ``(rows, timestamps)``."""
        if not self._tail:
            return np.zeros((0, 0)), np.zeros(0)
        rows = np.vstack([row for row, _ in self._tail])
        stamps = np.array([ts for _, ts in self._tail], dtype=np.float64)
        return rows, stamps

    def read_all(self) -> tuple[np.ndarray, np.ndarray]:
        """Read the whole session back from disk as ``(rows, timestamps)``."""
        self._data_file.flush()
        self._ts_file.flush()
        rows = np.fromfile(self._data_path, dtype=np.float64)
        stamps = np.fromfile(self._ts_path, dtype=np.float64)
        if self.n_channels > 0 and rows.size:
            rows = rows.reshape(-1, self.n_channels)
        return rows, stamps

    def close(self) -> None:
        """Flush and close the spool files."""
        for handle in (self._data_file, self._ts_file):
            try:
                handle.close()
            except OSError:  # pragma: no cover - defensive
                pass

    def remove(self) -> None:
        """Close the spool and delete its files."""
        self.close()
        for path in (self._data_path, self._ts_path):
            try:
                path.unlink()
            except OSError:  # pragma: no cover - defensive
                pass


class SpoolManager:
    """A spool per stream, living in one temporary directory."""

    def __init__(self, directory: Path | str | None = None) -> None:
        self._owns_dir = directory is None
        self.directory = (
            Path(directory)
            if directory is not None
            else Path(tempfile.mkdtemp(prefix="synapse-spool-"))
        )
        self._spools: dict[str, StreamSpool] = {}

    def spool(self, name: str) -> StreamSpool:
        """Return (and create on demand) the spool of a stream."""
        if name not in self._spools:
            self._spools[name] = StreamSpool(self.directory, name)
        return self._spools[name]

    def append(self, name: str, sample: np.ndarray | list[float], timestamp: float) -> None:
        """Append a sample to the spool of a stream."""
        self.spool(name).append(sample, timestamp)

    def tail(self, name: str) -> tuple[np.ndarray, np.ndarray]:
        """Return the in-memory tail of a stream."""
        if name not in self._spools:
            return np.zeros((0, 0)), np.zeros(0)
        return self._spools[name].tail()

    def read_all(self, name: str) -> tuple[np.ndarray, np.ndarray]:
        """Read a whole stream back from disk."""
        if name not in self._spools:
            return np.zeros((0, 0)), np.zeros(0)
        return self._spools[name].read_all()

    def counts(self) -> dict[str, int]:
        """Number of samples spooled per stream."""
        return {name: spool.n_samples for name, spool in self._spools.items()}

    def reset(self) -> None:
        """Drop every spool and start from an empty session.

        A new session must not inherit the samples of the previous one: the
        gates judge the counts of *this* run.
        """
        for spool in self._spools.values():
            spool.remove()
        self._spools.clear()

    def close(self) -> None:
        """Close every spool and remove the temporary directory."""
        for spool in self._spools.values():
            spool.remove()
        self._spools.clear()
        if self._owns_dir and self.directory.is_dir():
            try:
                self.directory.rmdir()
            except OSError:  # pragma: no cover - defensive
                pass
