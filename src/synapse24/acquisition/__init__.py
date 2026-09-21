"""Tiered acquisition state machine for SYNAPSE-24."""

from synapse24.signal_quality import Tier

from .clock_sync import (
    ClockDriftEstimator,
    DriftEstimate,
    MultiPodClockSync,
    SyncConfig,
    SyncMarker,
    SyncMarkerManager,
    TierSyncBudget,
    TimestampCorrector,
    quantify_residual_drift,
)
from .coordinator import SensorPodCoordinator
from .immobility import ImmobilityDetector
from .live_lsl_sync import LiveTwoPodConfig, LSLUnavailableError, run_live_2pod_sync
from .lsl_gateway import GatewayConfig, LSLGateway, PodStreamConfig, create_pod_stream_config
from .night_window import NightWindowScheduler
from .power_budget import (
    NOMINAL_VOLTAGE_V,
    EnergyBudgetStatus,
    PowerBudgetManager,
    PowerProfile,
    hours_for_charge,
    mah_for_power,
)
from .state_machine import (
    AcquisitionController,
    MotionGateConfig,
    TierStateMachine,
    TierTransition,
    TransitionEvent,
)
from .sync_marker_stream import SyncMarkerRecorder, SyncMarkerStream, SyncStreamConfig
from .tier1_coordinator import Tier0PodState, Tier1Coordinator, Tier1PodState

__all__ = [
    "Tier",
    "TierStateMachine",
    "TierTransition",
    "AcquisitionController",
    "MotionGateConfig",
    "ImmobilityDetector",
    "NOMINAL_VOLTAGE_V",
    "PowerBudgetManager",
    "EnergyBudgetStatus",
    "PowerProfile",
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
    "TierSyncBudget",
    "quantify_residual_drift",
    "SyncMarkerStream",
    "SyncMarkerRecorder",
    "SyncStreamConfig",
    "LiveTwoPodConfig",
    "LSLUnavailableError",
    "run_live_2pod_sync",
    "TransitionEvent",
    "LSLGateway",
    "PodStreamConfig",
    "GatewayConfig",
    "create_pod_stream_config",
    "Tier1Coordinator",
    "Tier1PodState",
    "Tier0PodState",
]
