"""Live 2-pod LSL sync gate — Phase 1 entry proof (0 EUR spend).

Architecture.md §23-31 (decoupled pod/hub), §33-43 (tiered acquisition),
§92 (multi-node clock drift); Roadmap.md §138 (Phase 1 entry: live
ECG+PPG+IMU streaming, synchronized in LSL) + §151 (LSL/XDF from day one).

Proves with real liblsl wire (not injected timestamps): forearm T0 pod
(ECG 500Hz / PPG 64Hz / IMU 100Hz per config/hardware.yaml) + head-pod
sync-marker stream -> outlet -> resolve -> inlet -> measured residual
vs. Tier-0 10ms budget -> XDF zero-drop at live scale.

Normative rates: hardware.yaml is single source of truth; no resampling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Normative Tier 0 rates — config/hardware.yaml forearm hub.
NORM_ECG_HZ = 500
NORM_PPG_HZ = 64
NORM_IMU_HZ = 100

pytestmark = pytest.mark.integration


def _run_live_or_skip(**kwargs):
    """Run live 2-pod sync, skipping when the LSL wire is unavailable."""
    from synapse24.acquisition.live_lsl_sync import (
        LiveTwoPodConfig,
        LSLUnavailableError,
        run_live_2pod_sync,
    )

    try:
        return run_live_2pod_sync(LiveTwoPodConfig(**kwargs))
    except LSLUnavailableError as exc:
        pytest.skip(f"LSL loopback unavailable: {exc}")


class TestLiveTwoPodResolveAndStream:
    """Two live outlets resolve and deliver every sample (zero drop)."""

    def test_forearm_head_streams_resolve_and_deliver(self) -> None:
        result = _run_live_or_skip(duration_s=1.0, seed=42)
        assert result["n_streams"] == 3
        assert result["total_dropped"] == 0
        assert result["overall_pass"] is True

    def test_normative_rates_match_hardware_yaml(self) -> None:
        result = _run_live_or_skip(duration_s=1.0, seed=42)
        by_name = {s["name"]: s for s in result["per_stream"]}
        assert by_name["SYNAPSE_ECG_T0"]["sampling_rate"] == float(NORM_ECG_HZ)
        assert by_name["SYNAPSE_PPG_T0"]["sampling_rate"] == float(NORM_PPG_HZ)
        assert by_name["SYNAPSE_ACC_T0"]["sampling_rate"] == float(NORM_IMU_HZ)

    def test_expected_counts_match_native_lengths(self) -> None:
        result = _run_live_or_skip(duration_s=1.0, seed=42)
        by_name = {s["name"]: s for s in result["per_stream"]}
        assert by_name["SYNAPSE_ECG_T0"]["expected"] == 1 * NORM_ECG_HZ
        assert by_name["SYNAPSE_PPG_T0"]["expected"] == 1 * NORM_PPG_HZ
        assert by_name["SYNAPSE_ACC_T0"]["expected"] == 1 * NORM_IMU_HZ


class TestLiveResidualWithinTier0:
    """Measured loopback residual stays inside the Tier-0 10ms budget."""

    def test_residual_within_10ms_on_all_streams(self) -> None:
        result = _run_live_or_skip(duration_s=1.0, seed=42)
        for s in result["per_stream"]:
            residual = s["residual"]
            assert residual["tolerance_ms"] == 10.0
            assert residual["tier_evaluated"] == "T0"
            assert residual["within_10ms_pct"] == 100.0

    def test_tier1_marker_budget_reported_not_silent(self) -> None:
        """T1 1ms marker residual is reported explicitly (BLE limit honest)."""
        result = _run_live_or_skip(duration_s=1.0, seed=42)
        marker = result["tier1_marker"]
        assert marker["tolerance_ms"] == 1.0
        assert marker["tier_evaluated"] == "T1"
        assert 0.0 <= marker["within_1ms_pct"] <= 100.0


class TestLiveToXdfZeroDrop:
    """Live-pulled payloads survive XDF round-trip with zero drops."""

    def test_live_capture_xdf_roundtrip_zero_drop(self, tmp_path: Path) -> None:
        from synapse24.acquisition.live_lsl_sync import LiveTwoPodConfig, run_live_2pod_sync

        try:
            result = run_live_2pod_sync(
                LiveTwoPodConfig(duration_s=1.0, seed=42, xdf_path=tmp_path / "live.xdf")
            )
        except Exception as exc:
            from synapse24.acquisition.live_lsl_sync import LSLUnavailableError

            if isinstance(exc, LSLUnavailableError):
                pytest.skip(f"LSL loopback unavailable: {exc}")
            raise
        xdf_proof = result["xdf_proof"]
        assert xdf_proof["all_streams_valid"]
        assert xdf_proof["total_dropped"] == 0
        assert xdf_proof["n_streams"] == 3


class TestLiveScriptFlag:
    """scripts/validate_sync.py exposes the live 2-pod gate (CI contract)."""

    @staticmethod
    def _load_script():
        import importlib.util

        script = Path(__file__).parent.parent / "scripts" / "validate_sync.py"
        spec = importlib.util.spec_from_file_location("validate_sync_live", script)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_live_2pod_flag_exists(self) -> None:
        module = self._load_script()
        import argparse

        parser = argparse.ArgumentParser()
        module.add_arguments(parser)
        args = parser.parse_args(["--live-2pod", "--live-duration", "1.0"])
        assert args.live_2pod is True
        assert args.live_duration == 1.0

    def test_run_live_validation_entry_point(self) -> None:
        module = self._load_script()
        assert callable(getattr(module, "run_live_validation", None))
