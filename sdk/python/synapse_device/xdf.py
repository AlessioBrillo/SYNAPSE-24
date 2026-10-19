# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""XDF (Extensible Data Format) I/O for the SDK.

Reads XDF sessions produced by the Synapse pipeline and exposes them as
per-stream numpy arrays with sampling rates and units. Thin wrapper around
``pyxdf`` so the SDK stays dependency-light.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

try:  # pragma: no cover - pyxdf is a project dep but optional for type checks
    import pyxdf
except ImportError:  # pragma: no cover
    pyxdf = None  # type: ignore[assignment]


@dataclass
class XdfStream:
    """One stream inside an XDF session."""

    name: str
    type_: str
    sample_rate: float
    channel_count: int
    timestamps: np.ndarray
    data: np.ndarray
    units: str = ""


@dataclass
class XdfSession:
    """A parsed XDF session."""

    path: Path
    streams: list[XdfStream] = field(default_factory=list)
    n_samples_total: int = 0


def _ensure_pyxdf() -> None:
    if pyxdf is None:
        raise ImportError("pyxdf is required to read XDF files: pip install synapse-device[xdf]")


def read_xdf(path: Path | str) -> XdfSession:
    """Read an XDF file into an :class:`XdfSession`."""
    _ensure_pyxdf()
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"XDF not found: {p}")

    streams, _header = pyxdf.load_xdf(str(p))
    session = XdfSession(path=p)
    for s in streams:
        info = s.get("info", {})
        name = info.get("name", [""])[0] if isinstance(info.get("name"), list) else ""
        type_ = info.get("type", [""])[0] if isinstance(info.get("type"), list) else ""
        nominal = info.get("nominal_srate", [np.nan])
        sample_rate = float(nominal[0]) if nominal else np.nan
        timestamps = np.asarray(s.get("time_stamps", []), dtype=np.float64)
        data = np.asarray(s.get("time_series", []))
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        session.streams.append(
            XdfStream(
                name=name,
                type_=type_,
                sample_rate=sample_rate,
                channel_count=int(data.shape[1]) if data.ndim == 2 else 1,
                timestamps=timestamps,
                data=data,
            )
        )
        session.n_samples_total += int(len(timestamps))
    return session
