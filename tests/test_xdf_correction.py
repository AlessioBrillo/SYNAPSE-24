"""Unit tests for XDF timestamp correction post-processor."""

from __future__ import annotations

import uuid
from pathlib import Path

import numpy as np
import pytest
import pyxdf

from synapse24.acquisition.clock_sync import (
    DriftEstimate,
    MultiPodClockSync,
    SyncConfig,
    SyncMarker,
    quantify_residual_drift,
)
from synapse24.utils.xdf import StreamConfig, verify_xdf_roundtrip
from synapse24.utils.xdf_correction import (
    correct_xdf_from_sync,
    correct_xdf_timestamps,
)
from tests.constants import (
    XDF_DEFAULT_N_SAMPLES,
    XDF_DRIFT_TOLERANCE_S,
    XDF_OFFSET_TOLERANCE_S,
)

_RNG = np.random.default_rng(42)


class TestXDFCorrection:
    """Tests for correct_xdf_timestamps function."""

    def _create_test_xdf(self, tmp_path: Path, n_streams: int = 2) -> tuple[Path, list[str]]:
        """Create a test XDF file with known timestamps."""
        source_ids = []
        streams = []

        for i in range(n_streams):
            source_id = f"synapse24_test_pod_{i}_{uuid.uuid4().hex[:8]}"
            source_ids.append(source_id)

            n_samples = XDF_DEFAULT_N_SAMPLES
            sampling_rate = 100.0 if i == 0 else 50.0
            timestamps = np.arange(n_samples, dtype=np.float64) / sampling_rate + 1000.0
            data = _RNG.standard_normal((n_samples, 3)).astype(np.float32)

            config = StreamConfig(
                name=f"TEST_POD_{i}",
                stream_type="EEG" if i == 0 else "PPG",
                channel_count=3,
                sampling_rate=sampling_rate,
                channel_format="float32",
                source_id=source_id,
                tier=1 if i == 0 else 0,
            )

            streams.append(
                {
                    "name": config.name,
                    "type": config.stream_type,
                    "data": data,
                    "timestamps": timestamps,
                    "sampling_rate": sampling_rate,
                    "source_id": config.source_id,
                }
            )

        output_path = tmp_path / "test_input.xdf"
        verify_xdf_roundtrip(streams, output_path)
        return output_path, source_ids

    def test_correct_xdf_timestamps_no_correction(self, tmp_path: Path):
        """Test correction with empty corrections dict (should copy unchanged)."""
        input_path, source_ids = self._create_test_xdf(tmp_path)
        output_path = tmp_path / "test_output.xdf"

        result = correct_xdf_timestamps(input_path, output_path, {})

        assert result.streams_corrected == 0
        assert result.output_path == output_path
        assert output_path.exists()

        validation = result.validation
        assert validation["validation"]["all_streams_valid"]
        assert validation["n_streams"] == 2

    def test_correct_xdf_timestamps_with_offset(self, tmp_path: Path):
        """Test correction with constant offset."""
        input_path, source_ids = self._create_test_xdf(tmp_path)
        output_path = tmp_path / "test_output.xdf"

        corrections = {
            source_ids[0]: DriftEstimate(
                pod_id=source_ids[0],
                offset_ms=5.0,
                drift_rate_ppm=0.0,
                confidence=1.0,
                method="marker",
            ),
        }

        result = correct_xdf_timestamps(input_path, output_path, corrections)

        assert result.streams_corrected == 1
        assert source_ids[0] in result.corrections_applied

        corrected_streams, _ = pyxdf.load_xdf(str(output_path))
        for stream in corrected_streams:
            info = stream["info"]
            src_id = info.get("source_id", [""])[0]
            if src_id == source_ids[0]:
                timestamps = stream["time_stamps"]
                original_start = 1000.0
                expected_start = 1000.0 - 0.005
                assert abs(timestamps[0] - expected_start) < XDF_OFFSET_TOLERANCE_S

    def test_correct_xdf_timestamps_with_drift(self, tmp_path: Path):
        """Test correction with clock drift."""
        input_path, source_ids = self._create_test_xdf(tmp_path)
        output_path = tmp_path / "test_output.xdf"

        corrections = {
            source_ids[0]: DriftEstimate(
                pod_id=source_ids[0],
                offset_ms=0.0,
                drift_rate_ppm=100.0,
                confidence=1.0,
                method="marker",
            ),
        }

        result = correct_xdf_timestamps(input_path, output_path, corrections)

        assert result.streams_corrected == 1

        corrected_streams, _ = pyxdf.load_xdf(str(output_path))
        for stream in corrected_streams:
            info = stream["info"]
            src_id = info.get("source_id", [""])[0]
            if src_id == source_ids[0]:
                timestamps = stream["time_stamps"]
                duration = timestamps[-1] - timestamps[0]
                assert abs(duration - 9.99) < XDF_DRIFT_TOLERANCE_S

    def test_correct_xdf_timestamps_combined_offset_drift(self, tmp_path: Path):
        """Test correction with both offset and drift."""
        input_path, source_ids = self._create_test_xdf(tmp_path)
        output_path = tmp_path / "test_output.xdf"

        corrections = {
            source_ids[0]: DriftEstimate(
                pod_id=source_ids[0],
                offset_ms=2.0,
                drift_rate_ppm=50.0,
                confidence=1.0,
                method="marker",
            ),
            source_ids[1]: DriftEstimate(
                pod_id=source_ids[1],
                offset_ms=-1.0,
                drift_rate_ppm=25.0,
                confidence=1.0,
                method="acc_correlation",
            ),
        }

        result = correct_xdf_timestamps(input_path, output_path, corrections)

        assert result.streams_corrected == 2
        assert len(result.corrections_applied) == 2

        validation = result.validation
        assert validation["validation"]["all_streams_valid"]

    def test_correct_xdf_preserves_all_streams(self, tmp_path: Path):
        """Test that all streams are preserved after correction."""
        input_path, source_ids = self._create_test_xdf(tmp_path, n_streams=3)
        output_path = tmp_path / "test_output.xdf"

        corrections = {
            source_ids[0]: DriftEstimate(
                pod_id=source_ids[0],
                offset_ms=3.0,
                drift_rate_ppm=0.0,
                confidence=1.0,
                method="marker",
            ),
        }

        result = correct_xdf_timestamps(input_path, output_path, corrections)

        validation = result.validation
        assert validation["n_streams"] == 3
        assert validation["validation"]["all_streams_valid"]

        corrected_streams, _ = pyxdf.load_xdf(str(output_path))
        assert len(corrected_streams) == 3

        for stream in corrected_streams:
            assert len(stream["time_series"]) == XDF_DEFAULT_N_SAMPLES

    def test_correct_xdf_from_sync(self, tmp_path: Path):
        """Test convenience wrapper using sync status format."""
        input_path, source_ids = self._create_test_xdf(tmp_path)
        output_path = tmp_path / "test_output.xdf"

        sync_estimates = {
            source_ids[0]: {
                "offset_ms": 4.0,
                "drift_rate_ppm": 10.0,
                "confidence": 0.95,
                "method": "acc_correlation",
            },
        }

        result = correct_xdf_from_sync(input_path, output_path, sync_estimates)

        assert result.streams_corrected == 1
        assert source_ids[0] in result.corrections_applied

    def test_correct_xdf_unknown_source_id(self, tmp_path: Path):
        """Test that unknown source_ids in corrections are ignored gracefully."""
        input_path, source_ids = self._create_test_xdf(tmp_path)
        output_path = tmp_path / "test_output.xdf"

        corrections = {
            "unknown_source_id": DriftEstimate(
                pod_id="unknown_source_id",
                offset_ms=10.0,
                drift_rate_ppm=0.0,
                confidence=1.0,
                method="marker",
            ),
        }

        result = correct_xdf_timestamps(input_path, output_path, corrections)

        assert result.streams_corrected == 0
        assert len(result.corrections_applied) == 0
        assert result.validation["validation"]["all_streams_valid"]

    def test_correct_xdf_invalid_input(self, tmp_path: Path):
        """Test error handling for invalid input file."""
        output_path = tmp_path / "test_output.xdf"
        input_path = tmp_path / "nonexistent.xdf"

        with pytest.raises(FileNotFoundError):
            correct_xdf_timestamps(input_path, output_path, {})

    def test_correct_xdf_invalid_magic(self, tmp_path: Path):
        """Test error handling for invalid XDF magic bytes."""
        bad_file = tmp_path / "bad.xdf"
        bad_file.write_bytes(b"NOT_XDF")

        output_path = tmp_path / "test_output.xdf"

        with pytest.raises(ValueError, match="Invalid XDF magic"):
            correct_xdf_timestamps(bad_file, output_path, {})


class TestCorrectionIntegration:
    """Integration tests with clock sync pipeline."""

    def test_end_to_end_drift_correction(self, tmp_path: Path):
        """Test full pipeline: MultiPodClockSync -> correct_xdf_timestamps -> verify."""
        n_samples = 5000
        sampling_rate = 100.0

        hub_timestamps = np.arange(n_samples, dtype=np.float64) / sampling_rate

        pod0_timestamps = (hub_timestamps + 0.005) * 1.0001
        pod0_data = _RNG.standard_normal((n_samples, 3)).astype(np.float32)

        pod1_timestamps = hub_timestamps * 1.00005
        pod1_data = _RNG.standard_normal((n_samples, 2)).astype(np.float32)

        streams = [
            {
                "name": "SYNAPSE_HEAD_POD",
                "type": "EEG",
                "data": pod0_data,
                "timestamps": pod0_timestamps,
                "sampling_rate": sampling_rate,
                "source_id": "synapse24_SYNAPSE_HEAD_POD_headpod",
            },
            {
                "name": "SYNAPSE_FOREARM_POD",
                "type": "PPG",
                "data": pod1_data,
                "timestamps": pod1_timestamps,
                "sampling_rate": sampling_rate,
                "source_id": "synapse24_SYNAPSE_FOREARM_POD_forearmpod",
            },
        ]

        input_path = tmp_path / "raw.xdf"
        verify_xdf_roundtrip(streams, input_path)

        sync = MultiPodClockSync(SyncConfig())
        sync.register_pod("head_pod", 100)
        sync.register_pod("forearm_pod", 100)

        for i in range(20):
            hub_t = float(i * 60.0)
            sync.drift_estimator.add_marker(
                SyncMarker(
                    sequence=i,
                    hub_timestamp=hub_t,
                    pod_timestamps={
                        "head_pod": (hub_t + 0.005) * 1.0001,
                        "forearm_pod": hub_t * 1.00005,
                    },
                )
            )

        sync.update_drift_estimates()
        sync_status = sync.get_sync_status()

        sync_estimates = {
            "synapse24_SYNAPSE_HEAD_POD_headpod": sync_status["pods"]["head_pod"],
            "synapse24_SYNAPSE_FOREARM_POD_forearmpod": sync_status["pods"]["forearm_pod"],
        }

        output_path = tmp_path / "corrected.xdf"
        result = correct_xdf_from_sync(input_path, output_path, sync_estimates)

        assert result.streams_corrected == 2
        assert result.validation["validation"]["all_streams_valid"]

        corrected_streams, _ = pyxdf.load_xdf(str(output_path))
        for stream in corrected_streams:
            info = stream["info"]
            name = info.get("name", [""])[0]
            timestamps = stream["time_stamps"]

            if name in ("SYNAPSE_HEAD_POD", "SYNAPSE_FOREARM_POD"):
                metrics = quantify_residual_drift(timestamps, hub_timestamps[: len(timestamps)])
                assert metrics["max_abs_offset_ms"] < 1.0
                assert metrics["p99_offset_ms"] < 1.0
