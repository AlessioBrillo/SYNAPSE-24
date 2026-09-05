"""Ingestion pipelines for public datasets."""

from synapse24.signal_quality import Tier

from .deap import (
    download_deap,
    ingest_deap,
    load_deap_subject,
    process_deap_subject,
)
from .mitbih import (
    download_mitbih,
    ingest_mitbih,
    load_mitbih_record,
    process_mitbih_record,
)
from .sleep_edf import (
    download_sleep_edf,
    ingest_sleep_edf,
    load_sleep_edf_record,
    process_sleep_edf_subject,
)
from .wesad import (
    FUSION_WINDOW_CONFIG,
    FUSION_WINDOW_FEATURE_NAMES,
    FUSION_WINDOW_LABEL_TO_ID,
    FusionWindow,
    compute_accel_magnitude,
    download_wesad,
    extract_chest_signals,
    extract_native_rate_fusion_windows,
    extract_native_rate_fusion_windows_for_subject,
    extract_wrist_signals,
    fusion_window_quality_to_features,
    ingest_wesad,
    load_wesad_subject,
    process_wesad_subject,
    resample_labels,
    segment_by_label,
)

__all__ = [
    "FUSION_WINDOW_CONFIG",
    "FUSION_WINDOW_FEATURE_NAMES",
    "FUSION_WINDOW_LABEL_TO_ID",
    "compute_accel_magnitude",
    "download_deap",
    "download_mitbih",
    "download_sleep_edf",
    "download_wesad",
    "extract_chest_signals",
    "extract_native_rate_fusion_windows",
    "extract_native_rate_fusion_windows_for_subject",
    "extract_wrist_signals",
    "fusion_window_quality_to_features",
    "ingest_deap",
    "ingest_mitbih",
    "ingest_sleep_edf",
    "ingest_wesad",
    "load_deap_subject",
    "load_mitbih_record",
    "load_sleep_edf_record",
    "load_wesad_subject",
    "process_deap_subject",
    "process_mitbih_record",
    "process_sleep_edf_subject",
    "process_wesad_subject",
    "resample_labels",
    "segment_by_label",
    "FusionWindow",
    "Tier",
]
