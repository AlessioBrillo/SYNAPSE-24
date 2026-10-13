"""Tests for the pod→hub clock model.

The estimator is the component that decides whether "synchronized" means
anything in the recorded dataset. It is proven here with injected ground
truth: a synthetic pod whose clock has a known offset and a known frequency
error, plus the faults a live BLE link produces (RTT spikes, stale replies,
too few samples). If the model can be fooled, these tests fail.
"""

from __future__ import annotations

import numpy as np
import pytest

from synapse24.acquisition.pod_clock_model import (
    ClockExchange,
    PodClockFit,
    PodClockModel,
)

# Ground truth of the synthetic pod used across the tests.
TRUE_OFFSET_MS = 12.5  # pod clock ahead of host by 12.5 ms
TRUE_DRIFT_PPM = 35.0  # pod clock runs 35 ppm fast
POD_EPOCH_US = 1_700_000_000_000_000  # pod boot epoch (its clock starts here)
# The host clock is expressed at the same scale: the sync exchange is what
# puts both clocks on one timeline, so before it they differ only by the
# injected offset and drift.
HOST_T0_US = POD_EPOCH_US


def make_exchange(
    sequence: int,
    elapsed_s: float,
    *,
    offset_ms: float = TRUE_OFFSET_MS,
    drift_ppm: float = TRUE_DRIFT_PPM,
    host_start_us: float = HOST_T0_US,
    rtt_us: float = 8_000.0,
    pod_turnaround_us: float = 1_500.0,
) -> ClockExchange:
    """Build one exchange for a pod whose clock is offset and drifting.

    The pod's clock follows ``pod(t) = POD_EPOCH + (t - HOST_T0) * (1 + drift)
    + offset``, expressed in microseconds. The exchange geometry (request in
    flight for rtt/2, pod turnaround) then falls out of that map, so the
    estimator is given exact ground truth to recover.
    """

    def pod_at(host_t_us: float) -> float:
        return (
            POD_EPOCH_US
            + (host_t_us - host_start_us) * (1.0 + drift_ppm / 1_000_000.0)
            + offset_ms * 1000.0
        )

    # Symmetric one-way path delay d, so the NTP offset estimator sees the
    # injected offset exactly (an asymmetric geometry would bias it, which
    # is a *radio* property, not a clock property).
    d = (rtt_us - pod_turnaround_us) / 2.0
    host_send = host_start_us + elapsed_s * 1_000_000.0
    host_recv = host_send + rtt_us
    pod_recv = pod_at(host_send + d)
    pod_send = pod_at(host_send + d + pod_turnaround_us)
    return ClockExchange(
        sequence=sequence,
        pod_recv_us=int(pod_recv),
        pod_send_us=int(pod_send),
        host_send_us=int(host_send),
        host_recv_us=int(host_recv),
    )


def feed_session(model: PodClockModel, n: int = 12, interval_s: float = 30.0) -> None:
    """Record a series of exchanges spanning several minutes of host time."""
    for i in range(n):
        model.record_exchange(make_exchange(sequence=i, elapsed_s=i * interval_s))


class TestOffsetAndDriftRecovery:
    """The estimator must recover the truth it was given."""

    def test_offset_is_recovered(self) -> None:
        model = PodClockModel(pod_id="forearm_hub")
        feed_session(model)
        assert model.is_calibrated

        fit = model.fit()
        assert fit is not None
        assert fit.offset_ms == pytest.approx(TRUE_OFFSET_MS, abs=1.0)

    def test_drift_ppm_is_recovered(self) -> None:
        model = PodClockModel(pod_id="forearm_hub")
        feed_session(model)

        fit = model.fit()
        assert fit is not None
        assert fit.drift_ppm == pytest.approx(TRUE_DRIFT_PPM, abs=10.0)

    def test_residual_stays_inside_the_tier1_budget(self) -> None:
        """Worst-case fit error must be sub-millisecond for a clean link."""
        model = PodClockModel(pod_id="forearm_hub")
        feed_session(model)

        fit = model.fit()
        assert fit is not None
        assert fit.residual_us < 1000.0, f"residual {fit.residual_us}us is not Tier-1 grade"

    def test_zero_offset_zero_drift_is_recovered(self) -> None:
        model = PodClockModel(pod_id="perfect_pod")
        for i in range(8):
            model.record_exchange(
                make_exchange(
                    sequence=i,
                    elapsed_s=i * 30.0,
                    offset_ms=0.0,
                    drift_ppm=0.0,
                )
            )
        fit = model.fit()
        assert fit is not None
        assert abs(fit.offset_ms) < 1.0
        assert abs(fit.drift_ppm) < 10.0


class TestCalibrationRefusesToGuess:
    """An uncalibrated model must not claim a synchronized recording."""

    def test_no_exchanges_is_not_calibrated(self) -> None:
        model = PodClockModel(pod_id="forearm_hub")
        assert not model.is_calibrated
        assert model.fit() is None
        assert model.to_lsl_seconds(POD_EPOCH_US) is None
        status = model.status()
        assert status["calibrated"] is False
        assert status["detail"]

    def test_single_exchange_is_not_calibrated(self) -> None:
        model = PodClockModel(pod_id="forearm_hub")
        model.record_exchange(make_exchange(sequence=0, elapsed_s=0.0))
        assert model.n_exchanges == 1
        assert not model.is_calibrated
        assert model.fit() is None


class TestFaultRejection:
    """Radio faults must be rejected before they reach the fit."""

    def test_rtt_spike_exchange_is_rejected(self) -> None:
        model = PodClockModel(pod_id="forearm_hub")
        feed_session(model, n=6)
        honest = model.fit()
        assert honest is not None

        # One exchange with a 5-second round trip must not move the model.
        assert (
            model.record_exchange(make_exchange(sequence=99, elapsed_s=0.0, rtt_us=5_000_000.0))
            is False
        )
        assert model.fit() == honest

    def test_negative_rtt_exchange_is_rejected(self) -> None:
        model = PodClockModel(pod_id="forearm_hub")
        feed_session(model, n=4)
        assert (
            model.record_exchange(make_exchange(sequence=99, elapsed_s=0.0, rtt_us=-5_000.0))
            is False
        )

    def test_outlier_exchange_does_not_drag_the_fit(self) -> None:
        """A usable-but-wrong sample is dropped by the residual filter."""
        model = PodClockModel(pod_id="forearm_hub")
        feed_session(model, n=10)
        clean = model.fit()
        assert clean is not None

        # Plausible RTT, but the pod stamps are 250 ms off: the exchange
        # passes the RTT gate yet must not survive the residual filter.
        wild = make_exchange(sequence=99, elapsed_s=300.0)
        object.__setattr__(wild, "pod_recv_us", wild.pod_recv_us + 250_000)
        object.__setattr__(wild, "pod_send_us", wild.pod_send_us + 250_000)
        model.record_exchange(wild)

        fitted = model.fit()
        assert fitted is not None
        assert abs(fitted.offset_ms - clean.offset_ms) < 5.0, "one wild sample moved the model"


class TestTimestampMapping:
    """Mapped timestamps keep device spacing inside the LSL domain."""

    def test_mapping_preserves_sample_spacing(self) -> None:
        model = PodClockModel(pod_id="forearm_hub")
        feed_session(model)

        # Two pod stamps 20 ms apart (one 50 Hz IMU period).
        t0 = model.to_lsl_seconds(POD_EPOCH_US)
        t1 = model.to_lsl_seconds(POD_EPOCH_US + 20_000)
        assert t0 is not None, "a calibrated model must map"
        assert t1 is not None, "a calibrated model must map"
        assert t1 - t0 == pytest.approx(0.02, abs=1e-5)

    def test_mapping_is_monotonic_across_the_32bit_wrap(self) -> None:
        """DeviceClock rebuilds a 64-bit timeline; the map must not bend it."""
        from synapse24.hardware.research_client import DeviceClock

        model = PodClockModel(pod_id="forearm_hub")
        feed_session(model)

        clock = DeviceClock()
        stamps = [
            clock.extend(ts, flags)
            for ts, flags in (
                (2**32 - 10_000, 0),
                (2**32 - 5_000, 0),
                (10, 0x01),  # first frame after the wrap
                (20_000, 0),
            )
        ]
        mapped = [model.to_lsl_seconds(s) for s in stamps]
        assert all(m is not None for m in mapped)
        diffs = np.diff(np.asarray(mapped, dtype=np.float64))
        assert np.all(diffs > 0), "remapped timestamps must stay strictly increasing"

    def test_mapped_timestamps_live_in_the_lsl_domain(self) -> None:
        """A pod stamp at host time T maps near T, not near the pod epoch."""
        model = PodClockModel(pod_id="forearm_hub")
        feed_session(model)

        fit = model.fit()
        assert fit is not None
        # The last exchange's pod midpoint should map onto its host midpoint:
        # the LSL-domain answer, not a boot-epoch timestamp.
        last = model.exchanges[-1]
        mapped = fit.to_lsl_seconds(last.pod_mid_us)
        assert mapped == pytest.approx(last.host_mid_us / 1_000_000.0, abs=5e-3)


class TestFitWiring:
    """The fit object is the seam the validator and the gate evaluate."""

    def test_fit_object_is_frozen_and_complete(self) -> None:
        model = PodClockModel(pod_id="forearm_hub")
        feed_session(model)
        fit = model.fit()
        assert isinstance(fit, PodClockFit)
        # Frozen value object: the fit is an observation, not a knob.
        with pytest.raises(AttributeError):
            fit.slope = 2.0  # type: ignore[misc]

    def test_status_reports_tier_grade_numbers(self) -> None:
        model = PodClockModel(pod_id="forearm_hub")
        feed_session(model)
        status = model.status()
        assert status["calibrated"] is True
        assert status["residual_ms"] < 1.0
        assert status["median_rtt_ms"] < 50.0
        assert status["n_samples"] >= 2
