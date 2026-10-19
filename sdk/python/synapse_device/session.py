# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""Session model for the SDK.

A :class:`Session` holds metadata + raw references (XDF/FIT) for one recording.
It is the in-memory counterpart of what ``synapse index`` prints and
``synapse report`` renders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class SessionStream:
    name: str
    type_: str
    sampling_rate_hz: float
    n_samples: int
    unit: str


@dataclass
class Session:
    session_id: str
    device_serial: str
    source: Path  # raw XDF/FIT file
    format: str  # "xdf" or "fit"
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_s: float | None = None
    streams: list[SessionStream] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    quality: dict = field(default_factory=dict)

    @property
    def window(self) -> dict[str, str | float | None]:
        return {
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_s": self.duration_s,
        }

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "device_serial": self.device_serial,
            "format": self.format,
            "source": str(self.source),
            "window": self.window,
            "streams": [
                {
                    "name": s.name,
                    "type": s.type_,
                    "sampling_rate_hz": s.sampling_rate_hz,
                    "n_samples": s.n_samples,
                    "unit": s.unit,
                }
                for s in self.streams
            ],
            "metrics": self.metrics,
            "quality": self.quality,
        }
