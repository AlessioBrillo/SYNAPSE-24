#!/usr/bin/env python3
"""Live LSL stream validation for SYNAPSE-24 Phase 1 hardware bringup.

Captures real-time streams from ESP32-S3 forearm hub via BLE,
validates signal quality in real-time, and writes XDF with
embedded quality metrics per segment.

Architecture.md §92: T0 sync budget ≤10ms residual drift
Roadmap.md Phase 1: Live ECG+PPG+IMU streaming, synchronized in LSL
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

try:
    import pylsl
    from pylsl import StreamInlet, local_clock, resolve_streams
except ImportError:
    pylsl = None
    StreamInlet = None
    resolve_streams = None
    local_clock = None

try:
    import pyxdf
except ImportError:
    pyxdf = None

from synapse24.config.loader import load_hardware_config
from synapse24.signal_quality import (
    QualityThresholds,
    Tier,
    compute_ecg_quality,
    compute_hrv_metrics,
    compute_ppg_quality,
    rmssd_mae,
)
from synapse24.utils import validate_xdf, write_xdf

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


@dataclass
class StreamBuffer:
    """Thread-safe circular buffer for LSL stream data."""

    name: str
    type: str
    sampling_rate: float
    channel_count: int
    max_samples: int
    data: deque = field(default_factory=deque)
    timestamps: deque = field(default_factory=deque)
    lock: threading.Lock = field(default_factory=threading.Lock)
    dropped: int = 0

    def append(self, sample: npt.NDArray[np.float64], timestamp: float) -> None:
        with self.lock:
            if len(self.data) >= self.max_samples:
                self.data.popleft()
                self.timestamps.popleft()
                self.dropped += 1
            self.data.append(sample)
            self.timestamps.append(timestamp)

    def get_arrays(self) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
        with self.lock:
            if not self.data:
                return np.empty((0, self.channel_count)), np.empty(0)
            return (
                np.array(self.data, dtype=np.float64),
                np.array(self.timestamps, dtype=np.float64),
            )

    def clear(self) -> None:
        with self.lock:
            self.data.clear()
            self.timestamps.clear()
            self.dropped = 0


@dataclass
class QualityMetricsBuffer:
    """Stores quality metrics per segment for XDF metadata stream."""

    segments: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add_segment(self, metrics: dict[str, Any]) -> None:
        with self.lock:
            self.segments.append(metrics)

    def get_all(self) -> list[dict[str, Any]]:
        with self.lock:
            return self.segments.copy()


class LiveLSLValidator:
    """Real-time LSL stream capture and validation."""

    def __init__(
        self,
        config_path: Path,
        output_dir: Path,
        duration_s: float = 60.0,
        segment_s: float = 10.0,
    ) -> None:
        self.config_path = config_path
        self.output_dir = Path(output_dir)
        self.duration_s = duration_s
        self.segment_s = segment_s

        # Load config
        self.config = load_hardware_config(config_path)

        # Stream buffers
        self.buffers: dict[str, StreamBuffer] = {}
        self.quality_buffer = QualityMetricsBuffer()
        self.marker_buffer: list[tuple[float, str]] = []  # (timestamp, marker)
        self.marker_lock = threading.Lock()

        # Runtime
        self.inlets: dict[str, StreamInlet] = {}
        self.reader_threads: dict[str, threading.Thread] = {}
        self.running = False
        self.start_time: float | None = None
        self._stop_event = threading.Event()

        # Quality thresholds (Tier 0)
        self.thresholds = QualityThresholds.for_tier(Tier.T0)

        # Stats
        self.stats = {
            "ecg_samples": 0,
            "ppg_samples": 0,
            "imu_samples": 0,
            "markers_received": 0,
            "quality_segments": 0,
        }

    def discover_streams(self, timeout: float = 10.0) -> dict[str, Any]:
        """Discover SYNAPSE LSL streams."""
        if not pylsl:
            raise RuntimeError("pylsl not installed")

        logger.info(f"Resolving LSL streams (timeout={timeout}s)...")
        streams = resolve_streams(timeout)

        synapse_streams = {}
        for info in streams:
            name = info.name()
            if name.startswith("SYNAPSE_"):
                synapse_streams[name] = {
                    "name": name,
                    "type": info.type(),
                    "channel_count": info.channel_count(),
                    "sampling_rate": info.nominal_srate(),
                    "source_id": info.source_id(),
                    "info": info,
                }
                logger.info(
                    f"  Found: {name} ({info.type()}, {info.channel_count()}ch, {info.nominal_srate()}Hz)"
                )

        if not synapse_streams:
            logger.warning("No SYNAPSE streams found. Is firmware running and BLE connected?")

        return synapse_streams

    def setup_buffers(self, streams: dict[str, Any]) -> None:
        """Initialize stream buffers based on discovered streams."""
        max_samples_per_buffer = int(
            max(s["sampling_rate"] for s in streams.values()) * self.duration_s * 1.5
        )

        for name, info in streams.items():
            self.buffers[name] = StreamBuffer(
                name=name,
                type=info["type"],
                sampling_rate=info["sampling_rate"],
                channel_count=info["channel_count"],
                max_samples=max_samples_per_buffer,
            )
            # Create inlet
            inlet = StreamInlet(info["info"], max_buflen=3600, max_chunklen=64)
            self.inlets[name] = inlet

    def _reader_loop(self, stream_name: str) -> None:
        """Background thread to read from LSL inlet."""
        inlet = self.inlets[stream_name]
        buffer = self.buffers[stream_name]

        while not self._stop_event.is_set():
            try:
                # Pull chunk (non-blocking with timeout)
                chunk, timestamps = inlet.pull_chunk(timeout=0.1, max_samples=64)
                if chunk:
                    for sample, ts in zip(chunk, timestamps):
                        buffer.append(np.array(sample, dtype=np.float64), float(ts))
                        # Update stats
                        if "ECG" in stream_name:
                            self.stats["ecg_samples"] += 1
                        elif "PPG" in stream_name:
                            self.stats["ppg_samples"] += 1
                        elif "ACC" in stream_name or "GYRO" in stream_name or "MAG" in stream_name:
                            self.stats["imu_samples"] += 1
            except Exception as e:
                logger.debug(f"Reader error for {stream_name}: {e}")
                time.sleep(0.01)

    def _marker_reader_loop(self) -> None:
        """Read markers stream."""
        marker_stream_name = "SYNAPSE_Markers"
        if marker_stream_name not in self.inlets:
            return

        inlet = self.inlets[marker_stream_name]
        while not self._stop_event.is_set():
            try:
                chunk, timestamps = inlet.pull_chunk(timeout=0.1, max_samples=10)
                if chunk:
                    with self.marker_lock:
                        for sample, ts in zip(chunk, timestamps):
                            marker_str = (
                                sample[0] if isinstance(sample, (list, np.ndarray)) else str(sample)
                            )
                            self.marker_buffer.append((float(ts), str(marker_str)))
                            self.stats["markers_received"] += 1
            except Exception:
                time.sleep(0.01)

    def start_capture(self) -> bool:
        """Start capturing from all streams."""
        if not self.inlets:
            logger.error("No inlets configured. Run discover_streams() first.")
            return False

        self.running = True
        self.start_time = local_clock() if local_clock else time.time()
        self._stop_event.clear()

        # Start reader threads
        for name in self.inlets:
            if name == "SYNAPSE_Markers":
                t = threading.Thread(target=self._marker_reader_loop, daemon=True)
            else:
                t = threading.Thread(target=self._reader_loop, args=(name,), daemon=True)
            t.start()
            self.reader_threads[name] = t

        logger.info(f"Capture started for {self.duration_s}s...")
        return True

    def wait_for_completion(self) -> None:
        """Wait for capture duration."""
        if not self.running:
            return

        time.sleep(self.duration_s)
        self.stop_capture()

    def stop_capture(self) -> None:
        """Stop capture and join threads."""
        self.running = False
        self._stop_event.set()

        for name, thread in self.reader_threads.items():
            thread.join(timeout=2.0)

        logger.info("Capture stopped")

    def compute_quality_metrics(self) -> dict[str, Any]:
        """Compute signal quality metrics for captured segments."""
        results = {}

        # ECG quality (SYNAPSE_ECG_T0)
        ecg_name = "SYNAPSE_ECG_T0"
        if ecg_name in self.buffers:
            ecg_data, ecg_ts = self.buffers[ecg_name].get_arrays()
            if len(ecg_data) > 0:
                ecg_signal = ecg_data[:, 0] if ecg_data.ndim > 1 else ecg_data
                fs = int(self.buffers[ecg_name].sampling_rate)
                eq = compute_ecg_quality(ecg_signal, fs, thresholds=self.thresholds)
                results["ecg"] = eq.to_dict()

        # PPG quality (SYNAPSE_PPG_T0) with ACC for motion artifact
        ppg_name = "SYNAPSE_PPG_T0"
        acc_name = "SYNAPSE_ACC_T0"
        if ppg_name in self.buffers:
            ppg_data, ppg_ts = self.buffers[ppg_name].get_arrays()
            if len(ppg_data) > 0:
                ppg_signal = ppg_data[:, 0] if ppg_data.ndim > 1 else ppg_data
                fs = int(self.buffers[ppg_name].sampling_rate)

                # Get ACC magnitude for MAP
                acc_mag = None
                if acc_name in self.buffers:
                    acc_data, acc_ts = self.buffers[acc_name].get_arrays()
                    if len(acc_data) > 0 and len(acc_ts) > 0:
                        # Resample ACC to PPG rate if needed
                        if len(acc_data) != len(ppg_signal):
                            from scipy.signal import resample

                            acc_mag = np.sqrt(np.sum(acc_data**2, axis=1))
                            acc_mag = resample(acc_mag, len(ppg_signal))
                        else:
                            acc_mag = np.sqrt(np.sum(acc_data**2, axis=1))

                pq = compute_ppg_quality(ppg_signal, fs, acc_mag, thresholds=self.thresholds)
                results["ppg"] = pq

        # IMU stats
        if acc_name in self.buffers:
            acc_data, _ = self.buffers[acc_name].get_arrays()
            if len(acc_data) > 0:
                acc_mag = np.sqrt(np.sum(acc_data**2, axis=1))
                results["imu"] = {
                    "acc_magnitude_mean": float(np.mean(acc_mag)),
                    "acc_magnitude_std": float(np.std(acc_mag)),
                    "acc_magnitude_max": float(np.max(acc_mag)),
                }

        # Segment-based quality for metadata stream
        self._compute_segment_quality(results)

        return results

    def _compute_segment_quality(self, results: dict) -> None:
        """Compute quality per segment for XDF metadata."""
        segment_samples = int(self.segment_s * 500)  # ECG rate as reference

        ecg_name = "SYNAPSE_ECG_T0"
        ppg_name = "SYNAPSE_PPG_T0"
        acc_name = "SYNAPSE_ACC_T0"

        if ecg_name not in self.buffers:
            return

        ecg_data, ecg_ts = self.buffers[ecg_name].get_arrays()
        if len(ecg_data) < segment_samples:
            return

        ppg_data, ppg_ts = (np.empty(0), np.empty(0))
        if ppg_name in self.buffers:
            ppg_data, ppg_ts = self.buffers[ppg_name].get_arrays()

        acc_data, acc_ts = (np.empty(0), np.empty(0))
        if acc_name in self.buffers:
            acc_data, acc_ts = self.buffers[acc_name].get_arrays()

        # Process segments
        n_segments = len(ecg_data) // segment_samples
        for i in range(n_segments):
            start = i * segment_samples
            end = start + segment_samples

            ecg_seg = ecg_data[start:end, 0] if ecg_data.ndim > 1 else ecg_data[start:end]
            ecg_ts_seg = ecg_ts[start:end]

            seg_metrics = {
                "segment_idx": i,
                "start_time_s": float(ecg_ts_seg[0]) if len(ecg_ts_seg) > 0 else 0,
                "end_time_s": float(ecg_ts_seg[-1]) if len(ecg_ts_seg) > 0 else 0,
                "duration_s": float(ecg_ts_seg[-1] - ecg_ts_seg[0])
                if len(ecg_ts_seg) > 1
                else self.segment_s,
            }

            # ECG quality per segment
            if len(ecg_seg) > 100:
                eq = compute_ecg_quality(ecg_seg, 500, thresholds=self.thresholds)
                seg_metrics["ecg_quality"] = eq.to_dict()

            # PPG quality per segment (match timestamps)
            if len(ppg_data) > 0:
                # Find PPG samples in this time range
                if len(ppg_ts) > 0:
                    ppg_mask = (ppg_ts >= ecg_ts_seg[0]) & (ppg_ts <= ecg_ts_seg[-1])
                    if np.any(ppg_mask):
                        ppg_seg = ppg_data[ppg_mask]
                        if ppg_seg.ndim > 1:
                            ppg_seg = ppg_seg[:, 0]
                        # ACC for this segment
                        acc_mag_seg = None
                        if len(acc_data) > 0 and len(acc_ts) > 0:
                            acc_mask = (acc_ts >= ecg_ts_seg[0]) & (acc_ts <= ecg_ts_seg[-1])
                            if np.any(acc_mask):
                                acc_seg = acc_data[acc_mask]
                                acc_mag_seg = np.sqrt(np.sum(acc_seg**2, axis=1))
                                # Resample to PPG length
                                if len(acc_mag_seg) != len(ppg_seg):
                                    from scipy.signal import resample

                                    acc_mag_seg = resample(acc_mag_seg, len(ppg_seg))

                        pq = compute_ppg_quality(
                            ppg_seg, 64, acc_mag_seg, thresholds=self.thresholds
                        )
                        seg_metrics["ppg_quality"] = pq

            self.quality_buffer.add_segment(seg_metrics)
            self.stats["quality_segments"] += 1

    def write_xdf(self, quality_results: dict) -> Path:
        """Write captured data to XDF with quality metadata."""
        self.output_dir.mkdir(parents=True, exist_ok=True)

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        xdf_path = self.output_dir / f"synapse_live_{timestamp}.xdf"

        # Prepare streams for write_xdf
        streams = []

        # Data streams
        for name, buffer in self.buffers.items():
            data, timestamps = buffer.get_arrays()
            if len(data) == 0:
                continue

            stream = {
                "name": name,
                "type": buffer.type,
                "data": data,
                "timestamps": timestamps,
                "sampling_rate": buffer.sampling_rate,
                "channel_count": buffer.channel_count,
                "dropped_samples": buffer.dropped,
            }
            streams.append(stream)

        # Quality metadata stream (irregular)
        quality_segments = self.quality_buffer.get_all()
        if quality_segments:
            # Create marker-style stream for quality
            q_timestamps = [s["start_time_s"] for s in quality_segments]
            q_data = [[json.dumps(s)] for s in quality_segments]
            streams.append(
                {
                    "name": "SYNAPSE_Quality_Metadata",
                    "type": "Quality_Metadata",
                    "data": np.array(q_data, dtype=object),
                    "timestamps": np.array(q_timestamps, dtype=np.float64),
                    "sampling_rate": 0.0,  # Irregular
                    "channel_count": 1,
                    "channel_format": "string",
                }
            )

        # Markers stream
        if self.marker_buffer:
            m_timestamps = [m[0] for m in self.marker_buffer]
            m_data = [[m[1]] for m in self.marker_buffer]
            streams.append(
                {
                    "name": "SYNAPSE_Markers",
                    "type": "Markers",
                    "data": np.array(m_data, dtype=object),
                    "timestamps": np.array(m_timestamps, dtype=np.float64),
                    "sampling_rate": 0.0,
                    "channel_count": 1,
                    "channel_format": "string",
                }
            )

        logger.info(f"Writing XDF to {xdf_path} ({len(streams)} streams)...")
        write_xdf(xdf_path, streams)

        # Validate
        validation = validate_xdf(xdf_path)
        if validation["validation"]["all_streams_valid"]:
            logger.info("XDF validation: PASSED")
        else:
            logger.warning(f"XDF validation issues: {validation['validation']}")

        return xdf_path

    def print_summary(self, quality_results: dict) -> None:
        """Print capture summary."""
        logger.info("=" * 60)
        logger.info("CAPTURE SUMMARY")
        logger.info("=" * 60)
        logger.info(f"Duration: {self.duration_s}s")
        logger.info(f"ECG samples: {self.stats['ecg_samples']}")
        logger.info(f"PPG samples: {self.stats['ppg_samples']}")
        logger.info(f"IMU samples: {self.stats['imu_samples']}")
        logger.info(f"Markers received: {self.stats['markers_received']}")
        logger.info(f"Quality segments: {self.stats['quality_segments']}")

        for name, buffer in self.buffers.items():
            if buffer.dropped > 0:
                logger.warning(f"  {name}: {buffer.dropped} dropped samples")

        # Quality summary
        if "ecg" in quality_results:
            ecg = quality_results["ecg"]
            logger.info(
                f"ECG: Se={ecg.get('metrics', {}).get('ecg', {}).get('r_peak_sensitivity', 'N/A'):.3f}, "
                f"PPV={ecg.get('metrics', {}).get('ecg', {}).get('r_peak_ppv', 'N/A'):.3f}"
            )

        if "ppg" in quality_results:
            ppg = quality_results["ppg"]
            logger.info(
                f"PPG: SQI={ppg.get('ppg_sqi', 'N/A'):.3f}, "
                f"PI={ppg.get('perfusion_index', 'N/A'):.3f}%, "
                f"MAP={ppg.get('motion_artifact_prob', 'N/A'):.3f}"
            )

        logger.info("=" * 60)


def main() -> int:
    parser = argparse.ArgumentParser(description="Live LSL validation for SYNAPSE-24 Phase 1")
    parser.add_argument("--config", type=Path, default=Path("config/hardware_bringup.yaml"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/processed/live_validation"))
    parser.add_argument("--duration", type=float, default=60.0, help="Capture duration in seconds")
    parser.add_argument(
        "--segment", type=float, default=10.0, help="Quality segment duration in seconds"
    )
    parser.add_argument(
        "--discover-timeout", type=float, default=15.0, help="Stream discovery timeout"
    )
    parser.add_argument("--validate-xdf", action="store_true", help="Validate XDF after write")
    args = parser.parse_args()

    if not pylsl:
        logger.error("pylsl not installed. Install with: uv pip install pylsl")
        return 1

    validator = LiveLSLValidator(
        config_path=args.config,
        output_dir=args.output_dir,
        duration_s=args.duration,
        segment_s=args.segment,
    )

    # Discover streams
    streams = validator.discover_streams(args.discover_timeout)
    if not streams:
        logger.error("No SYNAPSE streams found. Ensure firmware is running and BLE connected.")
        return 1

    # Setup and start
    validator.setup_buffers(streams)
    if not validator.start_capture():
        return 1

    # Wait for completion
    try:
        validator.wait_for_completion()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        validator.stop_capture()

    # Compute quality
    quality_results = validator.compute_quality_metrics()

    # Write XDF
    xdf_path = validator.write_xdf(quality_results)

    # Print summary
    validator.print_summary(quality_results)

    # Validate XDF if requested
    if args.validate_xdf:
        validation = validate_xdf(xdf_path)
        if validation["validation"]["all_streams_valid"]:
            logger.info("XDF round-trip validation: PASSED")
        else:
            logger.error("XDF round-trip validation: FAILED")
            return 1

    # Save quality report
    report_path = xdf_path.with_suffix(".quality.json")
    with open(report_path, "w") as f:
        json.dump(
            {
                "timestamp": time.time(),
                "duration_s": args.duration,
                "config": str(args.config),
                "quality": quality_results,
                "stats": validator.stats,
                "xdf_validation": validation["validation"] if args.validate_xdf else None,
            },
            f,
            indent=2,
            default=str,
        )
    logger.info(f"Quality report saved to {report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
