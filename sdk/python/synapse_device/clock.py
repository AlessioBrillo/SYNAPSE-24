# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""Pod clock model.

The device microsecond counter is 32 bits wide and wraps every
2**32 us (~4295 s, ~71 min). Frames carry the low 32 bits plus a wrap flag;
this module rebuilds a monotonic 64-bit timeline so a 24 h Tier-0 recording
never jumps backwards.

A :class:`PodClockModel` is estimated from NTP-style sync exchanges between
host and pod. An uncalibrated model is not usable: calling
:meth:`PodClockModel.map` before any exchange raises, so the host never claims
synchronization it has not measured.
"""

from __future__ import annotations

from dataclasses import dataclass, field

TS_WRAP_US: int = 1 << 32  # 2^32 us ≈ 4295 s


@dataclass
class ClockExchange:
    """One NTP-style sync exchange between host and pod.

    All timestamps are in microseconds on the respective clock.
    """

    host_send_us: int
    pod_recv_us: int
    pod_send_us: int
    host_recv_us: int

    @property
    def offset_us(self) -> int:
        """Estimated pod-minus-host clock offset (us), half-RTT corrected."""
        return (
            (self.pod_recv_us - self.host_send_us) + (self.pod_send_us - self.host_recv_us)
        ) // 2


@dataclass
class PodClockModel:
    """Maps pod timestamps (32-bit, wrapping) onto a monotonic 64-bit host timeline."""

    exchanges: list[ClockExchange] = field(default_factory=list)
    offset_us: int = 0

    @property
    def calibrated(self) -> bool:
        return len(self.exchanges) > 0

    def add_exchange(self, exchange: ClockExchange) -> None:
        """Fold one sync exchange into the model (latest offset wins)."""
        self.exchanges.append(exchange)
        self.offset_us = exchange.offset_us

    def map(self, pod_ts_us: int, wrapped: bool = False) -> int:
        """Map a pod timestamp to a monotonic 64-bit host timeline (us).

        Raises :class:`RuntimeError` if the model is not yet calibrated.
        """
        if not self.calibrated:
            raise RuntimeError("clock model not calibrated: no sync exchange performed")

        ts = (pod_ts_us & 0xFFFFFFFF) + (TS_WRAP_US if wrapped else 0)
        host_ts = ts + self.offset_us
        return host_ts
