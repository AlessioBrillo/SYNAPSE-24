"""Tier 0 live-loopback proof — Phase 1 entry gate (no hardware purchase).

Architecture.md decoupled pod/hub 23-31, tiered acquisition 33-43,
energy 55-62, drift risk 92; Roadmap.md Phase 1 138 + LSL-from-day-one 151.

Proves with 0 EUR spend: synthetic Tier 0 at NORMATIVE rates
(ECG 500Hz / PPG 64Hz / IMU 100Hz per config/hardware.yaml forearm hub)
-> LSL-clock timestamps -> real-time HR/HRV + PPG SQI/MAP
-> XDF zero-drop at full 60 s native scale -> Tier-0 sync + 24 h budget.

Normative rates: hardware.yaml is single source of truth.
ESP32Tier0Config + synthetic helper must match it; no silent resampling.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

# Normative Tier 0 rates — config/hardware.yaml forearm hub (single source of truth).
NORM_ECG_HZ = 500
NORM_PPG_HZ = 64
NORM_IMU_HZ = 100


class TestNormativeRates:
    """ESP32 defaults + synthetic helper match hardware.yaml (no silent resampling)."""

    def test_esp32_config_matches_hardware_yaml(self) -> None:
        from synapse24.hardware.esp32_tier0 import ESP32Tier0Config

        cfg = ESP32Tier0Config()
        assert cfg.ecg_sampling_rate == NORM_ECG_HZ, (
            f"ECG {cfg.ecg_sampling_rate} != normative {NORM_ECG_HZ}"
        )
        assert cfg.ppg_sampling_rate == NORM_PPG_HZ, (
            f"PPG {cfg.ppg_sampling_rate} != normative {NORM_PPG_HZ}"
        )
        assert cfg.imu_sampling_rate == NORM_IMU_HZ, (
            f"IMU {cfg.imu_sampling_rate} != normative {NORM_IMU_HZ}"
        )

    def test_synthetic_helper_emits_normative_lengths(self) -> None:
        from synapse24.hardware.esp32_tier0 import create_synthetic_tier0_data

        d = create_synthetic_tier0_data(duration_s=10.0, ecg_hr=72.0, seed=42)
        assert len(d["ecg"]) == 10 * NORM_ECG_HZ
        assert len(d["ppg_red"]) == 10 * NORM_PPG_HZ
        assert len(d["ppg_ir"]) == 10 * NORM_PPG_HZ
        assert len(d["acc_x"]) == 10 * NORM_IMU_HZ


class TestTier0LiveHrHrv:
    """Synthetic 72 bpm Tier 0 yields physiologic HR/HRV + PPG quality at normative rates."""

    def test_hr_within_2bpm_and_ppg_quality_present(self) -> None:
        from synapse24.hardware.esp32_tier0 import create_synthetic_tier0_data
        from synapse24.signal_quality import compute_ecg_quality, compute_ppg_quality

        d = create_synthetic_tier0_data(duration_s=60.0, ecg_hr=72.0, seed=42)
        ecg = np.asarray(d["ecg"], dtype=np.float64)
        ppg_red = np.asarray(d["ppg_red"], dtype=np.float64)

        q_ecg = compute_ecg_quality(ecg, NORM_ECG_HZ)
        hr = float(q_ecg.hrv_metrics.get("hr_mean_bpm", 0.0))
        assert abs(hr - 72.0) <= 2.0, f"HR {hr:.2f} bpm not within 2 bpm of 72"

        qp = compute_ppg_quality(ppg_red, NORM_PPG_HZ, None)
        assert qp["ppg_sqi"] > 0.0
        assert qp["perfusion_index"] > 0.0
        assert 0.0 <= qp["motion_artifact_prob"] <= 1.0


class TestTier0NativeXdfZeroDrop:
    """Full 60 s native payloads survive XDF round-trip with zero drops + tier metadata."""

    def test_60s_native_roundtrip_zero_drop(self, tmp_path: Path) -> None:
        from synapse24.utils import verify_xdf_roundtrip

        rng = np.random.default_rng(42)
        n_ecg = 60 * NORM_ECG_HZ
        n_ppg = 60 * NORM_PPG_HZ
        n_acc = 60 * NORM_IMU_HZ
        streams = [
            {
                "name": "SYNAPSE_ECG_T0",
                "type": "ECG",
                "data": rng.standard_normal((n_ecg, 1)),
                "timestamps": np.arange(n_ecg, dtype=np.float64) / NORM_ECG_HZ,
                "sampling_rate": float(NORM_ECG_HZ),
            },
            {
                "name": "SYNAPSE_PPG_T0",
                "type": "PPG",
                "data": rng.standard_normal((n_ppg, 2)),
                "timestamps": np.arange(n_ppg, dtype=np.float64) / NORM_PPG_HZ,
                "sampling_rate": float(NORM_PPG_HZ),
            },
            {
                "name": "SYNAPSE_ACC_T0",
                "type": "ACC",
                "data": rng.standard_normal((n_acc, 3)),
                "timestamps": np.arange(n_acc, dtype=np.float64) / NORM_IMU_HZ,
                "sampling_rate": float(NORM_IMU_HZ),
            },
        ]
        result = verify_xdf_roundtrip(streams, tmp_path / "tier0_60s.xdf")
        assert result["all_streams_valid"]
        assert result["total_dropped"] == 0
        assert result["n_streams"] == 3
        assert result["total_expected"] == n_ecg + n_ppg + n_acc

    def test_stream_config_carries_tier0(self) -> None:
        from synapse24.utils import StreamConfig, create_stream_info

        cfg = StreamConfig(
            name="SYNAPSE_ECG_T0",
            stream_type="ECG",
            channel_count=1,
            sampling_rate=float(NORM_ECG_HZ),
            tier=0,
        )
        info = create_stream_info(cfg)
        assert info.nominal_srate() == pytest.approx(float(NORM_ECG_HZ))
        assert "tier" in info.as_xml()


class TestLslClockInjection:
    """Guardian Principle 3: signal/tier paths use injectable LSL clock, never wall time."""

    def test_tier_state_machine_accepts_clock_fn(self) -> None:
        from synapse24.acquisition.state_machine import TierStateMachine

        fake_now = [1000.0]
        sm = TierStateMachine(clock_fn=lambda: fake_now[0])
        assert sm.current_tier.name == "T0"
        fake_now[0] = 1001.0
        sm.promote_to_tier1("immobility_detected")
        assert sm.is_tier1()
        evt = sm.transition_history[-1]
        assert evt.timestamp == pytest.approx(1001.0)

    def test_power_budget_accepts_clock_fn(self) -> None:
        from synapse24.acquisition.power_budget import PowerBudgetManager

        fake_now = [2000.0]
        pb = PowerBudgetManager(clock_fn=lambda: fake_now[0])
        fake_now[0] = 2000.0 + 3600.0  # +1 h Tier 0
        consumed = pb.get_consumed_mah()
        assert consumed > 0.0
        assert pb.get_remaining_mah() < pb.usable_mah

    def test_sync_marker_broadcast_uses_injected_clock(self) -> None:
        from synapse24.acquisition.clock_sync import SyncConfig, SyncMarkerManager

        fake_now = [3000.0]
        mgr = SyncMarkerManager(config=SyncConfig(), clock_fn=lambda: fake_now[0])
        marker = mgr.broadcast_sync()
        assert marker.hub_timestamp == pytest.approx(3000.0)


class TestNoHardcodedMacs:
    """Guardian security checklist: no hardcoded BLE MACs in tracked config."""

    def test_hardware_yaml_has_no_hardcoded_mac(self) -> None:
        root = Path(__file__).parent.parent
        text = (root / "config" / "hardware.yaml").read_text(encoding="utf-8")
        macs = re.findall(r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}", text)
        assert macs == [], f"Hardcoded BLE MAC(s) in config/hardware.yaml: {macs}"


class TestTier0Budget24h:
    """Arch 55-62: 3000 mAh hub sustains 24 h Tier 0 with reserve; 2 h Tier 1 affordable."""

    def test_24h_tier0_holds_with_reserve(self) -> None:
        from synapse24.acquisition.power_budget import PowerBudgetManager

        pb = PowerBudgetManager(hub_battery_mah=3000.0, target_lifetime_h=24.0)
        assert pb.usable_mah == pytest.approx(2700.0)
        assert pb.can_afford_tier1(duration_h=2.0) is True
        status = pb.get_status()
        assert status.battery_capacity_mah == pytest.approx(3000.0)
        assert status.can_afford_tier1 is True
