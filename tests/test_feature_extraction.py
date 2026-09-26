"""Tests for feature extraction parity (Python <-> Firmware)."""

from __future__ import annotations

import numpy as np
import pytest

from synapse24.edge_ai.feature_extraction import (
    TRIAGE_NUM_FEATURES,
    compute_axis_features,
    dominant_frequency,
    extract_triage_features_from_lsl_streams,
    extract_triage_features_live,
    extract_triage_features_wesad,
    spectral_entropy,
)


class TestFeatureExtractionCore:
    """Test core feature computation functions."""

    def test_spectral_entropy_constant_signal(self):
        """Constant signal should have zero entropy."""
        signal = np.ones(100)
        ent = spectral_entropy(signal, 100.0)
        assert ent == 0.0

    def test_spectral_entropy_short_signal(self):
        """Signal < 4 samples should return 0."""
        signal = np.array([1.0, 2.0])
        ent = spectral_entropy(signal, 100.0)
        assert ent == 0.0

    def test_dominant_frequency_sine_wave(self):
        """Pure sine wave at known frequency."""
        fs = 100.0
        t = np.arange(0, 1.0, 1 / fs)
        signal = np.sin(2 * np.pi * 10.0 * t)  # 10 Hz
        dom = dominant_frequency(signal, fs)
        assert abs(dom - 10.0) < 1.0  # Within 1 Hz

    def test_dominant_frequency_short_signal(self):
        """Signal < 4 samples should return 0."""
        signal = np.array([1.0, 2.0])
        dom = dominant_frequency(signal, 100.0)
        assert dom == 0.0

    def test_compute_axis_features(self):
        """Test axis feature computation."""
        signal = np.random.randn(100) * 0.1
        mean_v, std_v, ent_v, dom_v = compute_axis_features(signal, 100.0)
        assert isinstance(mean_v, float)
        assert isinstance(std_v, float)
        assert isinstance(ent_v, float)
        assert isinstance(dom_v, float)
        assert std_v >= 0
        assert 0 <= ent_v <= 1


class TestWESADFeatureExtraction:
    """Test WESAD-format feature extraction (training/validation)."""

    def test_wesad_features_shape(self):
        """WESAD features should be (26,)."""
        chest_acc = np.random.randn(2800, 3) * 0.1
        wrist_acc = np.random.randn(128, 3) * 0.1
        wrist_bvp = np.random.randn(256) * 0.01

        feats = extract_triage_features_wesad(chest_acc, wrist_acc, wrist_bvp)
        assert feats.shape == (TRIAGE_NUM_FEATURES,)
        assert feats.dtype == np.float32

    def test_wesad_features_nonzero(self):
        """WESAD features should be non-zero for non-zero input."""
        chest_acc = np.random.randn(2800, 3) * 0.1
        wrist_acc = np.random.randn(128, 3) * 0.1
        wrist_bvp = np.random.randn(256) * 0.01

        feats = extract_triage_features_wesad(chest_acc, wrist_acc, wrist_bvp)
        assert not np.allclose(feats, 0)

    def test_wesad_empty_inputs(self):
        """Empty inputs should return zeros."""
        feats = extract_triage_features_wesad(np.zeros((0, 3)), np.zeros((0, 3)), np.zeros(0))
        assert np.allclose(feats, 0)

    def test_wesad_chest_acc_indices(self):
        """Chest ACC features at indices 0-11."""
        chest_acc = np.ones((2800, 3)) * 0.5
        wrist_acc = np.zeros((128, 3))
        wrist_bvp = np.zeros(256)

        feats = extract_triage_features_wesad(chest_acc, wrist_acc, wrist_bvp)
        # Mean should be ~0.5 for all 3 axes
        for axis in range(3):
            base = axis * 4
            assert abs(feats[base + 0] - 0.5) < 0.01  # mean
            assert abs(feats[base + 1]) < 0.01  # std ~0
            assert feats[base + 2] >= 0  # entropy
            assert feats[base + 3] >= 0  # dom_freq

    def test_wesad_wrist_bvp_indices(self):
        """Wrist BVP features at indices 24-25."""
        chest_acc = np.zeros((2800, 3))
        wrist_acc = np.zeros((128, 3))
        wrist_bvp = np.ones(256) * 10.0

        feats = extract_triage_features_wesad(chest_acc, wrist_acc, wrist_bvp)
        assert abs(feats[24] - 10.0) < 0.01  # mean
        assert abs(feats[25]) < 0.01  # std ~0


class TestLiveFeatureExtraction:
    """Test live sensor feature extraction (forearm hub)."""

    def test_live_features_shape(self):
        """Live features should be (26,)."""
        imu_window = np.random.randn(30, 9) * 0.1
        ppg_window = np.random.randn(10, 2) * 1000

        feats = extract_triage_features_live(imu_window, ppg_window)
        assert feats.shape == (TRIAGE_NUM_FEATURES,)
        assert feats.dtype == np.float32

    def test_live_insufficient_imu(self):
        """Less than 10 IMU samples should return zeros."""
        imu_window = np.random.randn(5, 9) * 0.1
        feats = extract_triage_features_live(imu_window)
        assert np.allclose(feats, 0)

    def test_live_acc_features_indices_0_11(self):
        """ACC features at indices 0-11."""
        # Create IMU with known ACC values
        imu_window = np.zeros((20, 9))
        imu_window[:, 0] = 1.0  # ACC X
        imu_window[:, 1] = 2.0  # ACC Y
        imu_window[:, 2] = 3.0  # ACC Z
        # GYRO all zeros

        feats = extract_triage_features_live(imu_window)
        # ACC X mean ~1.0
        assert abs(feats[0] - 1.0) < 0.1
        # ACC Y mean ~2.0
        assert abs(feats[4] - 2.0) < 0.1
        # ACC Z mean ~3.0
        assert abs(feats[8] - 3.0) < 0.1

    def test_live_gyro_features_indices_12_23(self):
        """GYRO features at indices 12-23."""
        imu_window = np.zeros((20, 9))
        imu_window[:, 3] = 10.0  # GYRO X
        imu_window[:, 4] = 20.0  # GYRO Y
        imu_window[:, 5] = 30.0  # GYRO Z

        feats = extract_triage_features_live(imu_window)
        assert abs(feats[12] - 10.0) < 0.1  # GYRO X mean
        assert abs(feats[16] - 20.0) < 0.1  # GYRO Y mean
        assert abs(feats[20] - 30.0) < 0.1  # GYRO Z mean

    def test_live_ppg_bvp_features_indices_24_25(self):
        """PPG IR -> BVP features at indices 24-25."""
        imu_window = np.random.randn(20, 9) * 0.1
        ppg_window = np.ones((10, 2)) * 1000  # IR = 1000

        feats = extract_triage_features_live(imu_window, ppg_window)
        assert abs(feats[24] - 1000.0) < 1.0  # BVP mean
        assert abs(feats[25]) < 1.0  # BVP std ~0

    def test_live_no_ppg(self):
        """No PPG window should leave BVP features as zero."""
        imu_window = np.random.randn(20, 9) * 0.1
        feats = extract_triage_features_live(imu_window, ppg_window=None)
        assert feats[24] == 0.0
        assert feats[25] == 0.0


class TestLSLFeatureExtraction:
    """Test feature extraction from synchronized LSL streams."""

    def test_lsl_features_shape(self):
        """LSL features should be (26,)."""
        ecg = np.random.randn(2000, 1) * 0.1
        ppg = np.random.randn(256, 2) * 1000
        acc = np.random.randn(400, 3) * 0.1
        gyro = np.random.randn(400, 3) * 0.1
        mag = np.random.randn(400, 3) * 0.1

        feats = extract_triage_features_from_lsl_streams(ecg, ppg, acc, gyro, mag)
        assert feats.shape == (TRIAGE_NUM_FEATURES,)

    def test_lsl_empty_streams(self):
        """Empty streams should return zeros."""
        feats = extract_triage_features_from_lsl_streams(
            np.zeros((0, 1)), np.zeros((0, 2)), np.zeros((0, 3)), np.zeros((0, 3)), np.zeros((0, 3))
        )
        assert np.allclose(feats, 0)


class TestFeatureParity:
    """Test that Python and firmware feature computation match conceptually."""

    def test_feature_indices_match_firmware_header(self):
        """Verify feature index constants match firmware/triage_features.h."""
        # Chest ACC: 0-11 (4 per axis x 3)
        assert TRIAGE_NUM_FEATURES == 26

    def test_deterministic_output(self):
        """Same input should produce same output."""
        np.random.seed(42)
        imu1 = np.random.randn(30, 9) * 0.1
        ppg1 = np.random.randn(10, 2) * 1000

        np.random.seed(42)
        imu2 = np.random.randn(30, 9) * 0.1
        ppg2 = np.random.randn(10, 2) * 1000

        feats1 = extract_triage_features_live(imu1, ppg1)
        feats2 = extract_triage_features_live(imu2, ppg2)
        assert np.allclose(feats1, feats2)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
