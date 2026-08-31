"""Tests for dataset ingestion pipelines (using synthetic data)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from synapse24.ingestion import (
    extract_chest_signals,
    extract_wrist_signals,
)


class TestWESADIngestion:
    """Tests for WESAD ingestion (using mock data since download is slow)."""

    def test_extract_chest_signals_structure(self):
        """Test chest signal extraction structure."""
        # Create mock WESAD data structure
        mock_data = {
            "signal": {
                "chest": {
                    "ECG": np.random.randn(7000, 1),
                    "EDA": np.random.randn(7000, 1),
                    "EMG": np.random.randn(7000, 1),
                    "Resp": np.random.randn(7000, 1),
                    "Temp": np.random.randn(7000, 1),
                    "ACC": np.random.randn(7000, 3),
                }
            },
            "label": np.ones(7000),
        }

        chest = extract_chest_signals(mock_data)
        assert "ecg" in chest
        assert "eda" in chest
        assert "emg" in chest
        assert "resp" in chest
        assert "temp" in chest
        assert "acc_x" in chest
        assert "acc_y" in chest
        assert "acc_z" in chest
        assert "labels" in chest
        assert all(len(v) == 7000 for v in chest.values())

    def test_extract_wrist_signals_structure(self):
        """Test wrist signal extraction structure."""
        mock_data = {
            "signal": {
                "wrist": {
                    "BVP": np.random.randn(640, 1),
                    "EDA": np.random.randn(40, 1),
                    "Temp": np.random.randn(40, 1),
                    "ACC": np.random.randn(320, 3),
                }
            },
        }

        wrist = extract_wrist_signals(mock_data)
        assert "bvp" in wrist
        assert "eda" in wrist
        assert "temp" in wrist
        assert "acc_x" in wrist
        assert "acc_y" in wrist
        assert "acc_z" in wrist


class TestMITBIHIngestion:
    """Tests for MIT-BIH ingestion."""

    def test_load_mitbih_record_structure(self):
        """Test that MIT-BIH record loading returns expected structure."""
        # This would need actual data - skip in unit tests
        pytest.skip("Requires downloaded MIT-BIH data")

    def test_r_peak_validation_logic(self):
        """Test the R-peak validation logic with synthetic data."""
        from synapse24.signal_quality import (
            detect_r_peaks_neurokit,
            r_peak_detection_quality,
        )

        fs = 360  # MIT-BIH native rate
        duration = 10
        t = np.arange(0, duration, 1/fs)
        # Synthetic ECG at ~72 BPM
        ecg = np.sin(2 * np.pi * 1.2 * t)
        r_times = np.arange(0, duration, 1/1.2)
        r_indices = (r_times * fs).astype(int)
        for idx in r_indices:
            if idx < len(ecg):
                ecg[idx] += 2.0

        detected = detect_r_peaks_neurokit(ecg, fs)
        ref_peaks = r_indices[r_indices < len(ecg)]

        sens, ppv = r_peak_detection_quality(detected, ref_peaks, fs)
        # Should be reasonably high on clean synthetic data
        assert sens > 0.9
        assert ppv > 0.9


class TestDataValidation:
    """Tests for data validation and quality checks."""

    def test_signal_quality_thresholds(self):
        """Test QualityThresholds defaults."""
        from synapse24.signal_quality import QualityThresholds

        t = QualityThresholds()
        assert t.r_peak_sensitivity_min == 0.996
        assert t.r_peak_ppv_min == 0.996
        assert t.rmssd_mae_max_ms == 5.0
        assert t.ppg_sqi_min == 0.7

    def test_signal_quality_metrics_serialization(self):
        """Test SignalQualityMetrics to_dict serialization."""
        from synapse24.signal_quality import QualityThresholds, SignalQualityMetrics

        m = SignalQualityMetrics(
            r_peak_sensitivity=0.999,
            r_peak_ppv=0.998,
            rmssd_mae_ms=2.5,
            modality="ecg",
            thresholds=QualityThresholds(),
        )
        d = m.to_dict()
        assert d["modality"] == "ecg"
        assert d["metrics"]["ecg"]["r_peak_sensitivity"] == 0.999
        assert "evaluations" in d
        assert "overall_pass" in d


class TestXDFUtils:
    """Tests for XDF writing utilities."""

    def test_create_stream_info(self):
        """Test StreamInfo creation."""
        from synapse24.utils import create_stream_info

        info = create_stream_info(
            name="TEST_ECG",
            stream_type="ECG",
            channel_count=1,
            sampling_rate=250,
            channel_names=["ECG"],
            channel_units=["µV"],
        )
        assert info.name() == "TEST_ECG"
        assert info.type() == "ECG"
        assert info.channel_count() == 1
        assert info.nominal_srate() == 250

    @pytest.mark.skip(reason="XDF writing not implemented in pyxdf; requires LabRecorder")
    def test_write_xdf_roundtrip(self):
        """Test XDF write and read roundtrip (skipped - write not available)."""

    def test_generate_synthetic_timestamps(self):
        """Test timestamp generation."""
        from synapse24.utils import generate_synthetic_timestamps

        timestamps = generate_synthetic_timestamps(1000, 100, start_time=1000.0)
        assert len(timestamps) == 1000
        assert timestamps[0] == 1000.0
        assert abs(timestamps[-1] - (1000.0 + 9.99)) < 0.01
        # Check regular spacing
        diffs = np.diff(timestamps)
        assert np.allclose(diffs, 0.01)


class TestConfiguration:
    """Tests for configuration and environment."""

    def test_pyproject_toml_exists(self):
        """Test that pyproject.toml exists and is valid."""
        pyproject = Path("pyproject.toml")
        assert pyproject.exists()

        import tomllib
        with open(pyproject, "rb") as f:
            config = tomllib.load(f)
        assert "project" in config
        assert config["project"]["name"] == "synapse-24"

    def test_imports_work(self):
        """Test that all main modules can be imported."""
        import synapse24

        assert synapse24.__version__ == "0.1.0"
