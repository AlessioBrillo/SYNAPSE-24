"""SYNAPSE-24: 24/7 Multimodal Bio-Sensing Wearable Platform.

Phase 0: Software & Public Data Foundation

Public API:
- Ingestion: WESAD, MIT-BIH, Sleep-EDF, DEAP dataset pipelines
- Signal Quality: ECG, PPG, EEG, fNIRS, EDA metrics with tier-aware thresholds
- Edge AI: Model training, quantization (INT8/Float16), TFLM deployment
- Acquisition: Tier state machine (T0/T1/T2), power budget, clock sync
- LSL/XDF: Stream management, XDF I/O, validation
- Hardware: Device registry, sensor pod coordination
"""

__version__ = "0.1.0"
__author__ = "SYNAPSE-24 Team"

# Signal Quality
# Acquisition
from synapse24.acquisition import (
    AcquisitionController,
    ImmobilityDetector,
    MotionGateConfig,
    NightWindowScheduler,
    PowerBudgetManager,
    SensorPodCoordinator,
    TierStateMachine,
    TierTransition,
    TransitionEvent,
)

# Edge AI
from synapse24.edge_ai import (
    EdgeModel,
    ModelConfig,
    ModelType,
    QuantizationConfig,
    QuantizationResult,
    TargetPlatform,
    estimate_inference_latency,
    quantize_model,
    save_quantization_artifacts,
)

# Hardware
from synapse24.hardware import (
    BoardAdapter,
    BoardConfig,
    BoardManager,
    BoardState,
    CerelogAdapter,
    DeviceRegistry,
    EmotiBitAdapter,
    MuseSAdapter,
    OpenBCICytonAdapter,
    OpenBCIGanglionAdapter,
    PiEEGAdapter,
    SensorPodConfig,
    SyntheticBoardAdapter,
    SyntheticPlaybackAdapter,
)

# Ingestion
from synapse24.ingestion import (
    Tier as IngestionTier,  # re-export
)
from synapse24.ingestion import (
    download_mitbih,
    download_wesad,
    extract_chest_signals,
    extract_wrist_signals,
    ingest_mitbih,
    ingest_wesad,
    load_mitbih_record,
    load_wesad_subject,
    process_mitbih_record,
    process_wesad_subject,
)
from synapse24.signal_quality import (
    QualityThresholds,
    SignalQualityMetrics,
    Tier,
    alpha_band_power_ratio,
    compute_ecg_quality,
    compute_eeg_quality,
    compute_hrv_metrics,
    compute_ppg_quality,
    compute_ppg_sqi,
    compute_snr,
    detect_r_peaks_neurokit,
    perfusion_index,
    ppg_motion_artifact_probability,
    r_peak_detection_quality,
    rmssd_mae,
    spectral_flatness,
)

# LSL/XDF
from synapse24.utils import (
    LSLStreamManager,
    StreamConfig,
    create_marker_stream,
    create_quality_metadata_stream,
    create_stream_info,
    create_stream_info_from_dict,
    generate_synthetic_timestamps,
    validate_xdf,
    verify_xdf_roundtrip,
    write_xdf,
)

__all__ = [
    # Version
    "__version__",
    # Signal Quality
    "Tier",
    "QualityThresholds",
    "SignalQualityMetrics",
    "compute_ecg_quality",
    "compute_ppg_quality",
    "compute_eeg_quality",
    "detect_r_peaks_neurokit",
    "r_peak_detection_quality",
    "compute_hrv_metrics",
    "rmssd_mae",
    "perfusion_index",
    "compute_ppg_sqi",
    "ppg_motion_artifact_probability",
    "spectral_flatness",
    "alpha_band_power_ratio",
    "compute_snr",
    # Ingestion
    "IngestionTier",
    "ingest_wesad",
    "ingest_mitbih",
    "process_wesad_subject",
    "process_mitbih_record",
    "extract_chest_signals",
    "extract_wrist_signals",
    "load_wesad_subject",
    "load_mitbih_record",
    "download_wesad",
    "download_mitbih",
    # Edge AI
    "ModelConfig",
    "ModelType",
    "TargetPlatform",
    "EdgeModel",
    "QuantizationConfig",
    "QuantizationResult",
    "quantize_model",
    "save_quantization_artifacts",
    "estimate_inference_latency",
    # Acquisition
    "TierStateMachine",
    "AcquisitionController",
    "TierTransition",
    "TransitionEvent",
    "MotionGateConfig",
    "ImmobilityDetector",
    "NightWindowScheduler",
    "PowerBudgetManager",
    "SensorPodCoordinator",
    # LSL/XDF
    "StreamConfig",
    "LSLStreamManager",
    "create_stream_info",
    "create_stream_info_from_dict",
    "generate_synthetic_timestamps",
    "write_xdf",
    "validate_xdf",
    "verify_xdf_roundtrip",
    "create_marker_stream",
    "create_quality_metadata_stream",
    # Hardware
    "BoardConfig",
    "BoardManager",
    "BoardState",
    "BoardAdapter",
    "SensorPodConfig",
    "DeviceRegistry",
    "SyntheticBoardAdapter",
    "SyntheticPlaybackAdapter",
    "EmotiBitAdapter",
    "CerelogAdapter",
    "PiEEGAdapter",
    "OpenBCIGanglionAdapter",
    "OpenBCICytonAdapter",
    "MuseSAdapter",
]
