"""Canonical 60s WESAD window regression tests.

Architecture.md §51-53 + Roadmap.md §4: single source of truth is 60s
native-rate fusion windows (no resampling, purity>=0.9, overlap_s=0 for
validation, GroupKFold by subject). Baseline validator and training must
consume fusion_windows, with segment-level scoring kept only as deprecated
fallback for cached JSON without windows.

RED: fusion_window_quality_to_features() does not exist; validator scores
one vector per affect segment instead of one per 60s window.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from synapse24.ingestion.wesad import (
    FUSION_WINDOW_CONFIG,
    fusion_window_quality_to_features,
)


def _load_validator():
    """Load scripts/validate_baseline.py without requiring a package."""
    script_path = Path(__file__).parent.parent / "scripts" / "validate_baseline.py"
    spec = importlib.util.spec_from_file_location("validate_baseline", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _window_meta(label_name: str, mean_rr: float) -> dict:
    return {
        "window_idx": 0,
        "label": {"baseline": 1, "stress": 2, "amusement": 3}[label_name],
        "label_name": label_name,
        "duration_s": 60.0,
        "ecg_quality": {
            "metrics": {
                "ecg": {
                    "hrv_metrics": {
                        "mean_rr_ms": mean_rr,
                        "sdnn_ms": 50.0,
                        "rmssd_ms": 30.0,
                        "pnn50": 10.0,
                        "hr_mean_bpm": 60000 / mean_rr,
                        "lf_power": 100.0,
                        "hf_power": 80.0,
                        "lf_hf_ratio": 1.25,
                    }
                }
            }
        },
        "ppg_quality": {
            "ppg_sqi": 0.85,
            "perfusion_index": 3.0,
            "motion_artifact_prob": 0.1,
        },
    }


class TestCanonicalWindowConfig:
    def test_window_config_locked(self):
        assert FUSION_WINDOW_CONFIG["window_s"] == 60.0
        assert FUSION_WINDOW_CONFIG["overlap_s"] == 0.0
        assert FUSION_WINDOW_CONFIG["min_label_purity"] == 0.9


class TestFusionWindowFeatures:
    def test_baseline_window_feature_vector(self):
        feats, label = fusion_window_quality_to_features(_window_meta("baseline", 800.0))
        assert label == 0
        assert len(feats) == 11
        assert feats[0] == pytest.approx(800.0)

    def test_stress_window_label(self):
        _, label = fusion_window_quality_to_features(_window_meta("stress", 600.0))
        assert label == 1

    def test_amusement_window_label(self):
        _, label = fusion_window_quality_to_features(_window_meta("amusement", 700.0))
        assert label == 2

    def test_unknown_label_returns_none(self):
        meta = _window_meta("baseline", 800.0)
        meta["label_name"] = "meditation"
        assert fusion_window_quality_to_features(meta) is None

    def test_validator_prefers_windows_over_segments(self):
        """Validator must score per-window samples, not per-segment."""
        validator = _load_validator()
        validate_wesad_stress_classification = validator.validate_wesad_stress_classification

        results = []
        for sid in ["S2", "S3", "S4", "S5", "S6", "S7"]:
            results.append(
                {
                    "subject_id": sid,
                    "segments": {
                        "baseline": {
                            "ecg_quality": {
                                "metrics": {
                                    "ecg": {
                                        "hrv_metrics": {
                                            "mean_rr_ms": 800.0,
                                            "sdnn_ms": 50.0,
                                            "rmssd_ms": 30.0,
                                            "pnn50": 10.0,
                                            "hr_mean_bpm": 75.0,
                                            "lf_power": 100.0,
                                            "hf_power": 80.0,
                                            "lf_hf_ratio": 1.25,
                                        }
                                    }
                                }
                            },
                            "ppg_quality": {
                                "ppg_sqi": 0.85,
                                "perfusion_index": 3.0,
                                "motion_artifact_prob": 0.1,
                            },
                        },
                        "stress": {
                            "ecg_quality": {
                                "metrics": {
                                    "ecg": {
                                        "hrv_metrics": {
                                            "mean_rr_ms": 600.0,
                                            "sdnn_ms": 40.0,
                                            "rmssd_ms": 20.0,
                                            "pnn50": 5.0,
                                            "hr_mean_bpm": 100.0,
                                            "lf_power": 150.0,
                                            "hf_power": 50.0,
                                            "lf_hf_ratio": 3.0,
                                        }
                                    }
                                }
                            },
                            "ppg_quality": {
                                "ppg_sqi": 0.8,
                                "perfusion_index": 2.5,
                                "motion_artifact_prob": 0.15,
                            },
                        },
                        "amusement": {
                            "ecg_quality": {
                                "metrics": {
                                    "ecg": {
                                        "hrv_metrics": {
                                            "mean_rr_ms": 700.0,
                                            "sdnn_ms": 45.0,
                                            "rmssd_ms": 25.0,
                                            "pnn50": 8.0,
                                            "hr_mean_bpm": 85.0,
                                            "lf_power": 120.0,
                                            "hf_power": 70.0,
                                            "lf_hf_ratio": 1.7,
                                        }
                                    }
                                }
                            },
                            "ppg_quality": {
                                "ppg_sqi": 0.82,
                                "perfusion_index": 2.8,
                                "motion_artifact_prob": 0.12,
                            },
                        },
                    },
                    "fusion_windows": [
                        _window_meta("baseline", 800.0),
                        _window_meta("baseline", 810.0),
                        _window_meta("stress", 600.0),
                        _window_meta("stress", 610.0),
                        _window_meta("amusement", 700.0),
                        _window_meta("amusement", 710.0),
                    ],
                }
            )
        out = validate_wesad_stress_classification(results)
        assert out["feature_source"] == "fusion_windows_60s"
        # 6 subjects x 6 windows = 36 samples (not 18 segment samples).
        assert out["n_samples"] == 36
