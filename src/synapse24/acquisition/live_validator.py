"""Real-time Tier 0 quality validator for live LSL streams."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

try:
    from pylsl import StreamInlet, local_clock, resolve_byprop
except ImportError:  # pragma: no cover
    StreamInlet = None
    resolve_byprop = None
    local_clock = None

from synapse24.signal_quality import QualityThresholds, SignalQualityMetrics, Tier
from synapse24.utils import (
    LSLStreamManager,
    StreamConfig,
    create_stream_info,
    generate_synthetic_timestamps,
    write_xdf,
)


@dataclass
class ValidatorConfig:
    """Configuration for live Tier 0 validator."""

    # Stream names to connect to
    ecg_stream_name: str = "SYNAPSE_ECG_T0"
    ppg_stream_name: str = "SYNAPSE_PPG_T0"
    acc_stream_name: str = "SYNAPSE_ACC_T0"

    # Quality assessment windows
    ecg_window_s: float = 10.0
    ppg_window_s: float = 30.0

    # Output
    output_dir: Path = Path("data/live_validation")
    save_xdf: bool = True
    save_json: bool = True

    # Tier for thresholds
    tier: Tier = Tier.T0

    # Max duration
    max_duration_s: float | None = 300.0  # 5 minutes default


class LiveTier0Validator:
    """Connects to LSL streams, computes real-time quality metrics, logs to XDF."""

    def __init__(self, config: ValidatorConfig | None = None) -> None:
        self.config = config or ValidatorConfig()
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

        self._inlets: dict[str, StreamInlet] = {}
        self._thresholds = QualityThresholds.for_tier(self.config.tier)
        self._running = False

        # Buffers for quality computation
        self._ecg_buffer: list[float] = []
        self._ppg_red_buffer: list[float] = []
        self._ppg_ir_buffer: list[float] = []
        self._acc_mag_buffer: list[float] = []

        # Timestamps for XDF export
        self._ecg_timestamps: list[float] = []
        self._ppg_timestamps: list[float] = []
        self._acc_timestamps: list[float] = []

        # Quality results
        self._quality_results: list[dict[str, Any]] = []
        self._start_time: float = 0.0

    def connect(self) -> dict[str, bool]:
        """Resolve and connect to LSL streams."""
        if resolve_byprop is None or StreamInlet is None:
            raise RuntimeError("pylsl not installed")

        results = {}

        # Connect to ECG
        try:
            streams = resolve_byprop("name", self.config.ecg_stream_name, timeout=5.0)
            if streams:
                self._inlets["ecg"] = StreamInlet(streams[0], max_buflen=360)
                results["ecg"] = True
            else:
                results["ecg"] = False
        except Exception:
            results["ecg"] = False

        # Connect to PPG
        try:
            streams = resolve_byprop("name", self.config.ppg_stream_name, timeout=5.0)
            if streams:
                self._inlets["ppg"] = StreamInlet(streams[0], max_buflen=360)
                results["ppg"] = True
            else:
                results["ppg"] = False
        except Exception:
            results["ppg"] = False

        # Connect to ACC
        try:
            streams = resolve_byprop("name", self.config.acc_stream_name, timeout=5.0)
            if streams:
                self._inlets["acc"] = StreamInlet(streams[0], max_buflen=360)
                results["acc"] = True
            else:
                results["acc"] = False
        except Exception:
            results["acc"] = False

        return results

    def run(self, duration_s: float | None = None) -> dict[str, Any]:
        """Run validation loop for specified duration."""
        if not self._inlets:
            raise RuntimeError("No streams connected. Call connect() first.")

        self._running = True
        self._start_time = local_clock() if local_clock else time.time()

        max_duration = duration_s or self.config.max_duration_s or 300.0
        end_time = self._start_time + max_duration

        sample_counts = {"ecg": 0, "ppg": 0, "acc": 0}

        print(f"Starting live Tier 0 validation for {max_duration:.0f}s...")
        print(f"Connected streams: {list(self._inlets.keys())}")

        try:
            while self._running and (local_clock() if local_clock else time.time()) < end_time:
                self._process_streams(sample_counts)

                # Periodic status
                elapsed = (local_clock() if local_clock else time.time()) - self._start_time
                if int(elapsed) % 30 == 0 and elapsed > 0:
                    print(f"  [{elapsed:.0f}s] ECG: {sample_counts['ecg']} samples, "
                          f"PPG: {sample_counts['ppg']}, ACC: {sample_counts['acc']}")

        except KeyboardInterrupt:
            print("\nValidation interrupted by user")
        finally:
            self._running = False
            for inlet in self._inlets.values():
                inlet.close_stream()

        return self._finalize()

    def _process_streams(self, sample_counts: dict[str, int]) -> None:
        """Pull samples from all inlets and process."""
        current_time = local_clock() if local_clock else time.time()

        # Process ECG
        if "ecg" in self._inlets:
            sample, timestamp = self._inlets["ecg"].pull_sample(timeout=0.01)
            if sample is not None:
                self._ecg_buffer.append(sample[0])
                self._ecg_timestamps.append(timestamp)
                sample_counts["ecg"] += 1

                # Check ECG quality window
                if len(self._ecg_buffer) >= int(self.config.ecg_window_s * self._get_ecg_fs()):
                    self._assess_ecg_quality(current_time)

        # Process PPG
        if "ppg" in self._inlets:
            sample, timestamp = self._inlets["ppg"].pull_sample(timeout=0.01)
            if sample is not None:
                self._ppg_red_buffer.append(sample[0])
                self._ppg_ir_buffer.append(sample[1] if len(sample) > 1 else 0.0)
                self._ppg_timestamps.append(timestamp)
                sample_counts["ppg"] += 1

                # Check PPG quality window
                if len(self._ppg_red_buffer) >= int(self.config.ppg_window_s * self._get_ppg_fs()):
                    self._assess_ppg_quality(current_time)

        # Process ACC
        if "acc" in self._inlets:
            sample, timestamp = self._inlets["acc"].pull_sample(timeout=0.01)
            if sample is not None:
                acc_mag = np.sqrt(sum(s**2 for s in sample[:3]))
                self._acc_mag_buffer.append(acc_mag)
                self._acc_timestamps.append(timestamp)
                sample_counts["acc"] += 1

    def _get_ecg_fs(self) -> int:
        """Get ECG sampling rate from stream info."""
        if "ecg" in self._inlets:
            info = self._inlets["ecg"].info()
            return int(info.nominal_srate())
        return 250

    def _get_ppg_fs(self) -> int:
        """Get PPG sampling rate from stream info."""
        if "ppg" in self._inlets:
            info = self._inlets["ppg"].info()
            return int(info.nominal_srate())
        return 100

    def _assess_ecg_quality(self, timestamp: float) -> None:
        """Assess ECG quality on current buffer."""
        ecg_arr = np.array(self._ecg_buffer, dtype=np.float64)
        fs = self._get_ecg_fs()

        from synapse24.signal_quality import compute_ecg_quality

        quality = compute_ecg_quality(ecg_arr, fs, thresholds=self._thresholds)

        result = {
            "timestamp": timestamp,
            "modality": "ecg",
            "quality": quality.to_dict(),
            "buffer_len": len(ecg_arr),
        }
        self._quality_results.append(result)

        # Log pass/fail
        evals = quality.evaluate()
        overall = quality.overall_pass()
        print(f"  ECG Quality @ {timestamp:.1f}s: PASS={overall}, "
              f"Se={evals.get('r_peak_sensitivity', 'N/A')}, "
              f"PPV={evals.get('r_peak_ppv', 'N/A')}")

        # Keep overlap for continuity
        keep_samples = int(fs * 5)  # 5 seconds overlap
        self._ecg_buffer = self._ecg_buffer[-keep_samples:]
        self._ecg_timestamps = self._ecg_timestamps[-keep_samples:]

    def _assess_ppg_quality(self, timestamp: float) -> None:
        """Assess PPG quality on current buffer."""
        ppg_arr = np.array(self._ppg_red_buffer, dtype=np.float64)
        fs = self._get_ppg_fs()

        accel_mag = np.array(self._acc_mag_buffer, dtype=np.float64) if self._acc_mag_buffer else None

        from synapse24.signal_quality import compute_ppg_quality

        quality = compute_ppg_quality(ppg_arr, fs, accel_mag, self._thresholds)

        result = {
            "timestamp": timestamp,
            "modality": "ppg",
            "quality": quality,
            "buffer_len": len(ppg_arr),
        }
        self._quality_results.append(result)

        evals = quality.get("evaluations", {}) if "evaluations" in quality else {}
        overall = quality.get("overall_pass", False) if "overall_pass" in quality else False
        print(f"  PPG Quality @ {timestamp:.1f}s: PASS={overall}, "
              f"SQI={quality.get('ppg_sqi', 'N/A'):.3f}, "
              f"PI={quality.get('perfusion_index', 'N/A'):.3f}, "
              f"MAP={quality.get('motion_artifact_prob', 'N/A'):.3f}")

        # Keep overlap
        keep_samples = int(fs * 10)
        self._ppg_red_buffer = self._ppg_red_buffer[-keep_samples:]
        self._ppg_ir_buffer = self._ppg_ir_buffer[-keep_samples:]
        self._ppg_timestamps = self._ppg_timestamps[-keep_samples:]
        if self._acc_mag_buffer:
            self._acc_mag_buffer = self._acc_mag_buffer[-keep_samples:]

    def _finalize(self) -> dict[str, Any]:
        """Finalize validation, export XDF and JSON."""
        elapsed = (local_clock() if local_clock else time.time()) - self._start_time

        # Prepare streams for XDF
        streams = []

        if self._ecg_buffer and self.config.save_xdf:
            ecg_data = np.array(self._ecg_buffer, dtype=np.float64).reshape(-1, 1)
            ecg_ts = np.array(self._ecg_timestamps, dtype=np.float64)
            ecg_info = create_stream_info(StreamConfig(
                name=self.config.ecg_stream_name,
                stream_type="ECG_T0",
                channel_count=1,
                sampling_rate=self._get_ecg_fs(),
                channel_names=["ECG"],
                channel_units=["µV"],
                tier=self.config.tier.value,
            ))
            streams.append({
                "info": ecg_info,
                "data": ecg_data,
                "timestamps": ecg_ts,
            })

        if self._ppg_red_buffer and self.config.save_xdf:
            ppg_data = np.column_stack([
                np.array(self._ppg_red_buffer, dtype=np.float64),
                np.array(self._ppg_ir_buffer, dtype=np.float64),
            ])
            ppg_ts = np.array(self._ppg_timestamps, dtype=np.float64)
            ppg_info = create_stream_info(StreamConfig(
                name=self.config.ppg_stream_name,
                stream_type="PPG_T0",
                channel_count=2,
                sampling_rate=self._get_ppg_fs(),
                channel_names=["PPG_RED", "PPG_IR"],
                channel_units=["a.u.", "a.u."],
                tier=self.config.tier.value,
            ))
            streams.append({
                "info": ppg_info,
                "data": ppg_data,
                "timestamps": ppg_ts,
            })

        if self._acc_mag_buffer and self.config.save_xdf:
            acc_data = np.array(self._acc_mag_buffer, dtype=np.float64).reshape(-1, 1)
            acc_ts = np.array(self._acc_timestamps, dtype=np.float64)
            acc_info = create_stream_info(StreamConfig(
                name=self.config.acc_stream_name,
                stream_type="ACC_T0",
                channel_count=1,
                sampling_rate=self._get_imu_fs(),
                channel_names=["ACC_MAG"],
                channel_units=["g"],
                tier=self.config.tier.value,
            ))
            streams.append({
                "info": acc_info,
                "data": acc_data,
                "timestamps": acc_ts,
            })

        # Quality metadata stream
        if self._quality_results and self.config.save_xdf:
            from synapse24.utils import create_quality_metadata_stream

            # Aggregate all quality results
            agg_quality = {
                "session_duration_s": elapsed,
                "ecg_assessments": [r for r in self._quality_results if r["modality"] == "ecg"],
                "ppg_assessments": [r for r in self._quality_results if r["modality"] == "ppg"],
                "thresholds": self._thresholds.to_dict(),
            }
            quality_stream = create_quality_metadata_stream(
                agg_quality,
                stream_name="SYNAPSE_Quality_T0",
            )
            streams.append(quality_stream)

        # Export XDF
        xdf_path = None
        if streams and self.config.save_xdf:
            xdf_path = self.config.output_dir / f"live_tier0_{int(self._start_time)}.xdf"
            write_xdf(xdf_path, streams)
            print(f"XDF saved to: {xdf_path}")

        # Export JSON summary
        json_path = None
        if self.config.save_json:
            json_path = self.config.output_dir / f"live_tier0_{int(self._start_time)}.json"
            summary = {
                "session": {
                    "start_time": self._start_time,
                    "duration_s": elapsed,
                    "tier": self.config.tier.value,
                    "sample_counts": {
                        "ecg": len(self._ecg_buffer),
                        "ppg": len(self._ppg_red_buffer),
                        "acc": len(self._acc_mag_buffer),
                    },
                },
                "quality_results": self._quality_results,
                "xdf_path": str(xdf_path) if xdf_path else None,
            }
            with open(json_path, "w") as f:
                json.dump(summary, f, indent=2, default=str)
            print(f"JSON summary saved to: {json_path}")

        return {
            "session_duration_s": elapsed,
            "quality_assessments": len(self._quality_results),
            "xdf_path": str(xdf_path) if xdf_path else None,
            "json_path": str(json_path) if json_path else None,
        }

    def _get_imu_fs(self) -> int:
        """Get IMU sampling rate from stream info."""
        if "acc" in self._inlets:
            info = self._inlets["acc"].info()
            return int(info.nominal_srate())
        return 100


def run_live_validation(
    ecg_stream: str = "SYNAPSE_ECG_T0",
    ppg_stream: str = "SYNAPSE_PPG_T0",
    acc_stream: str = "SYNAPSE_ACC_T0",
    duration_s: float = 300.0,
    output_dir: Path = Path("data/live_validation"),
    tier: Tier = Tier.T0,
) -> dict[str, Any]:
    """Convenience function to run live Tier 0 validation."""
    config = ValidatorConfig(
        ecg_stream_name=ecg_stream,
        ppg_stream_name=ppg_stream,
        acc_stream_name=acc_stream,
        max_duration_s=duration_s,
        output_dir=output_dir,
        tier=tier,
    )

    validator = LiveTier0Validator(config)
    connected = validator.connect()

    print(f"Connected streams: {connected}")
    if not any(connected.values()):
        raise RuntimeError("No LSL streams found. Ensure ESP32 is streaming.")

    return validator.run(duration_s=duration_s)
