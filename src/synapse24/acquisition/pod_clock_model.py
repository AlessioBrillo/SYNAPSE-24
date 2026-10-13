"""Pod→hub clock model: offset and drift from BLE sync exchanges.

Guardian Principle 3 (LSL as the synchronization backbone) requires every
timestamp to live in the ``pylsl.local_clock()`` domain. A pod's samples,
however, are stamped by the pod's own microsecond counter, which (a) starts
at zero at boot, (b) is 32-bit and wraps every ~71 minutes, and (c) runs at
its own frequency error. Pushing raw pod stamps into LSL — as the validator
used to do — puts two clock domains in one file: markers say one thing,
samples say another, and any drift analysis is fiction.

This module bridges the two domains honestly:

* each NTP-style SYNC exchange yields one (pod_time, host_time) point pair
  and a round-trip-time sanity measurement;
* a linear fit ``lsl_seconds = a * pod_seconds + b`` recovers the constant
  offset *and* the frequency error (drift in ppm);
* sample timestamps are mapped through that fit, so the dataset keeps the
  device's true inter-sample spacing instead of the BLE stack's arrival
  jitter, while living in the LSL clock domain.

The fit refuses to guess: with fewer than two usable exchanges nothing is
``is_calibrated`` and callers must not claim a synchronized recording.
Outliers (RTT spikes, stale replies) are rejected before fitting, so one
bad radio second cannot move the model.

All functions are pure and synchronous; the transport (BLE) and the LSL
clock are injected, so the estimator is provable in CI with no hardware.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

__all__ = [
    "ClockExchange",
    "PodClockFit",
    "PodClockModel",
]

# An exchange whose round-trip time exceeds this is radio noise, not a
# measurement: BLE connection intervals and Windows Bluetooth stack latency
# routinely add tens of milliseconds; 200 ms is already generous and still
# bounds the offset error to ~±100 ms before rejection.
DEFAULT_RTT_BUDGET_US = 200_000.0

# Samples further than this from the fit are dropped and the fit is recomputed
# once, so a single wild exchange cannot drag the line.
DEFAULT_RESIDUAL_OUTLIER_US = 20_000.0

# The LSL clock, in seconds. Injected so tests need no liblsl.
default_lsl_clock: Callable[[], float] | None = None


def _lsl_clock() -> float:
    """pylsl.local_clock(), importing lazily so the module stays importable."""
    global default_lsl_clock  # noqa: PLW0603 - module-level injection point
    if default_lsl_clock is None:
        from pylsl import local_clock

        default_lsl_clock = local_clock
    return float(default_lsl_clock())


@dataclass(frozen=True)
class ClockExchange:
    """One NTP-style sync exchange, all fields in microseconds.

    Both directions are kept (``pod_recv``/``pod_send`` and
    ``host_send``/``host_recv``) so the round-trip delay can be subtracted
    from the offset estimate instead of merely assumed small.
    """

    sequence: int
    pod_recv_us: int
    pod_send_us: int
    host_send_us: int
    host_recv_us: int

    @property
    def rtt_us(self) -> float:
        """Round-trip delay: host-observed minus pod turnaround."""
        return (self.host_recv_us - self.host_send_us) - (self.pod_send_us - self.pod_recv_us)

    @property
    def offset_us(self) -> float:
        """Estimated (pod clock − host clock) offset in microseconds."""
        return (
            (self.pod_recv_us - self.host_send_us) + (self.pod_send_us - self.host_recv_us)
        ) / 2.0

    @property
    def pod_mid_us(self) -> float:
        return (self.pod_recv_us + self.pod_send_us) / 2.0

    @property
    def host_mid_us(self) -> float:
        return (self.host_send_us + self.host_recv_us) / 2.0

    def is_usable(self, rtt_budget_us: float = DEFAULT_RTT_BUDGET_US) -> bool:
        """Reject negative or absurd RTTs rather than corrupting the model."""
        return 0.0 <= self.rtt_us < rtt_budget_us


@dataclass(frozen=True)
class PodClockFit:
    """Result of fitting pod time to host (LSL) time."""

    slope: float  # host seconds per pod second; 1.0 + drift
    intercept_s: float  # host seconds at pod time 0
    offset_ms: float  # (pod − host) at the first sample of the fit window, ms
    drift_ppm: float  # pod frequency error, parts per million (pod − host rate)
    residual_us: float  # worst |fit − observed| across the samples used
    n_samples: int
    median_rtt_us: float
    span_s: float  # host time covered by the fit; 0 means drift is unidentified

    def to_lsl_seconds(self, pod_us: float) -> float:
        """Map a device microsecond stamp into the LSL clock domain."""
        return self.slope * (pod_us / 1_000_000.0) + self.intercept_s


@dataclass
class PodClockModel:
    """Linear pod→hub clock model estimated from sync exchanges.

    Args:
        pod_id: Pod identifier, for logging and gate reporting.
        max_samples: Number of recent exchanges kept for the fit.
        rtt_budget_us: Reject exchanges slower than this.
        residual_outlier_us: Drop samples further than this from the first fit
            and refit once, so a wild exchange cannot drag the line.
    """

    pod_id: str
    max_samples: int = 64
    rtt_budget_us: float = DEFAULT_RTT_BUDGET_US
    residual_outlier_us: float = DEFAULT_RESIDUAL_OUTLIER_US
    exchanges: deque[ClockExchange] = field(default_factory=deque)

    @property
    def n_exchanges(self) -> int:
        return len(self.exchanges)

    @property
    def is_calibrated(self) -> bool:
        """True once at least two usable exchanges pin offset and drift."""
        return len(self._usable()) >= 2

    def _usable(self) -> list[ClockExchange]:
        return [ex for ex in self.exchanges if ex.is_usable(self.rtt_budget_us)]

    def record_exchange(self, exchange: ClockExchange) -> bool:
        """Record one sync exchange.

        Returns:
            True when the exchange was usable and kept. An unusable exchange
            is counted out (logged) but never allowed to influence the fit.
        """
        if not exchange.is_usable(self.rtt_budget_us):
            logger.debug(
                "pod %s: rejected sync exchange seq=%s rtt=%.1fms",
                self.pod_id,
                exchange.sequence,
                exchange.rtt_us / 1000.0,
            )
            return False
        self.exchanges.append(exchange)
        while len(self.exchanges) > self.max_samples:
            self.exchanges.popleft()
        return True

    def fit(self) -> PodClockFit | None:
        """Fit host(LSL) time as a linear function of pod time.

        Returns:
            The fit, or ``None`` when fewer than two usable exchanges exist.
            With exactly one exchange the fit degenerates to a pure offset
            (slope 1), which is the honest statement given one measurement.
        """
        usable = self._usable()
        if len(usable) < 2:
            return None

        pod_s, host_s, used = self._reject_offset_outliers(usable)
        slope, intercept_s, residual_us = _linear_fit(pod_s, host_s)
        span_s = (host_s[-1] - host_s[0]) if len(host_s) > 1 else 0.0

        median_rtt = _median([ex.rtt_us for ex in usable])
        # Offset reported at the FIRST sample of the fit window, not at its
        # mean: at the mean the number silently accumulates the drift, which
        # is a separate, explicitly reported quantity. Sign convention
        # matches ClockExchange.offset_us and DriftEstimate: pod − host.
        pod_first_s = pod_s[0] if pod_s else 0.0
        offset_s = pod_first_s - (slope * pod_first_s + intercept_s)

        return PodClockFit(
            slope=slope,
            intercept_s=intercept_s,
            offset_ms=offset_s * 1000.0,
            # drift is the POD's frequency error: how many ppm the pod clock
            # gains per unit time. slope is host-per-pod, so the pod's own
            # rate is its inverse (a pod running fast has slope < 1).
            drift_ppm=(1.0 / slope - 1.0) * 1_000_000.0 if slope else 0.0,
            residual_us=residual_us,
            n_samples=used,
            median_rtt_us=median_rtt,
            span_s=span_s,
        )

    def to_lsl_seconds(self, pod_us: float) -> float | None:
        """Map one device timestamp into the LSL domain, or None if uncalibrated."""
        fit = self.fit()
        return None if fit is None else fit.to_lsl_seconds(pod_us)

    def status(self) -> dict[str, float | int | bool | str]:
        """Observation for gate reports: residual, drift and sample count."""
        fit = self.fit()
        if fit is None:
            return {
                "pod_id": self.pod_id,
                "calibrated": False,
                "n_exchanges": self.n_exchanges,
                "detail": "fewer than two usable sync exchanges",
            }
        return {
            "pod_id": self.pod_id,
            "calibrated": True,
            "n_exchanges": self.n_exchanges,
            "n_samples": fit.n_samples,
            "offset_ms": round(fit.offset_ms, 4),
            "drift_ppm": round(fit.drift_ppm, 3),
            # How much host time the fit spans. A near-zero span means the
            # drift estimate is unidentified (reported as 0 ppm), which is
            # honest for a short window and refines as exchanges accrue.
            "span_s": round(fit.span_s, 3),
            "residual_us": round(fit.residual_us, 3),
            "residual_ms": round(fit.residual_us / 1000.0, 4),
            "median_rtt_ms": round(fit.median_rtt_us / 1000.0, 3),
        }

    # ------------------------------------------------------------------

    def _reject_offset_outliers(
        self, exchanges: list[ClockExchange]
    ) -> tuple[list[float], list[float], int]:
        """Drop exchanges whose offset deviates from the robust median.

        The first rejection attempt in this project compared each sample's
        residual against a *first-pass least-squares fit* — and a single
        grossly wrong sample drags that fit so far that the good samples
        become the outliers and the contaminated fit gets returned. Anchoring
        on the median offset instead is what actually survives one wild
        exchange. Returns (pod_seconds, host_seconds, n_used).
        """
        offsets_us = [ex.pod_mid_us - ex.host_mid_us for ex in exchanges]
        median_offset = _median(offsets_us)

        kept = [
            ex
            for ex in exchanges
            if abs((ex.pod_mid_us - ex.host_mid_us) - median_offset) <= self.residual_outlier_us
        ]
        if len(kept) < 2:
            # Degenerate case: keep the two samples closest to the median so
            # the fit is still an honest two-point estimate.
            ordered = sorted(
                exchanges, key=lambda ex: abs((ex.pod_mid_us - ex.host_mid_us) - median_offset)
            )
            kept = ordered[:2]

        pod_s = [ex.pod_mid_us / 1_000_000.0 for ex in kept]
        host_s = [ex.host_mid_us / 1_000_000.0 for ex in kept]
        return pod_s, host_s, len(kept)


def _linear_fit(x: list[float], y: list[float]) -> tuple[float, float, float]:
    """Plain least squares y = a*x + b; returns (a, b, worst |residual| in µs)."""
    n = len(x)
    if n == 0:
        return 1.0, 0.0, 0.0
    if n == 1:
        return 1.0, y[0] - x[0], 0.0

    mean_x = _mean(x)
    mean_y = _mean(y)
    var_x = sum((xi - mean_x) ** 2 for xi in x)
    if var_x == 0.0:
        # All samples at the same pod time: no slope information.
        return 1.0, mean_y - mean_x, 0.0

    cov_xy = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    slope = cov_xy / var_x
    intercept = mean_y - slope * mean_x
    worst = max(abs(slope * xi + intercept - yi) for xi, yi in zip(x, y))
    return slope, intercept, worst * 1_000_000.0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0
