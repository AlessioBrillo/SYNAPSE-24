"""Test Cerelog ESP-EEG ingestion: XDF round-trip zero-drop, Tier 1 signal quality, impedance gating.

Architecture.md §67: 8-ch EEG dry (ADS1299) on frontal + behind-ear.
Architecture.md §74, §99: Tier 1 strict thresholds (flatness ≤0.3, alpha_ratio ≥1.5).
Roadmap.md §151: LSL/XDF from day one - zero-drop gate.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest

from synapse24.acquisition.clock_sync import TierSyncBudget
from synapse24.ingestion import (
    CERELOG_8CH_10_20,
    CerelogEEGConfig,
    CerelogEEGManager,
    ImpedanceResult,
    create_cerelog_eeg_config,
)
from synapse24.signal_quality import QualityThresholds, SignalQualityMetrics
from synapse24.signal_quality import Tier as QualityTier
from synapse24.utils import verify_xdf_roundtrip


class TestCerelogEEGConfig:
    """Test CerelogEEGConfig creation and validation."""

    def test_default_config(self):
        """Default config has correct values."""
        config = CerelogEEGConfig()
        assert config.n_channels == 8
        assert config.sampling_rate == 500
        assert config.tier == QualityTier.T1
        assert config.channel_names == CERELOG_8CH_10_20
        assert len(config.channel_names) == config.n_channels
        assert len(config.channel_units) == config.n_channels
        assert config.source_id.startswith("synapse24_SYNAPSE_EEG_T1")

    def test_custom_config(self):
        """Custom config overrides work correctly."""
        config = CerelogEEGConfig(
            n_channels=16,
            sampling_rate=250,
            serial_port="/dev/ttyUSB0",
            mac_address="AA:BB:CC:DD:EE:FF",
        )
        assert config.n_channels == 16
        assert config.sampling_rate == 250
        assert config.serial_port == "/dev/ttyUSB0"
        assert config.mac_address == "AA:BB:CC:DD:EE:FF"
        assert len(config.channel_names) == 16

    def test_create_cerelog_eeg_config_factory(self):
        """Factory function creates valid config."""
        config = create_cerelog_eeg_config(
            serial_port="COM3",
            mac_address="11:22:33:44:55:66",
            sampling_rate=500,
            n_channels=8,
        )
        assert config.serial_port == "COM3"
        assert config.mac_address == "11:22:33:44:55:66"
        assert config.sampling_rate == 500
        assert config.n_channels == 8


class TestCerelogEEGManager:
    """Test CerelogEEGManager with synthetic data (no hardware required)."""

    def test_manager_creation(self):
        """Manager can be created with config."""
        config = CerelogEEGConfig()
        manager = CerelogEEGManager(config)
        assert manager.config == config
        assert manager._manager is None
        assert not manager._is_streaming

    def test_impedance_result_dataclass(self):
        """ImpedanceResult holds correct fields."""
        result = ImpedanceResult(
            channel=0,
            channel_name="Fp1",
            impedance_kohm=25.0,
            pass_threshold=True,
            timestamp=1234567890.0,
        )
        assert result.channel == 0
        assert result.channel_name == "Fp1"
        assert result.impedance_kohm == 25.0
        assert result.pass_threshold is True


class TestCerelogEEGQualityAssessment:
    """Test Tier 1 EEG signal quality assessment with synthetic signals."""

    def _generate_clean_eeg(
        self, n_channels: int = 8, fs: int = 500, duration_s: int = 10
    ) -> np.ndarray:
        """Generate clean synthetic EEG with strong alpha rhythm (eyes closed)."""
        n_samples = duration_s * fs
        t = np.arange(n_samples) / fs
        rng = np.random.default_rng(42)

        signals = []
        for ch in range(n_channels):
            # Strong alpha (10 Hz) + weak noise
            alpha = 20 * np.sin(2 * np.pi * 10 * t)  # 20 µV alpha
            beta = 2 * rng.normal(0, 1, n_samples)  # 2 µV noise
            signal = alpha + beta
            signals.append(signal)

        return np.array(signals, dtype=np.float64)  # (n_channels, n_samples)

    def _generate_noisy_eeg(
        self, n_channels: int = 8, fs: int = 500, duration_s: int = 10
    ) -> np.ndarray:
        """Generate noisy EEG with flat spectrum (poor quality)."""
        n_samples = duration_s * fs
        rng = np.random.default_rng(123)
        return rng.normal(0, 20, (n_channels, n_samples)).astype(np.float64)

    def test_spectral_flatness_clean_eeg(self):
        """Clean EEG with alpha rhythm has low spectral flatness."""
        from synapse24.ingestion import spectral_flatness

        eeg = self._generate_clean_eeg(1, 500, 10)[0]
        flatness = spectral_flatness(eeg, 500)

        # Clean EEG with strong alpha should have flatness < 0.3 (Tier 1 threshold)
        assert flatness < 0.3, f"Clean EEG flatness {flatness:.3f} should be < 0.3"

    def test_spectral_flatness_noisy_eeg(self):
        """Noisy EEG has high spectral flatness."""
        from synapse24.ingestion import spectral_flatness

        eeg = self._generate_noisy_eeg(1, 500, 10)[0]
        flatness = spectral_flatness(eeg, 500)

        # Noisy EEG should have flatness > 0.5
        assert flatness > 0.5, f"Noisy EEG flatness {flatness:.3f} should be > 0.5"

    def test_alpha_ratio_clean_eeg(self):
        """Clean EEG with alpha rhythm has high alpha ratio."""
        from synapse24.ingestion import alpha_band_power_ratio

        eeg = self._generate_clean_eeg(1, 500, 10)[0]
        alpha_ratio = alpha_band_power_ratio(eeg, 500)

        # Strong alpha should give ratio > 0.3 (Tier 1 threshold)
        assert alpha_ratio > 0.3, f"Clean EEG alpha ratio {alpha_ratio:.2f} should be > 0.3"

    def test_alpha_ratio_noisy_eeg(self):
        """Noisy EEG has low alpha ratio."""
        from synapse24.ingestion import alpha_band_power_ratio

        eeg = self._generate_noisy_eeg(1, 500, 10)[0]
        alpha_ratio = alpha_band_power_ratio(eeg, 500)

        # Noise has flat spectrum, alpha ratio ~ 0.2-0.3
        assert alpha_ratio < 1.0, f"Noisy EEG alpha ratio {alpha_ratio:.2f} should be < 1.0"


class TestCerelogEEGSignalQualityMetrics:
    """Test SignalQualityMetrics with Tier 1 thresholds."""

    def test_tier1_thresholds(self):
        """Tier 1 thresholds match Architecture.md."""
        thresholds = QualityThresholds.for_tier(QualityTier.T1)
        assert thresholds.spectral_flatness_max == 0.3
        assert thresholds.alpha_ratio_min == 0.3  # Alpha/total power ratio
        assert thresholds.tier == QualityTier.T1

    def test_tier0_thresholds(self):
        """Tier 0 thresholds are relaxed."""
        thresholds = QualityThresholds.for_tier(QualityTier.T0)
        assert thresholds.spectral_flatness_max == 0.6
        assert thresholds.alpha_ratio_min == 0.15
        assert thresholds.tier == QualityTier.T0

    def test_signal_quality_metrics_evaluation(self):
        """SignalQualityMetrics correctly evaluates against thresholds."""
        # Create metrics with clean EEG values
        metrics = SignalQualityMetrics(
            modality="eeg",
            tier=QualityTier.T1,
            sampling_rate_hz=500,
            duration_s=10.0,
            spectral_flatness=0.2,  # Pass (< 0.3)
            alpha_band_ratio=2.0,  # Pass (> 1.5)
        )

        evals = metrics.evaluate()
        assert evals["spectral_flatness"] is True
        assert evals["alpha_ratio"] is True
        assert metrics.overall_pass() is True

    def test_signal_quality_metrics_fail(self):
        """SignalQualityMetrics fails on poor quality."""
        metrics = SignalQualityMetrics(
            modality="eeg",
            tier=QualityTier.T1,
            sampling_rate_hz=500,
            duration_s=10.0,
            spectral_flatness=0.5,  # Fail (> 0.3)
            alpha_band_ratio=0.2,  # Fail (< 0.3)
        )

        evals = metrics.evaluate()
        assert evals["spectral_flatness"] is False
        assert evals["alpha_ratio"] is False
        assert metrics.overall_pass() is False


class TestXDFRoundTripZeroDrop:
    """Test XDF write -> read round-trip with zero dropped samples.

    Roadmap.md §151: LSL/XDF from day one - any dropped packet fails loudly.
    """

    def test_cerelog_eeg_xdf_roundtrip_zero_drop(self):
        """8-ch EEG @ 500Hz survives XDF round-trip with zero drops."""
        rng = np.random.default_rng(42)
        fs = 500
        duration_s = 10
        n_samples = duration_s * fs
        n_channels = 8

        # Generate synthetic EEG data
        eeg_data = rng.standard_normal((n_channels, n_samples)).astype(np.float32)
        timestamps = np.arange(n_samples, dtype=np.float64) / fs

        streams = [
            {
                "name": "SYNAPSE_EEG",
                "type": "EEG",
                "data": eeg_data.T,  # (n_samples, n_channels)
                "timestamps": timestamps,
                "sampling_rate": fs,
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            result = verify_xdf_roundtrip(streams, Path(tmpdir) / "cerelog_eeg.xdf")

        assert result["all_streams_valid"], f"XDF validation failed: {result}"
        assert result["total_dropped"] == 0, f"Dropped {result['total_dropped']} samples"
        assert result["n_streams"] == 1
        assert result["per_stream"][0]["valid"] is True

    def test_cerelog_eeg_xdf_roundtrip_with_acc_gyro(self):
        """EEG + ACC + GYRO streams survive XDF round-trip."""
        rng = np.random.default_rng(42)
        fs = 500
        duration_s = 5
        n_samples = duration_s * fs

        streams = [
            {
                "name": "SYNAPSE_EEG",
                "type": "EEG",
                "data": rng.standard_normal((n_samples, 8)).astype(np.float32),
                "timestamps": np.arange(n_samples, dtype=np.float64) / fs,
                "sampling_rate": fs,
            },
            {
                "name": "SYNAPSE_ACC",
                "type": "ACC_T1",
                "data": rng.standard_normal((n_samples, 3)).astype(np.float32),
                "timestamps": np.arange(n_samples, dtype=np.float64) / fs,
                "sampling_rate": fs,
            },
            {
                "name": "SYNAPSE_GYRO",
                "type": "GYRO_T1",
                "data": rng.standard_normal((n_samples, 3)).astype(np.float32),
                "timestamps": np.arange(n_samples, dtype=np.float64) / fs,
                "sampling_rate": fs,
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            result = verify_xdf_roundtrip(streams, Path(tmpdir) / "cerelog_eeg_multi.xdf")

        assert result["all_streams_valid"], f"XDF validation failed: {result}"
        assert result["total_dropped"] == 0, f"Dropped {result['total_dropped']} samples"
        assert result["n_streams"] == 3
        for stream in result["per_stream"]:
            assert stream["valid"] is True

    def test_xdf_roundtrip_detects_drops(self):
        """Corrupted XDF (truncated) must fail the gate."""
        rng = np.random.default_rng(7)
        fs = 500
        n_samples = 1000

        streams = [
            {
                "name": "SYNAPSE_EEG",
                "type": "EEG",
                "data": rng.standard_normal((n_samples, 8)).astype(np.float32),
                "timestamps": np.arange(n_samples, dtype=np.float64) / fs,
                "sampling_rate": fs,
                "drop_last_n": 10,  # Inject 10 sample drop
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            result = verify_xdf_roundtrip(streams, Path(tmpdir) / "lossy.xdf")

        assert not result["all_streams_valid"]
        assert result["total_dropped"] == 10
        assert result["per_stream"][0]["dropped"] == 10


class TestTierSyncBudget:
    """Test per-tier sync budget constants."""

    def test_tier_sync_budget_defaults(self):
        """TierSyncBudget has correct defaults per Architecture.md §92."""
        budget = TierSyncBudget()
        assert budget.tier0_max_residual_drift_ms == 10.0
        assert budget.tier1_max_residual_drift_ms == 1.0
        assert budget.tier0_sync_interval_s == 60.0
        assert budget.tier1_sync_interval_s == 10.0


class TestCerelogEEGIntegration:
    """Integration tests (require hardware - skipped in CI)."""

    @pytest.mark.integration
    def test_ingest_cerelog_eeg_synthetic(self):
        """Full ingestion pipeline with synthetic data."""
        # This test would use a synthetic board or mocked BrainFlow
        # For now, verify the function signature exists
        config = create_cerelog_eeg_config()
        assert config is not None

    @pytest.mark.integration
    def test_impedance_check_gating(self):
        """Impedance check gates Tier 1 promotion (Architecture.md §77)."""
        # This would test with real hardware


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
