"""Cerelog ESP-EEG ingestion pipeline with LSL/XDF export for Tier 1 acquisition.

Architecture.md §67: 8-16ch EEG dry (ADS1299 x2) on frontal + behind-ear + fNIRS.
Cerelog ESP-EEG provides 8-channel ADS1299 with ESP32 (Wi-Fi/USB-C) + LiPo charging,
closed-loop active bias (DRL), BrainFlow/OpenBCI-GUI/LSL compatible.

Roadmap.md §148: Cerelog ESP-EEG ($349.99) recommended over OpenBCI for sensor-pod architecture.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import numpy.typing as npt

from synapse24.acquisition.clock_sync import MultiPodClockSync
from synapse24.hardware import BOARD_ADAPTERS, BoardConfig, BoardManager
from synapse24.signal_quality import (
    QualityThresholds,
    SignalQualityMetrics,
    Tier,
    compute_eeg_quality,
)
from synapse24.utils import (
    StreamConfig,
    create_marker_stream,
    create_quality_metadata_stream,
    create_stream_info,
    verify_xdf_roundtrip,
    write_xdf,
)

try:
    import brainflow
    from brainflow import BoardIds
except ImportError:  # pragma: no cover - brainflow optional
    brainflow = None
    BoardIds = None

logger = logging.getLogger(__name__)

# Cerelog ESP-EEG standard 10-20 electrode mapping for 8 channels
# Default: Fp1, Fp2, F3, F4, C3, C4, P3, P4 (frontal + central + parietal)
CERELOG_8CH_10_20 = ["Fp1", "Fp2", "F3", "F4", "C3", "C4", "P3", "P4"]

# Extended 16-channel mapping (PiEEG, Cyton) for future compatibility
CERELOG_16CH_10_20 = [
    "Fp1",
    "Fp2",
    "F7",
    "F3",
    "Fz",
    "F4",
    "F8",
    "T3",
    "C3",
    "Cz",
    "C4",
    "T4",
    "T5",
    "P3",
    "Pz",
    "P4",
]


@dataclass(frozen=True)
class CerelogEEGConfig:
    """Configuration for Cerelog EEG acquisition."""

    # Board identification
    board_type: str = "CERELOG_BOARD"
    board_id: str = "SYNTHETIC_BOARD"  # BrainFlow ID, overridden by adapter

    # Connection
    serial_port: str = ""
    mac_address: str = ""

    # Acquisition parameters
    sampling_rate: int = 500  # Hz - Architecture.md §92: Tier 1 requires 500Hz for EEG
    n_channels: int = 8
    channel_names: list[str] = field(default_factory=lambda: CERELOG_8CH_10_20)
    channel_units: list[str] = field(default_factory=lambda: ["µV"] * 8)

    # Impedance checking (Architecture.md §77: dry electrode impedance 10-100x gel)
    check_impedance: bool = True
    impedance_threshold_kohm: float = 50.0  # kΩ, reject channels above this
    impedance_test_duration_s: float = 5.0

    # Tier configuration (Architecture.md §33-43)
    tier: Tier = Tier.T1

    # LSL stream configuration
    stream_name: str = "SYNAPSE_EEG"
    stream_type: str = "EEG"
    source_id: str = ""

    # XDF export
    xdf_device: str = "Cerelog ESP-EEG"
    xdf_model: str = "ADS1299 8-ch"
    xdf_software: str = "synapse24"
    xdf_version: str = "0.1.0"

    def __post_init__(self) -> None:
        if not self.source_id:
            object.__setattr__(self, "source_id", f"synapse24_{self.stream_name}_{self.tier.name}")
        if len(self.channel_names) != self.n_channels:
            if self.n_channels <= 8:
                object.__setattr__(self, "channel_names", CERELOG_8CH_10_20[: self.n_channels])
            else:
                object.__setattr__(self, "channel_names", CERELOG_16CH_10_20[: self.n_channels])
        if len(self.channel_units) != self.n_channels:
            object.__setattr__(self, "channel_units", ["µV"] * self.n_channels)


@dataclass
class ImpedanceResult:
    """Electrode impedance measurement result."""

    channel: int
    channel_name: str
    impedance_kohm: float
    pass_threshold: bool
    timestamp: float


class CerelogEEGManager:
    """Manages Cerelog ESP-EEG acquisition with BrainFlow, LSL streaming, and XDF export.

    Provides:
    - Real-time LSL outlet for EEG, ACC, GYRO, MAG streams
    - Electrode impedance validation (Architecture.md §77)
    - Tier 1 signal quality assessment (flatness ≤0.3, alpha_ratio ≥1.5)
    - XDF export with zero-drop guarantee (Roadmap.md §151)
    - Integration with MultiPodClockSync for <1ms residual drift
    """

    def __init__(self, config: CerelogEEGConfig | None = None) -> None:
        self.config = config or CerelogEEGConfig()
        self._manager: BoardManager | None = None
        self._board_adapter = None
        self._lsl_outlets: dict[str, Any] = {}
        self._stream_configs: dict[str, StreamConfig] = {}
        self._impedance_results: list[ImpedanceResult] = []
        self._quality_metrics: SignalQualityMetrics | None = None
        self._is_streaming = False
        self._start_time: float | None = None

    def prepare(self) -> None:
        """Prepare the board session (connect, configure, validate)."""
        if brainflow is None:
            raise RuntimeError("brainflow not installed. Install with: pip install brainflow")

        # Get adapter and create config
        adapter_class = BOARD_ADAPTERS.get(self.config.board_type)
        if adapter_class is None:
            raise ValueError(f"Unknown board type: {self.config.board_type}")

        self._board_adapter = adapter_class()
        board_config = self._board_adapter.create_config(
            serial_port=self.config.serial_port,
            mac_address=self.config.mac_address,
            sampling_rate=self.config.sampling_rate,
        )

        # Override board_id to match BrainFlow's CERELOG_BOARD if available
        if hasattr(BoardIds, "CERELOG_BOARD"):
            board_config.board_id = "CERELOG_BOARD"

        self._manager = BoardManager(board_config)
        self._manager.prepare()

        # Create LSL stream configs
        self._create_lsl_streams()

        logger.info(
            "Cerelog EEG prepared: %d ch @ %d Hz, Tier %s, source_id=%s",
            self._manager.n_channels,
            self._manager.sampling_rate,
            self.config.tier.name,
            self.config.source_id,
        )

    def _create_lsl_streams(self) -> None:
        """Create LSL StreamInfo objects for all modalities."""
        from pylsl import StreamInfo, StreamOutlet

        # Get stream mapping from adapter
        stream_mapping = self._board_adapter.get_stream_mapping()

        for stream_key, mapping in stream_mapping.items():
            n_ch = len(mapping["channels"])
            if n_ch == 0:
                continue

            # Determine tier-specific stream type suffix
            tier_suffix = f"_T{self.config.tier.value}"

            config = StreamConfig(
                name=f"{self.config.stream_name}_{stream_key}",
                stream_type=f"{mapping['type']}{tier_suffix}",
                channel_count=n_ch,
                sampling_rate=float(self.config.sampling_rate),
                channel_format="float32",
                channel_names=[self.config.channel_names[i] for i in mapping["channels"]],
                channel_units=[mapping["unit"]] * n_ch,
                source_id=self.config.source_id,
                tier=self.config.tier.value,
                device=self.config.xdf_device,
                model=self.config.xdf_model,
                software=self.config.xdf_software,
                version=self.config.xdf_version,
            )

            outlet = StreamOutlet(
                create_stream_info(config),
                chunk_size=32,
                max_buffered=360,
            )

            self._lsl_outlets[stream_key] = outlet
            self._stream_configs[stream_key] = config

    def check_impedance(self) -> list[ImpedanceResult]:
        """Check electrode impedance on all EEG channels.

        Architecture.md §77: Dry electrode impedance is 10-100x more unstable than gel,
        especially on scalp with hair. Tier 1 requires strict impedance gating.

        Returns:
            List of ImpedanceResult for each channel.
        """
        if self._manager is None:
            raise RuntimeError("Board not prepared. Call prepare() first.")

        if not self.config.check_impedance:
            logger.info("Impedance check disabled, skipping")
            return []

        results = []

        # BrainFlow impedance check (if supported by board)
        try:
            if hasattr(self._manager._board, "get_impedance"):
                # Some boards support get_impedance()
                impedances = self._manager._board.get_impedance()
                for ch_idx, ch_name in enumerate(self.config.channel_names):
                    if ch_idx < len(impedances):
                        z_kohm = float(impedances[ch_idx])
                    else:
                        z_kohm = float("inf")
                    passed = z_kohm <= self.config.impedance_threshold_kohm
                    results.append(
                        ImpedanceResult(
                            channel=ch_idx,
                            channel_name=ch_name,
                            impedance_kohm=z_kohm,
                            pass_threshold=passed,
                            timestamp=time.time(),
                        )
                    )
            else:
                # Fallback: measure from EEG noise floor
                # Acquire short segment and estimate impedance from signal variance
                logger.warning(
                    "Board does not support direct impedance read, using noise-floor estimation"
                )
                self._manager.start_stream(
                    buffer_size=self.config.sampling_rate
                    * int(self.config.impedance_test_duration_s)
                )
                time.sleep(self.config.impedance_test_duration_s)
                data = self._manager.get_board_data()
                self._manager.stop_stream()

                if data.size > 0:
                    eeg_data = data[: self.config.n_channels, :]
                    for ch_idx, ch_name in enumerate(self.config.channel_names):
                        # Estimate impedance from signal variance (simplified)
                        # Higher noise = higher impedance (inverse relationship for driven-right-leg)
                        signal_var = np.var(eeg_data[ch_idx])
                        # Empirical mapping: var < 10 µV² ≈ good contact (<20kΩ)
                        # var > 100 µV² ≈ poor contact (>50kΩ)
                        if signal_var < 10:
                            z_kohm = 15.0
                        elif signal_var < 50:
                            z_kohm = 30.0
                        elif signal_var < 100:
                            z_kohm = 45.0
                        else:
                            z_kohm = 80.0
                        passed = z_kohm <= self.config.impedance_threshold_kohm
                        results.append(
                            ImpedanceResult(
                                channel=ch_idx,
                                channel_name=ch_name,
                                impedance_kohm=z_kohm,
                                pass_threshold=passed,
                                timestamp=time.time(),
                            )
                        )

        except Exception:
            logger.exception("Impedance check failed")
            # Fail-open for now, but log warning
            for ch_idx, ch_name in enumerate(self.config.channel_names):
                results.append(
                    ImpedanceResult(
                        channel=ch_idx,
                        channel_name=ch_name,
                        impedance_kohm=float("inf"),
                        pass_threshold=False,
                        timestamp=time.time(),
                    )
                )

        self._impedance_results = results

        passed_count = sum(1 for r in results if r.pass_threshold)
        logger.info(
            "Impedance check: %d/%d channels passed (threshold %.1f kΩ)",
            passed_count,
            len(results),
            self.config.impedance_threshold_kohm,
        )

        return results

    def get_impedance_report(self) -> dict[str, Any]:
        """Get impedance check report as dictionary."""
        return {
            "threshold_kohm": self.config.impedance_threshold_kohm,
            "total_channels": len(self._impedance_results),
            "passed": sum(1 for r in self._impedance_results if r.pass_threshold),
            "failed": sum(1 for r in self._impedance_results if not r.pass_threshold),
            "details": [
                {
                    "channel": r.channel,
                    "name": r.channel_name,
                    "impedance_kohm": r.impedance_kohm,
                    "passed": r.pass_threshold,
                }
                for r in self._impedance_results
            ],
        }

    def start_streaming(self, buffer_size: int = 450000) -> None:
        """Start real-time LSL streaming."""
        if self._manager is None:
            raise RuntimeError("Board not prepared. Call prepare() first.")

        if self._is_streaming:
            logger.warning("Already streaming")
            return

        self._manager.start_stream(buffer_size)
        self._is_streaming = True
        self._start_time = time.time()
        logger.info("Cerelog EEG streaming started on %d LSL outlets", len(self._lsl_outlets))

    def stop_streaming(self) -> None:
        """Stop real-time LSL streaming."""
        if self._manager and self._is_streaming:
            self._manager.stop_stream()
            self._is_streaming = False
            logger.info("Cerelog EEG streaming stopped")

    def push_lsl_chunk(self, num_samples: int | None = None) -> int:
        """Push latest data chunk to LSL outlets.

        Args:
            num_samples: Number of samples to pull (None = all available)

        Returns:
            Number of samples pushed.
        """
        if self._manager is None or not self._is_streaming:
            return 0

        data = self._manager.get_board_data(num_samples)
        if data.size == 0:
            return 0

        n_samples = data.shape[1]
        timestamps = self._manager.get_board_timestamp()

        if len(timestamps) != n_samples:
            # Fallback: generate timestamps
            timestamps = np.linspace(
                self._start_time or time.time(),
                self._start_time + n_samples / self.config.sampling_rate,
                n_samples,
            )

        # Get stream mapping from adapter
        stream_mapping = self._board_adapter.get_stream_mapping()

        for stream_key, mapping in stream_mapping.items():
            if stream_key not in self._lsl_outlets:
                continue

            channel_indices = mapping["channels"]
            if max(channel_indices) >= data.shape[0]:
                continue

            stream_data = data[channel_indices, :].T  # (n_samples, n_channels)
            self._lsl_outlets[stream_key].push_chunk(
                stream_data.astype(np.float32),
                timestamps.astype(np.float64),
            )

        return n_samples

    def acquire_blocking(
        self,
        duration_s: float,
        check_quality: bool = True,
        clock_sync: MultiPodClockSync | None = None,
        pod_id: str | None = None,
    ) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64], dict[str, Any]]:
        """Acquire a blocking EEG segment for offline processing/validation.

        Args:
            duration_s: Duration to acquire in seconds
            check_quality: Run Tier 1 signal quality assessment
            clock_sync: Optional MultiPodClockSync for drift correction (Architecture.md §92)
            pod_id: Pod identifier for clock_sync (required if clock_sync provided)

        Returns:
            Tuple of (eeg_data, timestamps, quality_report)
            eeg_data: (n_channels, n_samples)
            timestamps: (n_samples,) in hub LSL clock domain (drift-corrected if clock_sync provided)
        """
        if self._manager is None:
            raise RuntimeError("Board not prepared. Call prepare() first.")

        # Start stream if not already
        was_streaming = self._is_streaming
        if not was_streaming:
            self.start_streaming()

        try:
            n_samples = int(duration_s * self.config.sampling_rate)
            buffer_size = n_samples + int(self.config.sampling_rate * 2)  # extra buffer
            self._manager.start_stream(buffer_size)
            time.sleep(duration_s)
            data = self._manager.get_board_data(n_samples)
            timestamps = self._manager.get_board_timestamp()

            if len(timestamps) != data.shape[1]:
                timestamps = np.linspace(time.time(), time.time() + duration_s, data.shape[1])

            eeg_data = data[: self.config.n_channels, :]

            # Apply clock drift correction if clock_sync provided (Architecture.md §92: <1ms residual)
            if clock_sync is not None and pod_id is not None:
                timestamps = clock_sync.correct_pod_timestamps(pod_id, timestamps)

            # Quality assessment
            quality_report = {}
            if check_quality:
                quality_report = self.assess_quality(eeg_data, timestamps)

            return eeg_data, timestamps, quality_report

        finally:
            if not was_streaming:
                self.stop_streaming()

    def assess_quality(
        self,
        eeg_data: npt.NDArray[np.float64],
        timestamps: npt.NDArray[np.float64] | None = None,
    ) -> dict[str, Any]:
        """Assess EEG signal quality against Tier 1 thresholds.

        Architecture.md §74, §99: Tier 1 strict thresholds:
        - spectral_flatness ≤ 0.3 (high-density scalp, eyes-closed alpha dominant)
        - alpha_ratio ≥ 1.5 (resting eyes-closed alpha power > total)

        Args:
            eeg_data: (n_channels, n_samples) EEG data in µV
            timestamps: Optional timestamps for duration calculation

        Returns:
            Quality report with per-channel and aggregate metrics.
        """
        n_channels, n_samples = eeg_data.shape
        fs = self.config.sampling_rate

        per_channel = {}
        all_flatness = []
        all_alpha_ratio = []

        for ch_idx in range(min(n_channels, self.config.n_channels)):
            ch_name = self.config.channel_names[ch_idx]
            signal = eeg_data[ch_idx]

            flatness = spectral_flatness(signal, fs)
            alpha_ratio = alpha_band_power_ratio(signal, fs)

            all_flatness.append(flatness)
            all_alpha_ratio.append(alpha_ratio)

            thresholds = QualityThresholds.for_tier(Tier.T1)
            per_channel[ch_name] = {
                "spectral_flatness": flatness,
                "alpha_ratio": alpha_ratio,
                "flatness_pass": flatness <= thresholds.spectral_flatness_max,
                "alpha_ratio_pass": alpha_ratio >= thresholds.alpha_ratio_min,
            }

        # Aggregate metrics (median across channels)
        median_flatness = float(np.median(all_flatness))
        median_alpha_ratio = float(np.median(all_alpha_ratio))

        thresholds = QualityThresholds.for_tier(Tier.T1)
        overall_pass = (
            median_flatness <= thresholds.spectral_flatness_max
            and median_alpha_ratio >= thresholds.alpha_ratio_min
        )

        # Create SignalQualityMetrics for compatibility
        metrics = SignalQualityMetrics(
            modality="eeg",
            tier=Tier.T1,
            sampling_rate_hz=fs,
            duration_s=duration_s if timestamps is not None else n_samples / fs,
            spectral_flatness=median_flatness,
            alpha_band_ratio=median_alpha_ratio,
        )
        metrics.thresholds = thresholds
        self._quality_metrics = metrics

        report = {
            "tier": self.config.tier.name,
            "overall_pass": overall_pass,
            "median_spectral_flatness": median_flatness,
            "median_alpha_ratio": median_alpha_ratio,
            "flatness_threshold": thresholds.spectral_flatness_max,
            "alpha_ratio_threshold": thresholds.alpha_ratio_min,
            "per_channel": per_channel,
            "metrics_dict": metrics.to_dict(),
        }

        logger.info(
            "EEG Quality (Tier 1): flatness=%.3f (thresh≤%.1f), alpha_ratio=%.2f (thresh≥%.1f) -> %s",
            median_flatness,
            thresholds.spectral_flatness_max,
            median_alpha_ratio,
            thresholds.alpha_ratio_min,
            "PASS" if overall_pass else "FAIL",
        )

        return report

    def export_xdf(
        self,
        output_path: Path,
        eeg_data: npt.NDArray[np.float64] | None = None,
        timestamps: npt.NDArray[np.float64] | None = None,
        include_quality: bool = True,
        include_markers: bool = True,
        clock_sync: MultiPodClockSync | None = None,
        pod_id: str | None = None,
    ) -> dict[str, Any]:
        """Export acquired data to XDF with zero-drop guarantee.

        Roadmap.md §151: LSL/XDF from day one - any dropped packet fails loudly.

        Args:
            output_path: Output .xdf file path
            eeg_data: EEG data (n_channels, n_samples), if None uses last acquisition
            timestamps: Timestamps (n_samples,), if None generates synthetic
            include_quality: Include quality metadata stream
            include_markers: Include marker stream
            clock_sync: Optional MultiPodClockSync for drift correction (Architecture.md §92)
            pod_id: Pod identifier for clock_sync (required if clock_sync provided)

        Returns:
            Verification report from verify_xdf_roundtrip
        """
        if eeg_data is None:
            # Acquire a test block if no data provided
            eeg_data, timestamps, _ = self.acquire_blocking(10.0, check_quality=False)

        if timestamps is None:
            timestamps = np.linspace(
                0, eeg_data.shape[1] / self.config.sampling_rate, eeg_data.shape[1]
            )

        # Apply clock drift correction if clock_sync provided (Architecture.md §92: <1ms residual)
        if clock_sync is not None and pod_id is not None:
            timestamps = clock_sync.correct_pod_timestamps(pod_id, timestamps)

        n_samples, n_channels = eeg_data.shape[1], eeg_data.shape[0]

        # Build streams list for XDF
        streams = []

        # EEG stream
        eeg_config = StreamConfig(
            name=self.config.stream_name,
            stream_type="EEG",
            channel_count=n_channels,
            sampling_rate=float(self.config.sampling_rate),
            channel_format="float32",
            channel_names=self.config.channel_names[:n_channels],
            channel_units=self.config.channel_units[:n_channels],
            source_id=self.config.source_id,
            tier=self.config.tier.value,
            device=self.config.xdf_device,
            model=self.config.xdf_model,
        )
        streams.append(
            {
                "data": eeg_data.T.astype(np.float32),  # (n_samples, n_channels)
                "timestamps": timestamps,
                "info": eeg_config,
            }
        )

        # Add ACC, GYRO, MAG if available from board
        stream_mapping = self._board_adapter.get_stream_mapping() if self._board_adapter else {}
        for stream_key in ["ACC", "GYRO", "MAG"]:
            if stream_key in stream_mapping:
                mapping = stream_mapping[stream_key]
                ch_indices = mapping["channels"]
                if max(ch_indices) < eeg_data.shape[0]:
                    aux_data = eeg_data[ch_indices, :].T
                    aux_config = StreamConfig(
                        name=f"{self.config.stream_name}_{stream_key}",
                        stream_type=stream_key,
                        channel_count=len(ch_indices),
                        sampling_rate=float(self.config.sampling_rate),
                        channel_format="float32",
                        channel_names=[self.config.channel_names[i] for i in ch_indices],
                        channel_units=[mapping["unit"]] * len(ch_indices),
                        source_id=self.config.source_id,
                        tier=self.config.tier.value,
                    )
                    streams.append(
                        {
                            "data": aux_data.astype(np.float32),
                            "timestamps": timestamps,
                            "info": aux_config,
                        }
                    )

        # Quality metadata stream
        if include_quality and self._quality_metrics:
            quality_stream = create_quality_metadata_stream(
                self._quality_metrics.to_dict(),
                stream_name=f"{self.config.stream_name}_Quality",
                source_id=self.config.source_id,
            )
            streams.append(quality_stream)

        # Marker stream (session start/end, impedance check, etc.)
        if include_markers:
            markers = [
                (timestamps[0], "session_start"),
                (timestamps[-1], "session_end"),
            ]
            for r in self._impedance_results:
                markers.append(
                    (
                        r.timestamp,
                        f"impedance_ch{r.channel}_{r.channel_name}_{r.impedance_kohm:.1f}kΩ",
                    )
                )
            marker_stream = create_marker_stream(
                markers, stream_name=f"{self.config.stream_name}_Markers"
            )
            streams.append(marker_stream)

        # Write and verify
        logger.info("Writing XDF to %s with %d streams", output_path, len(streams))
        verify_report = verify_xdf_roundtrip(streams, output_path)

        if not verify_report["all_streams_valid"]:
            raise RuntimeError(
                f"XDF round-trip verification failed: {verify_report['total_dropped']} samples dropped"
            )

        logger.info(
            "XDF export verified: %d streams, %d samples, zero drops",
            verify_report["n_streams"],
            verify_report["total_recovered"],
        )

        return verify_report

    def release(self) -> None:
        """Release board resources."""
        self.stop_streaming()
        if self._manager:
            self._manager.release()
            self._manager = None
        logger.info("Cerelog EEG released")


# Convenience functions for pipeline integration


def create_cerelog_eeg_config(
    serial_port: str = "",
    mac_address: str = "",
    sampling_rate: int = 500,
    n_channels: int = 8,
    tier: Tier = Tier.T1,
) -> CerelogEEGConfig:
    """Create Cerelog EEG configuration from parameters."""
    return CerelogEEGConfig(
        serial_port=serial_port,
        mac_address=mac_address,
        sampling_rate=sampling_rate,
        n_channels=n_channels,
        tier=tier,
    )


def ingest_cerelog_eeg(
    config: CerelogEEGConfig,
    duration_s: float,
    output_dir: Path,
    session_name: str | None = None,
    clock_sync: MultiPodClockSync | None = None,
    pod_id: str | None = None,
) -> dict[str, Any]:
    """Full Cerelog EEG ingestion pipeline: acquire -> quality -> XDF.

    Args:
        config: CerelogEEGConfig instance
        duration_s: Acquisition duration in seconds
        output_dir: Output directory for XDF and reports
        session_name: Optional session identifier
        clock_sync: Optional MultiPodClockSync for drift correction (Architecture.md §92)
        pod_id: Pod identifier for clock_sync (required if clock_sync provided)

    Returns:
        Dictionary with acquisition report, quality metrics, and XDF verification.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    session_name = session_name or f"cerelog_eeg_{int(time.time())}"
    xdf_path = output_dir / f"{session_name}.xdf"
    report_path = output_dir / f"{session_name}_report.json"

    manager = CerelogEEGManager(config)
    try:
        manager.prepare()

        # Impedance check
        impedance_results = manager.check_impedance()
        impedance_report = manager.get_impedance_report()

        # Acquire data with drift correction if clock_sync provided
        eeg_data, timestamps, quality_report = manager.acquire_blocking(
            duration_s=duration_s,
            check_quality=True,
            clock_sync=clock_sync,
            pod_id=pod_id,
        )

        # Export XDF with drift correction if clock_sync provided
        xdf_report = manager.export_xdf(
            xdf_path, eeg_data, timestamps, clock_sync=clock_sync, pod_id=pod_id
        )

        # Compile full report
        full_report = {
            "session": session_name,
            "config": {
                "board_type": config.board_type,
                "sampling_rate": config.sampling_rate,
                "n_channels": config.n_channels,
                "tier": config.tier.name,
                "duration_s": duration_s,
            },
            "impedance": impedance_report,
            "quality": quality_report,
            "xdf_verification": xdf_report,
            "output_files": {
                "xdf": str(xdf_path),
                "report": str(report_path),
            },
        }

        # Save report
        with open(report_path, "w") as f:
            json.dump(full_report, f, indent=2, default=str)

        logger.info("Cerelog EEG ingestion complete: %s", session_name)
        return full_report

    finally:
        manager.release()


# Re-export signal quality functions for convenience
from synapse24.signal_quality.eeg import alpha_band_power_ratio, band_power, spectral_flatness

__all__ = [
    "CERELOG_8CH_10_20",
    "CERELOG_16CH_10_20",
    "CerelogEEGConfig",
    "ImpedanceResult",
    "CerelogEEGManager",
    "create_cerelog_eeg_config",
    "ingest_cerelog_eeg",
    "spectral_flatness",
    "alpha_band_power_ratio",
    "band_power",
]
