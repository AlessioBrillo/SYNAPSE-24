"""Unit tests for XDF timestamp correction post-processor."""

from __future__ import annotations

import io
import struct
import uuid
from pathlib import Path

import numpy as np
import pytest
import pyxdf
from synapse_device.xdf_writer import write_varlen_int
from synapse_device.xdf_writer import write_xdf_chunk as _write_xdf_chunk

from synapse24.acquisition.clock_sync import (
    DriftEstimate,
    MultiPodClockSync,
    SyncConfig,
    SyncMarker,
    quantify_residual_drift,
)
from synapse24.utils.xdf import (
    XDF_CHUNK_TAGS,
    XDF_MAGIC,
    StreamConfig,
    verify_xdf_roundtrip,
    write_xdf,
)
from synapse24.utils.xdf_correction import (
    CorrectionError,
    _process_samples_chunk,
    _read_xdf_chunk,
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

    def test_correct_xdf_unknown_source_id_fails_closed(self, tmp_path: Path):
        """A non-empty correction set matching no stream must not emit a silent no-op file."""
        from synapse24.utils.xdf_correction import CorrectionError

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

        with pytest.raises(CorrectionError, match="matched any XDF stream"):
            correct_xdf_timestamps(input_path, output_path, corrections)
        assert not output_path.exists(), "no uncorrected file is written on a no-op"

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


class TestCorrectionSelfDescribingOutput:
    """The corrected file keeps its metadata and never applies a silent no-op."""

    def test_pod_id_corrections_resolve_through_explicit_mapping(self, tmp_path: Path) -> None:
        input_path, source_ids = TestXDFCorrection()._create_test_xdf(tmp_path)
        output_path = tmp_path / "corrected.xdf"
        # Callers get get_sync_status()["pods"] keyed by pod_id, not source_id.
        result = correct_xdf_from_sync(
            input_path,
            output_path,
            {"head_pod": {"offset_ms": 5.0, "drift_rate_ppm": 0.0}},
            pod_id_to_source_id={"head_pod": source_ids[0]},
        )
        assert result.streams_corrected == 1
        assert result.corrections_applied[source_ids[0]].pod_id == "head_pod"

    def test_original_metadata_channel_info_and_tier_are_preserved(self, tmp_path: Path) -> None:
        source_id = "synapse24_ecg_t0_SYN-000123"
        n = 100
        t = 1000.0 + np.arange(n) / 100.0
        write_xdf(
            tmp_path / "in.xdf",
            [
                {
                    "data": np.zeros((n, 2)),
                    "timestamps": t,
                    "info": StreamConfig(
                        name="SYNAPSE_ECG_T0",
                        stream_type="ECG",
                        channel_count=2,
                        sampling_rate=100.0,
                        channel_names=["ECG_A", "ECG_B"],
                        channel_units=["mV", "mV"],
                        source_id=source_id,
                        tier=1,
                        metadata={"session_kind": "human", "utc0_s": "1735689600.0"},
                    ),
                }
            ],
        )
        out = tmp_path / "out.xdf"
        result = correct_xdf_timestamps(
            tmp_path / "in.xdf",
            out,
            {
                source_id: DriftEstimate(
                    pod_id="head_pod",
                    offset_ms=2.0,
                    drift_rate_ppm=0.0,
                    confidence=1.0,
                    method="marker",
                )
            },
        )
        assert result.streams_corrected == 1
        streams, _ = pyxdf.load_xdf(str(out))
        info = streams[0]["info"]
        desc = info["desc"][0]
        labels = [str((c.get("label") or [""])[0]) for c in desc["channels"][0]["channel"]]
        units = [str((c.get("unit") or [""])[0]) for c in desc["channels"][0]["channel"]]
        assert labels == ["ECG_A", "ECG_B"]
        assert units == ["mV", "mV"]
        assert str(desc["acquisition"][0]["tier"][0]) == "1"
        meta = desc["metadata"][0]
        assert str(meta["session_kind"][0]) == "human"
        assert str(meta["utc0_s"][0]) == "1735689600.0"
        assert str(meta["corrected"][0]) == "True"


class TestSampleChunkByteWidths:
    """Channel payload width comes from channel_format, not a 4-byte assumption."""

    @staticmethod
    def _samples(n_channels: int, values: list[int], fmt: str) -> bytes:
        payload = bytearray()
        write_varlen_int(payload, 1)
        payload.extend(b"\x01")
        payload.extend(struct.pack("<d", 1000.0))
        if fmt == "int16":
            payload.extend(struct.pack("<" + "h" * n_channels, *values))
        else:  # double
            payload.extend(struct.pack("<" + "d" * n_channels, *values))
        return bytes(payload)

    def test_int16_payload_is_not_overrun(self) -> None:
        correction = DriftEstimate(
            pod_id="p", offset_ms=5.0, drift_rate_ppm=0.0, confidence=1.0, method="marker"
        )
        raw = self._samples(2, [3, -4], "int16")
        corrected = _process_samples_chunk(raw, 2, "int16", correction)
        # varlen(1)=2 bytes, ts_flag=1 byte, then the timestamp and the values
        assert struct.unpack("<d", corrected[3:11])[0] == pytest.approx(999.995)
        assert struct.unpack("<2h", corrected[11:15]) == (3, -4)

    def test_double_payload_keeps_all_eight_bytes_per_channel(self) -> None:
        correction = DriftEstimate(
            pod_id="p", offset_ms=0.0, drift_rate_ppm=0.0, confidence=1.0, method="marker"
        )
        raw = self._samples(2, [1.5, -2.25], "double")
        corrected = _process_samples_chunk(raw, 2, "double", correction)
        assert struct.unpack("<2d", corrected[11:27]) == (1.5, -2.25)

    def test_unknown_numeric_format_is_refused(self) -> None:
        correction = DriftEstimate(
            pod_id="p", offset_ms=0.0, drift_rate_ppm=0.0, confidence=1.0, method="marker"
        )
        raw = self._samples(1, [1], "int16")
        with pytest.raises(CorrectionError, match="unsupported numeric channel_format"):
            _process_samples_chunk(raw, 1, "int128", correction)


def test_stale_clock_chunk_is_dropped_for_corrected_stream(tmp_path: Path) -> None:
    """A rewritten SAMPLES map must not be re-jittered by an old CLOCK chunk."""
    source_id = "synapse24_clock_test"
    n = 50
    src = tmp_path / "in.xdf"
    verify_xdf_roundtrip(
        [
            {
                "name": "ECG",
                "type": "ECG",
                "data": np.zeros((n, 1)),
                "timestamps": 1000.0 + np.arange(n) / 100.0,
                "sampling_rate": 100.0,
                "source_id": source_id,
            }
        ],
        src,
    )
    # Insert a CLOCK chunk (tag 4) for stream 1 right after its SAMPLES chunk.
    reader = io.BytesIO(src.read_bytes()[len(XDF_MAGIC) :])
    chunks: list[tuple[int, int | None, bytes]] = []
    while (chunk := _read_xdf_chunk(reader)) is not None:
        chunks.append(chunk)
    samples_index = next(i for i, c in enumerate(chunks) if c[0] == XDF_CHUNK_TAGS["SAMPLES"])
    chunks.insert(samples_index + 1, (XDF_CHUNK_TAGS["CLOCK"], 1, b"\x00" * 16))
    crafted = tmp_path / "with_clock.xdf"
    with crafted.open("wb") as fout:
        fout.write(XDF_MAGIC)
        for tag, sid, payload in chunks:
            _write_xdf_chunk(fout, tag, payload, sid)

    corrected = tmp_path / "corrected.xdf"
    correct_xdf_timestamps(
        crafted,
        corrected,
        {
            source_id: DriftEstimate(
                pod_id="p", offset_ms=5.0, drift_rate_ppm=0.0, confidence=1.0, method="marker"
            )
        },
        validate_output=False,
    )

    reader = io.BytesIO(corrected.read_bytes()[len(XDF_MAGIC) :])
    tags: list[int] = []
    while (chunk := _read_xdf_chunk(reader)) is not None:
        tags.append(chunk[0])
    assert XDF_CHUNK_TAGS["CLOCK"] not in tags
    assert tags.count(XDF_CHUNK_TAGS["SAMPLES"]) >= 1
