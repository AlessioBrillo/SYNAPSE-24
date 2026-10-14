"""The SOP, executed end to end without hardware.

``docs/hardware/BRINGUP_SOP.md`` describes a procedure: wire, flash,
subscribe, capture, validate, record the ten numbers of
``docs/hardware/PROCUREMENT.md`` into ``data/live_validation/gate_report.json``.
Until now none of that was executable in CI — the pieces existed (decoder,
BLE client, ingestor, gates) but the assembled ``run()`` had never once
executed, which is how ``_collect_results`` came to pass a keyword argument
its own report dataclass did not declare and ``push_sample`` came to assume
a numpy array.

This file drives the real ``Phase1Validator.run()`` against the synthetic pod
through an injected transport, and asserts the SOP's observable contract:
the report exists, the gates judged something, the gate report carries all
ten measurements, and the XDF round-trip is valid.

If the SOP cannot run here, it cannot run on the bench.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_phase1_entry import Phase1Validator  # noqa: E402
from tests.test_validator_clock_gate import (  # noqa: E402
    CONFIG,
    LSL_NOW,
    SyntheticPodTransport,
    _Char,
    _logger,
)

DURATION_S = 2.0


@pytest.fixture
def validator(tmp_path: Path) -> Phase1Validator:
    """A validator wired to the synthetic pod, no bleak, no radio.

    The injected LSL clock is the pod's world clock: both the sync
    exchanges and the pushed timestamps must live in the same domain, or a
    perfectly calibrated pod looks unmeasurable. The pod's session length
    matches the validator's window, so the sample-rate gate compares like
    with like exactly as it does on the bench.
    """
    reset_clock()
    return Phase1Validator(
        CONFIG,
        _logger(),
        transport=SyntheticPodTransport(offset_ms=0.0, duration_s=DURATION_S),
    )


def reset_clock() -> None:
    LSL_NOW[0] = 1_000.0


def run_sync(validator: Phase1Validator, **kwargs):
    """Drive run() with the shared synthetic LSL clock injected."""
    import asyncio

    kwargs.setdefault("lsl_clock_fn", lambda: float(LSL_NOW[0]))
    return asyncio.run(validator.run(**kwargs))


class TestSopEndToEnd:
    """``run()`` from configuration to a written report."""

    def test_run_produces_a_complete_report(self, validator, tmp_path: Path) -> None:
        report = run_sync(validator, duration_s=DURATION_S)

        assert report.run_id
        assert report.gates, "the SOP must evaluate its gates, not skip them"
        assert report.sample_counts["ecg"] > 0, "samples must reach the outlets"
        assert report.tier0_sync_status is not None
        assert report.power_budget is not None
        assert report.xdf_proof.get("all_streams_valid") is True
        assert report.xdf_path

    def test_xdf_survives_the_round_trip(self, validator) -> None:
        report = run_sync(validator, duration_s=DURATION_S)
        path = Path(report.xdf_path)
        assert path.exists(), "the SOP must leave a file behind"

        from synapse24.utils import validate_xdf

        summary = validate_xdf(path)
        flags = summary["validation"]
        assert flags["all_streams_valid"] is True, flags
        assert flags["timestamp_monotonic"] is True, flags
        assert flags["sample_count_match"] is True, flags
        # Every declared stream must actually be in the file.
        names = {s["name"] for s in summary["streams"]}
        assert "SYNAPSE_ECG_T0" in names, names
        assert "SYNAPSE_PPG_T0" in names, names

    def test_gate_report_carries_all_ten_measurements(self, validator, tmp_path: Path) -> None:
        report = run_sync(validator, duration_s=DURATION_S)
        gate_report = validator._build_gate_report(report)

        measurements = gate_report["measurements"]
        # The ten rows of PROCUREMENT.md's measurement SOP, in order.
        expected = [
            "idle_current_ma",
            "tier0_avg_current_ma",
            "ble_throughput_bytes_per_s",
            "dropped_frames",
            "hr_mean_bpm",
            "rmssd_ms",
            "ppg_sqi",
            "ppg_map",
            "clock_residual_drift_ms",
            "xdf_integrity",
            "triage_latency_ms",
        ]
        for name in expected:
            assert name in measurements, f"missing SOP measurement: {name}"

        # Bench measurements are explicitly unfilled, not silently zero.
        assert measurements["idle_current_ma"]["source"] == "bench"
        assert measurements["idle_current_ma"]["value"] is None
        assert measurements["triage_latency_ms"]["source"] == "serial log"

        # Software measurements are real numbers from this run.
        assert measurements["dropped_frames"]["total"] == 0
        assert measurements["hr_mean_bpm"]["value"] is not None
        assert measurements["ppg_sqi"]["value"] is not None
        assert measurements["clock_residual_drift_ms"]["value"] is not None
        assert measurements["xdf_integrity"]["all_streams_valid"] is True

    def test_gate_report_is_json_serialisable(self, validator, tmp_path: Path) -> None:
        report = run_sync(validator, duration_s=DURATION_S)
        payload = json.dumps(validator._build_gate_report(report), indent=2, default=str)
        assert "overall_pass" in payload
        assert "measurements" in payload

    def test_clean_pod_passes_the_assembled_gate(self, validator) -> None:
        """A conforming pod with a clean link must PASS the whole SOP."""
        report = run_sync(validator, duration_s=DURATION_S)
        failed = [g.name for g in report.gates if not g.passed]
        assert failed == [], f"unexpected gate failures: {failed}"
        assert report.overall_pass is True

    def test_lossy_link_fails_the_assembled_gate(self) -> None:
        """A pod dropping frames must FAIL the SOP, and say which gate."""
        validator = Phase1Validator(
            CONFIG,
            _logger(),
            transport=SyntheticPodTransport(offset_ms=0.0, duration_s=DURATION_S),
        )
        # Drop every 7th notification: the decoder's sequence watch sees the
        # hole, and the XDF round-trip must account for it as dropped
        # samples — a recording with a gap is worse than one that stopped.
        transport = validator.transport

        async def lossy_start(char_uuid, callback):  # noqa: ANN001
            index = 0
            for uuid, frame in transport._frames:
                if uuid != char_uuid:
                    continue
                index += 1
                if index % 7 == 0:
                    continue  # the frame never arrives
                callback(_Char(char_uuid), bytearray(frame))

        transport.start_notify = lossy_start
        report = run_sync(validator, duration_s=DURATION_S)
        failed = {g.name for g in report.gates if not g.passed}
        assert "research_stream_zero_loss" in failed, failed
        assert report.overall_pass is False

    def test_run_is_repeatable(self, validator) -> None:
        first = run_sync(validator, duration_s=DURATION_S)
        second = run_sync(validator, duration_s=DURATION_S)
        assert first.run_id != second.run_id
        assert first.overall_pass == second.overall_pass


class TestCliSurface:
    """The documented commands must exist and parse."""

    def test_help_lists_live_hardware_and_soak(self) -> None:
        proc = subprocess.run(  # noqa: S603 - fixed interpreter and script path
            [sys.executable, str(ROOT / "scripts" / "validate_phase1_entry.py"), "--help"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )
        assert proc.returncode == 0
        assert "--live-hardware" in proc.stdout
        assert "--soak" in proc.stdout
        assert "--legacy-format" in proc.stdout

    def test_dry_run_still_passes(self, tmp_path: Path) -> None:
        validator = Phase1Validator(CONFIG, _logger())
        report = run_sync(validator, duration_s=30.0, dry_run=True)
        assert report.overall_pass is True
        assert report.run_id == "dry-run"


# ----------------------------------------------------------------------
# A synchronous façade over the async run(), so the tests read like the SOP.
# ----------------------------------------------------------------------


def _run_sync(self: Phase1Validator, **kwargs):
    import asyncio

    return asyncio.run(self.run(**kwargs))


Phase1Validator.run_sync = _run_sync  # type: ignore[attr-defined]
