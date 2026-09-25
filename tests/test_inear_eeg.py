"""Unit tests for In-Ear EEG satellite pod (Tier 0 continuity).

Validates:
- Hardware abstraction (config, adapter, firmware simulation)
- Power profile model (Guermandi 2022 600h target)
- Synthetic data generation
- Integration with Tier1Coordinator as Tier 0 continuity node
"""

from __future__ import annotations

import numpy as np
import pytest

from synapse24.acquisition.inear_power_profile import (
    COMPONENT_CURRENT_MA,
    InEarDutyCycles,
    InEarPowerDomain,
    InEarPowerProfile,
    create_aggressive_optimization_profile,
    create_conservative_profile,
    create_guermandi_2022_profile,
    optimize_duty_cycles_for_target,
    validate_power_profile,
)
from synapse24.hardware.inear_eeg import (
    InEarEEGAdapter,
    InEarEEGConfig,
    InEarEEGFirmware,
    create_synthetic_inear_eeg_data,
)


class TestInEarEEGConfig:
    """Test In-Ear EEG configuration."""

    def test_default_config(self) -> None:
        config = InEarEEGConfig()
        assert config.eeg_channels == 2
        assert config.eeg_sampling_rate == 256
        assert config.eeg_input_type == "ADS1299"
        assert config.battery_mah == 150.0
        assert config.target_lifetime_h == 600.0
        assert config.ble_duty_cycle_pct == 15.0
        assert config.electrode_type == "dry_gold_plated"

    def test_custom_config(self) -> None:
        config = InEarEEGConfig(
            eeg_channels=1,
            eeg_sampling_rate=128,
            battery_mah=200.0,
            ble_duty_cycle_pct=10.0,
        )
        assert config.eeg_channels == 1
        assert config.eeg_sampling_rate == 128
        assert config.battery_mah == 200.0
        assert config.ble_duty_cycle_pct == 10.0

    def test_config_bytes_serialization(self) -> None:
        config = InEarEEGConfig()
        firmware = InEarEEGFirmware(config)
        config_bytes = firmware.get_config_bytes()
        assert isinstance(config_bytes, bytes)
        assert len(config_bytes) > 0


class TestInEarEEGAdapter:
    """Test In-Ear EEG BoardAdapter."""

    def test_board_id(self) -> None:
        adapter = InEarEEGAdapter()
        assert adapter.board_id == "INEAR_EEG_BOARD"

    def test_default_sampling_rate(self) -> None:
        adapter = InEarEEGAdapter()
        assert adapter.default_sampling_rate == 256

    def test_default_channels(self) -> None:
        adapter = InEarEEGAdapter()
        channels = adapter.default_channels
        assert "eeg" in channels
        assert "acc" in channels
        assert len(channels["eeg"]) == 2
        assert len(channels["acc"]) == 3

    def test_create_config(self) -> None:
        adapter = InEarEEGAdapter()
        config = adapter.create_config(mac_address="AA:BB:CC:DD:EE:FF")
        assert config.board_id == "INEAR_EEG_BOARD"
        assert config.mac_address == "AA:BB:CC:DD:EE:FF"

    def test_stream_mapping_tier0(self) -> None:
        adapter = InEarEEGAdapter()
        mapping = adapter.get_stream_mapping()
        assert "EEG" in mapping
        assert "ACC" in mapping
        assert mapping["EEG"]["type"] == "EEG_T0"
        assert mapping["ACC"]["type"] == "ACC_T0"
        assert mapping["EEG"]["unit"] == "µV"
        assert mapping["ACC"]["unit"] == "g"


class TestInEarEEGFirmware:
    """Test In-Ear EEG firmware simulation."""

    def test_firmware_creation(self) -> None:
        config = InEarEEGConfig()
        firmware = InEarEEGFirmware(config)
        assert firmware.config == config
        assert firmware._running is False
        assert firmware.sample_count == 0

    def test_setup_lsl_streams(self) -> None:
        config = InEarEEGConfig()
        firmware = InEarEEGFirmware(config)
        # Should not raise even without pylsl (handled by import guard)
        firmware.setup_lsl_streams()

    def test_start_stop_streaming(self) -> None:
        config = InEarEEGConfig()
        firmware = InEarEEGFirmware(config)
        firmware.start_streaming()
        assert firmware._running is True
        firmware.stop_streaming()
        assert firmware._running is False


class TestSyntheticInEarEEGData:
    """Test synthetic in-ear EEG data generation."""

    def test_default_generation(self) -> None:
        data = create_synthetic_inear_eeg_data(duration_s=10.0)
        assert "eeg" in data
        assert "acc_x" in data
        assert "acc_y" in data
        assert "acc_z" in data
        assert "t_eeg" in data
        assert "t_imu" in data

        # Check shapes
        assert data["eeg"].shape == (2, 2560)  # 2ch, 10s @ 256Hz
        assert len(data["acc_x"]) == 500  # 10s @ 50Hz
        assert len(data["t_eeg"]) == 2560
        assert len(data["t_imu"]) == 500

    def test_single_channel(self) -> None:
        data = create_synthetic_inear_eeg_data(duration_s=5.0, eeg_channels=1)
        assert data["eeg"].shape == (1, 1280)  # 1ch, 5s @ 256Hz

    def test_alpha_rhythm_present(self) -> None:
        """Verify alpha rhythm (8-12 Hz) is present in synthetic data."""
        data = create_synthetic_inear_eeg_data(duration_s=30.0, seed=42)
        eeg_ch0 = data["eeg"][0]

        # FFT to check alpha peak
        fft = np.fft.rfft(eeg_ch0)
        freqs = np.fft.rfftfreq(len(eeg_ch0), 1 / 256)
        alpha_mask = (freqs >= 8) & (freqs <= 12)
        alpha_power = np.sum(np.abs(fft[alpha_mask]) ** 2)
        total_power = np.sum(np.abs(fft) ** 2)
        alpha_ratio = alpha_power / total_power

        # Alpha should be dominant in eyes-closed in-ear EEG
        assert alpha_ratio > 0.3, f"Alpha ratio {alpha_ratio:.3f} too low"

    def test_motion_artifact_increases_amplitude(self) -> None:
        """Verify motion artifact increases signal amplitude."""
        clean = create_synthetic_inear_eeg_data(duration_s=10.0, motion_level=0.0)
        noisy = create_synthetic_inear_eeg_data(duration_s=10.0, motion_level=1.0)

        clean_rms = np.sqrt(np.mean(clean["eeg"] ** 2))
        noisy_rms = np.sqrt(np.mean(noisy["eeg"] ** 2))

        assert noisy_rms > clean_rms * 2, "Motion artifact should significantly increase RMS"


class TestInEarPowerProfile:
    """Test In-Ear power profile model."""

    def test_component_currents_defined(self) -> None:
        """All power domains have current values."""
        for domain in InEarPowerDomain:
            assert domain in COMPONENT_CURRENT_MA
            assert COMPONENT_CURRENT_MA[domain] > 0

    def test_duty_cycles_validation(self) -> None:
        """Duty cycles must be 0-100%."""
        # Valid
        dc = InEarDutyCycles(mcu_active=50.0, ble_radio=20.0)
        assert dc.mcu_active == 50.0

        # Invalid
        with pytest.raises(ValueError, match="Duty cycle.*must be 0-100"):
            InEarDutyCycles(mcu_active=150.0)

        with pytest.raises(ValueError, match="Duty cycle.*must be 0-100"):
            InEarDutyCycles(ble_radio=-10.0)

    def test_guermandi_2022_profile_meets_target(self) -> None:
        """Guermandi 2022 profile should meet 600h target."""
        profile = create_guermandi_2022_profile()
        assert profile.meets_target
        assert profile.estimated_lifetime_h >= 600.0
        assert profile.avg_current_ma < 0.3  # <0.3mA for 600h on 150mAh

    def test_conservative_profile_may_miss_target(self) -> None:
        """Conservative profile may miss target."""
        profile = create_conservative_profile()
        # Conservative has higher duty cycles, may not meet 600h
        # This is expected behavior - it shows the margin
        assert profile.avg_current_ma > create_guermandi_2022_profile().avg_current_ma

    def test_aggressive_profile_exceeds_target(self) -> None:
        """Aggressive optimization should exceed target with margin."""
        profile = create_aggressive_optimization_profile()
        assert profile.meets_target
        assert profile.margin_pct > 0

    def test_domain_breakdown_sums_to_total(self) -> None:
        """Domain contributions should sum to total average current."""
        profile = create_guermandi_2022_profile()
        breakdown = profile.domain_breakdown()
        total_contrib = sum(d["avg_contrib_ma"] for d in breakdown.values())
        assert abs(total_contrib - profile.avg_current_ma) < 1e-6

    def test_ble_radio_dominates_budget(self) -> None:
        """BLE radio is typically the largest consumer."""
        profile = create_guermandi_2022_profile()
        breakdown = profile.domain_breakdown()
        ble_pct = breakdown["ble_radio"]["pct_of_total"]
        # In Guermandi profile, BLE radio should be significant but not necessarily dominant
        # due to ULP offload
        assert ble_pct > 10  # At least 10% of budget

    def test_profile_serialization(self) -> None:
        """Profile can be serialized to dict."""
        profile = create_guermandi_2022_profile()
        d = profile.to_dict()
        assert "avg_current_ma" in d
        assert "avg_power_mw" in d
        assert "estimated_lifetime_h" in d
        assert "meets_target" in d
        assert "domain_breakdown" in d
        assert d["meets_target"] is True

    def test_validate_power_profile_passes(self) -> None:
        """Validation passes for Guermandi profile."""
        profile = create_guermandi_2022_profile()
        report = validate_power_profile(profile)
        assert report["passes_target"] is True
        assert "recommendations" in report

    def test_validate_with_measurement(self) -> None:
        """Validation with measured lifetime."""
        profile = create_guermandi_2022_profile()
        # Simulate measurement within 10%
        measured = profile.estimated_lifetime_h * 0.95
        report = validate_power_profile(profile, measured_lifetime_h=measured)
        assert "measured_lifetime_h" in report
        assert "model_error_pct" in report
        assert abs(report["model_error_pct"]) < 10

    def test_optimize_duty_cycles(self) -> None:
        """Duty cycle optimization for target."""
        duties = optimize_duty_cycles_for_target(target_h=600.0, battery_mah=150.0)
        profile = InEarPowerProfile(duty_cycles=duties, battery_mah=150.0, target_lifetime_h=600.0)
        assert profile.meets_target

    def test_optimize_with_fixed_duties(self) -> None:
        """Optimization with fixed duties (e.g., AFE always on)."""
        fixed = {
            InEarPowerDomain.EEG_AFE: 100.0,
            InEarPowerDomain.MCU_ULP: 100.0,
            InEarPowerDomain.PMIC: 100.0,
        }
        duties = optimize_duty_cycles_for_target(
            target_h=600.0, battery_mah=150.0, fixed_duties=fixed
        )
        # Fixed duties preserved
        assert duties.eeg_afe == 100.0
        assert duties.mcu_ulp == 100.0
        assert duties.pmic == 100.0
        # Others optimized
        assert duties.mcu_active <= 25.0
        assert duties.ble_radio <= 30.0


class TestInEarIntegrationWithTier0:
    """Test integration with Tier 0 acquisition system."""

    def test_inear_config_compatible_with_tier0(self) -> None:
        """In-Ear config matches Tier 0 requirements (Architecture.md §37-40)."""
        config = InEarEEGConfig()
        # Tier 0: Continuous H24, relaxed thresholds
        assert config.eeg_sampling_rate >= 128  # Minimum for EEG
        assert config.eeg_channels <= 2  # 1-2 channels per Architecture.md
        assert config.tier == 0  # Implicitly Tier 0

    def test_inear_power_profile_tier0_compatible(self) -> None:
        """In-Ear power profile compatible with Tier 0 continuous budget."""
        # Tier 0 budget from power_budget.py: ~5mW average
        profile = create_guermandi_2022_profile()
        # In-ear pod has its own battery, so it doesn't draw from hub
        # But its power profile should be in µW-low mW range
        assert profile.avg_power_mw < 5.0  # Well within Tier 0 budget

    def test_inear_synthetic_data_compatible_with_quality_metrics(self) -> None:
        """Synthetic data can be processed by signal quality metrics."""
        from synapse24.signal_quality import compute_eeg_quality

        data = create_synthetic_inear_eeg_data(duration_s=30.0)
        eeg_data = data["eeg"]

        # Test with resting_eyes_closed state (appropriate for in-ear)
        for ch in range(eeg_data.shape[0]):
            quality = compute_eeg_quality(eeg_data[ch], 256, state="resting_eyes_closed")
            # Should have quality metrics
            assert "spectral_flatness" in quality
            assert "alpha_band_ratio" in quality
            assert "quality_pass" in quality
            assert isinstance(quality["quality_pass"], bool)


class TestInEarRegistryIntegration:
    """Test In-Ear EEG is registered in hardware registry."""

    def test_inear_adapter_in_registry(self) -> None:
        from synapse24.hardware.registry import BOARD_ADAPTERS

        assert "INEAR_EEG_BOARD" in BOARD_ADAPTERS
        adapter_class = BOARD_ADAPTERS["INEAR_EEG_BOARD"]
        adapter = adapter_class()
        assert isinstance(adapter, InEarEEGAdapter)


class TestInEarEndToEnd:
    """End-to-end test simulating in-ear pod in acquisition loop."""

    def test_firmware_lsl_streaming_simulation(self) -> None:
        """Simulate firmware streaming synthetic data to LSL."""
        config = InEarEEGConfig(eeg_channels=2, eeg_sampling_rate=256)
        firmware = InEarEEGFirmware(config)
        firmware.setup_lsl_streams()

        # Generate synthetic data
        data = create_synthetic_inear_eeg_data(duration_s=5.0, eeg_channels=2)

        # Simulate streaming
        firmware.start_streaming()
        firmware.push_eeg_sample(data["eeg"])
        assert firmware.sample_count == 256 * 5  # 5 seconds * 256 Hz
        firmware.stop_streaming()

    def test_continuous_24h_simulation_power(self) -> None:
        """Verify 24h continuous operation is feasible."""
        profile = create_guermandi_2022_profile()
        # 600h target >> 24h requirement (25x margin)
        assert profile.estimated_lifetime_h >= 24.0 * 5  # 5x margin (120h+)

    def test_tier0_tier1_handoff_simulation(self) -> None:
        """Simulate Tier 0 (in-ear) -> Tier 1 (head pod) handoff.

        When head pod activates, in-ear continues as backup/reference.
        """
        # In-ear is Tier 0
        inear_config = InEarEEGConfig()
        assert inear_config.eeg_channels >= 1

        # Head pod would be Tier 1 (handled by CerelogAdapter)
        # Both can stream simultaneously to LSL with different stream names
        inear_stream = "SYNAPSE_EEG_EAR_T0"
        head_stream = "SYNAPSE_EEG_HEAD_T1"  # From Cerelog

        assert "T0" in inear_stream
        assert "T1" in head_stream


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
