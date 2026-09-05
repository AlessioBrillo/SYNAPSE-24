"""Tiered acquisition state machine for SYNAPSE-24."""

from .clock_sync import (
    ClockDriftEstimator,
    DriftEstimate,
    MultiPodClockSync,
    SyncConfig,
    SyncMarker,
    SyncMarkerManager,
    TimestampCorrector,
    quantify_residual_drift,
)
from .coordinator import SensorPodCoordinator
from .immobility import ImmobilityDetector
from .night_window import NightWindowScheduler
from .power_budget import NOMINAL_VOLTAGE_V, PowerBudgetManager, hours_for_charge, mah_for_power
from .state_machine import TierStateMachine, TierTransition
from .sync_marker_stream import SyncMarkerRecorder, SyncMarkerStream, SyncStreamConfig

__all__ = [
    "TierStateMachine",
    "TierTransition",
    "ImmobilityDetector",
    "NOMINAL_VOLTAGE_V",
    "PowerBudgetManager",
    "hours_for_charge",
    "mah_for_power",
    "NightWindowScheduler",
    "SensorPodCoordinator",
    "SyncConfig",
    "SyncMarker",
    "SyncMarkerManager",
    "ClockDriftEstimator",
    "TimestampCorrector",
    "MultiPodClockSync",
    "DriftEstimate",
    "quantify_residual_drift",
    "SyncMarkerStream",
    "SyncMarkerRecorder",
    "SyncStreamConfig",
]
