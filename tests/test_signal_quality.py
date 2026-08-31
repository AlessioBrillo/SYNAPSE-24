"""Unit tests for signal quality metrics."""

from __future__ import annotations

import numpy as np

from synapse24.signal_quality import (
    QualityThresholds,
    SignalQualityMetrics,
    alpha_band_power_ratio,
    compute_ecg_quality,
    compute_eeg_quality,
    compute_hrv_metrics,
    compute_ppg_quality,
    compute_ppg_sqi,
    detect_r_peaks_neurokit,
    perfusion_index,
    ppg_motion_artifact_probability,
    r_peak_detection_quality,
    rmssd_mae,
    spectral_flatness,
)


class TestECGQuality:
    """Tests for ECG signal quality metrics."""

    def test_r_peak_detection_synthetic(self):
        """Test R-peak detection on synthetic ECG."""
        fs = 1000
        duration = 10
        t = np.arange(0, duration, 1/fs)
        # Simple synthetic ECG: 1 Hz heart rate = 60 BPM
        ecg = np.sin(2 * np.pi * 1 * t) + 0.5 * np.sin(2 * np.pi * 2 * t)
        # Add QRS-like spikes
        r_times = np.arange(0, duration, 1.0)  # 1 Hz
        r_indices = (r_times * fs).astype(int)
        for idx in r_indices:
            if idx < len(ecg):
                ecg[idx] += 2.0

        peaks = detect_r_peaks_neurokit(ecg, fs)
        assert len(peaks) > 0
        # Should detect approximately 10 peaks for 10 seconds at 1 Hz
        assert 8 <= len(peaks) <= 12

    def test_r_peak_quality_perfect_match(self):
        """Test quality metrics with perfect match."""
        fs = 1000
        ref_peaks = np.array([1000, 2000, 3000, 4000, 5000])
        det_peaks = ref_peaks.copy()

        sens, ppv = r_peak_detection_quality(det_peaks, ref_peaks, fs)
        assert sens == 1.0
        assert ppv == 1.0

        mae = rmssd_mae(det_peaks, ref_peaks, fs)
        assert mae == 0.0

    def test_r_peak_quality_mismatch(self):
        """Test quality metrics with known mismatches."""
        fs = 1000
        ref_peaks = np.array([1000, 2000, 3000, 4000, 5000])
        # Miss one, add one false positive
        det_peaks = np.array([1000, 2000, 3000, 4000, 6000])

        sens, ppv = r_peak_detection_quality(det_peaks, ref_peaks, fs)
        assert sens == 0.8  # 4/5 detected
        assert ppv == 0.8   # 4/5 correct

    def test_hrv_metrics(self):
        """Test HRV metric computation."""
        fs = 1000
        # 60 BPM = 1 Hz = 1000 ms RR
        r_peaks = np.array([1000, 2000, 3000, 4000, 5000, 6000])
        hrv = compute_hrv_metrics(r_peaks, fs)

        assert "mean_rr_ms" in hrv
        assert "rmssd_ms" in hrv
        assert "sdnn_ms" in hrv
        assert abs(hrv["mean_rr_ms"] - 1000) < 1
        assert hrv["rmssd_ms"] == 0  # Perfect regularity


class TestPPGQuality:
    """Tests for PPG signal quality metrics."""

    def test_perfusion_index(self):
        """Test perfusion index computation."""
        # DC = 100, AC = 10 (peak-to-peak = 10, 10% modulation)
        ppg = 100 + 5 * np.sin(2 * np.pi * 1 * np.arange(1000) / 100)
        pi = perfusion_index(ppg)
        assert 9.5 <= pi <= 10.5

    def test_ppg_sqi_clean_signal(self):
        """Test SQI on clean synthetic PPG."""
        fs = 100
        t = np.arange(0, 30, 1/fs)
        # Clean 1 Hz PPG
        ppg = 100 + 10 * np.sin(2 * np.pi * 1 * t)
        sqi = compute_ppg_sqi(ppg, fs)
        assert sqi > 0.7

    def test_ppg_sqi_noisy_signal(self):
        """Test SQI on noisy PPG."""
        fs = 100
        t = np.arange(0, 30, 1/fs)
        # Noisy signal with fixed seed for reproducibility
        rng = np.random.default_rng(42)
        ppg = rng.normal(0, 50, size=len(t))
        sqi = compute_ppg_sqi(ppg, fs)
        assert sqi < 0.55

    def test_motion_artifact_probability(self):
        """Test MAP computation."""
        fs = 100
        t = np.arange(0, 10, 1/fs)
        ppg = 100 + 10 * np.sin(2 * np.pi * 1 * t)
        # High motion
        accel = np.abs(np.sin(2 * np.pi * 2 * t)) * 10

        map_score = ppg_motion_artifact_probability(ppg, accel, fs)
        assert 0 <= map_score <= 1

        # No motion
        accel_none = np.zeros_like(t)
        map_score_none = ppg_motion_artifact_probability(ppg, accel_none, fs)
        assert map_score_none < map_score


class TestEEGQuality:
    """Tests for EEG signal quality metrics."""

    def test_spectral_flatness_tonal(self):
        """Test spectral flatness on tonal signal."""
        fs = 256
        t = np.arange(0, 10, 1/fs)
        # Pure alpha rhythm at 10 Hz
        eeg = 10 * np.sin(2 * np.pi * 10 * t)
        sf = spectral_flatness(eeg, fs)
        assert sf < 0.1  # Very tonal

    def test_spectral_flatness_noise(self):
        """Test spectral flatness on white noise."""
        fs = 256
        eeg = np.random.randn(fs * 10)
        sf = spectral_flatness(eeg, fs)
        assert sf > 0.8  # Flat spectrum

    def test_alpha_ratio_eyes_closed(self):
        """Test alpha ratio on eyes-closed like signal."""
        fs = 256
        t = np.arange(0, 20, 1/fs)
        # Strong alpha
        eeg = 20 * np.sin(2 * np.pi * 10 * t) + 5 * np.random.randn(len(t))
        ratio = alpha_band_power_ratio(eeg, fs)
        assert ratio > 0.3

    def test_alpha_ratio_eyes_open(self):
        """Test alpha ratio on eyes-open like signal (less alpha)."""
        fs = 256
        t = np.arange(0, 20, 1/fs)
        # More beta, less alpha
        eeg = 10 * np.sin(2 * np.pi * 20 * t) + 5 * np.sin(2 * np.pi * 10 * t) + 5 * np.random.randn(len(t))
        ratio = alpha_band_power_ratio(eeg, fs)
        assert ratio < 0.3


class TestSignalQualityMetrics:
    """Tests for SignalQualityMetrics container."""

    def test_evaluate_pass(self):
        """Test evaluation with passing metrics."""
        thresholds = QualityThresholds(r_peak_sensitivity_min=0.9, r_peak_ppv_min=0.9)
        metrics = SignalQualityMetrics(
            r_peak_sensitivity=0.99,
            r_peak_ppv=0.99,
            modality="ecg",
            thresholds=thresholds,
        )
        evals = metrics.evaluate()
        assert evals["r_peak_sensitivity"] is True
        assert evals["r_peak_ppv"] is True
        assert metrics.overall_pass() is True

    def test_evaluate_fail(self):
        """Test evaluation with failing metrics."""
        thresholds = QualityThresholds(r_peak_sensitivity_min=0.99, r_peak_ppv_min=0.99)
        metrics = SignalQualityMetrics(
            r_peak_sensitivity=0.95,
            r_peak_ppv=0.95,
            modality="ecg",
            thresholds=thresholds,
        )
        evals = metrics.evaluate()
        assert evals["r_peak_sensitivity"] is False
        assert evals["r_peak_ppv"] is False
        assert metrics.overall_pass() is False

    def test_to_dict(self):
        """Test serialization."""
        metrics = SignalQualityMetrics(
            r_peak_sensitivity=0.99,
            modality="ecg",
        )
        d = metrics.to_dict()
        assert d["modality"] == "ecg"
        assert d["metrics"]["ecg"]["r_peak_sensitivity"] == 0.99


class TestIntegration:
    """Integration tests for full quality pipelines."""

    def test_compute_ecg_quality_full(self):
        """Test full ECG quality pipeline."""
        fs = 1000
        duration = 30
        t = np.arange(0, duration, 1/fs)
        ecg = np.sin(2 * np.pi * 1.2 * t) + 0.3 * np.sin(2 * np.pi * 2.4 * t)
        r_times = np.arange(0, duration, 1/1.2)
        r_indices = (r_times * fs).astype(int)
        for idx in r_indices:
            if idx < len(ecg):
                ecg[idx] += 2.0

        quality = compute_ecg_quality(ecg, fs)
        assert quality.modality == "ecg"
        assert quality.sampling_rate_hz == fs
        assert quality.duration_s == duration
        assert "rmssd_ms" in quality.hrv_metrics

    def test_compute_ppg_quality_full(self):
        """Test full PPG quality pipeline."""
        fs = 100
        duration = 30
        t = np.arange(0, duration, 1/fs)
        ppg = 100 + 10 * np.sin(2 * np.pi * 1.2 * t)
        accel = np.zeros_like(ppg)

        quality = compute_ppg_quality(ppg, fs, accel)
        assert "ppg_sqi" in quality
        assert "perfusion_index" in quality
        assert "motion_artifact_prob" in quality
        assert 0 <= quality["ppg_sqi"] <= 1
        assert quality["motion_artifact_prob"] < 0.1

    def test_compute_eeg_quality_full(self):
        """Test full EEG quality pipeline."""
        fs = 256
        duration = 20
        t = np.arange(0, duration, 1/fs)
        eeg = 20 * np.sin(2 * np.pi * 10 * t) + 3 * np.random.randn(len(t))

        quality = compute_eeg_quality(eeg, fs, "resting_eyes_closed")
        assert "spectral_flatness" in quality
        assert "alpha_band_ratio" in quality
        assert "band_powers" in quality
        assert quality["quality_pass"] is True


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_empty_signal(self):
        """Test handling of empty signals."""
        assert perfusion_index(np.array([])) == 0.0
        assert compute_ppg_sqi(np.array([]), 100) == 0.0
        assert spectral_flatness(np.array([]), 256) == 1.0
        assert alpha_band_power_ratio(np.array([]), 256) == 0.0

    def test_short_signal(self):
        """Test handling of signals shorter than 1 second."""
        fs = 100
        ppg = np.array([100, 101, 102])
        assert compute_ppg_sqi(ppg, fs) == 0.0

    def test_constant_signal(self):
        """Test handling of constant (flat) signals."""
        ppg = np.ones(1000) * 100
        assert perfusion_index(ppg) == 0.0
        assert compute_ppg_sqi(ppg, 100) == 0.0
