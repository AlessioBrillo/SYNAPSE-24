# mypy: ignore-errors
"""Tier state machine for SYNAPSE-24 acquisition."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from synapse24.signal_quality import Tier


def _default_clock() -> float:
    """LSL clock domain (Guardian Principle 3), wall-time fallback for CI."""
    try:
        from pylsl import local_clock

        return float(local_clock())
    except Exception:
        return float(time.time())


class TierTransition(Enum):
    """Possible tier transitions."""

    T0_TO_T1_IMMOBILITY = "t0_to_t1_immobility"
    T0_TO_T1_NIGHT_WINDOW = "t0_to_t1_night_window"
    T1_TO_T0_MOVEMENT = "t1_to_t0_movement"
    T1_TO_T0_WINDOW_END = "t1_to_t0_window_end"
    T0_TO_T2_USER = "t0_to_t2_user"
    T1_TO_T2_USER = "t1_to_t2_user"
    T2_TO_T0_DONE = "t2_to_t0_done"
    T2_TO_T1_DONE = "t2_to_t1_done"


@dataclass
class TransitionEvent:
    """Record of a tier transition."""

    from_tier: Tier
    to_tier: Tier
    transition: TierTransition
    timestamp: float
    reason: str
    metadata: dict = field(default_factory=dict)


@dataclass
class MotionGateConfig:
    """Motion-quality gate for Tier 0 -> Tier 1 promotion.

    Architecture.md §74: motion artifact is the dominant EEG/fNIRS failure
    mode, so promotion must be gated on measured PPG quality, not just
    immobility + power budget. Defaults match the Tier-0 literature
    thresholds (QualityThresholds.for_tier(T0): Karlen et al. 2013 wearable
    PPG SQI >= 0.5; MAP tolerance <= 0.5 for ambulatory context).
    """

    sqi_min: float = 0.5
    map_max: float = 0.5
    required_consecutive_clean: int = 2


class TierStateMachine:
    """State machine for managing tiered acquisition.

    Architecture.md §33-43: Three-tier acquisition strategy.
    - Tier 0: Continuous H24 (PPG, IMU, temp, 1-2ch EEG in-ear)
    - Tier 1: High-density rest/sleep (EEG 6-16ch, fNIRS, ECG rest)
    - Tier 2: On-demand sessions (cognitive tasks, calibration)
    """

    def __init__(
        self,
        initial_tier: Tier = Tier.T0,
        on_transition: Callable[[TransitionEvent], None] | None = None,
        clock_fn: Callable[[], float] | None = None,
    ) -> None:
        self._current_tier = initial_tier
        self._previous_tier: Tier | None = None
        self._on_transition = on_transition
        self._clock: Callable[[], float] = clock_fn or _default_clock
        self._transition_history: list[TransitionEvent] = []
        self._t1_start_time: float | None = None
        self._t2_start_time: float | None = None

    @property
    def current_tier(self) -> Tier:
        return self._current_tier

    @property
    def previous_tier(self) -> Tier | None:
        return self._previous_tier

    @property
    def transition_history(self) -> list[TransitionEvent]:
        return self._transition_history.copy()

    def is_tier0(self) -> bool:
        return self._current_tier == Tier.T0

    def is_tier1(self) -> bool:
        return self._current_tier == Tier.T1

    def is_tier2(self) -> bool:
        return self._current_tier == Tier.T2

    def _transition(
        self,
        new_tier: Tier,
        transition: TierTransition,
        reason: str,
        metadata: dict | None = None,
    ) -> bool:
        """Execute a tier transition."""
        if self._current_tier == new_tier:
            return False

        now = self._clock()
        event = TransitionEvent(
            from_tier=self._current_tier,
            to_tier=new_tier,
            transition=transition,
            timestamp=now,
            reason=reason,
            metadata=metadata or {},
        )

        old_tier = self._current_tier
        self._previous_tier = old_tier
        self._current_tier = new_tier
        self._transition_history.append(event)

        # Track Tier 1/2 start times for power budget (Architecture.md §55-62).
        # Durations are per continuous segment: leaving a tier clears its
        # timer, entering (re)arms it. Check old_tier (not _current_tier,
        # which already equals new_tier here).
        if new_tier == Tier.T1:
            self._t1_start_time = now
        elif old_tier == Tier.T1:
            self._t1_start_time = None

        if new_tier == Tier.T2:
            self._t2_start_time = now
        elif old_tier == Tier.T2:
            self._t2_start_time = None

        if self._on_transition:
            try:
                self._on_transition(event)
            except Exception:
                pass  # Don't let callback errors break state machine

        return True

    def promote_to_tier1(self, reason: str, metadata: dict | None = None) -> bool:
        """Promote from Tier 0 to Tier 1."""
        if self._current_tier != Tier.T0:
            return False

        if "night" in reason.lower():
            transition = TierTransition.T0_TO_T1_NIGHT_WINDOW
        else:
            transition = TierTransition.T0_TO_T1_IMMOBILITY

        return self._transition(Tier.T1, transition, reason, metadata)

    def demote_to_tier0(self, reason: str, metadata: dict | None = None) -> bool:
        """Demote from Tier 1 to Tier 0."""
        if self._current_tier != Tier.T1:
            return False

        if "movement" in reason.lower():
            transition = TierTransition.T1_TO_T0_MOVEMENT
        else:
            transition = TierTransition.T1_TO_T0_WINDOW_END

        return self._transition(Tier.T0, transition, reason, metadata)

    def start_tier2(self, reason: str, metadata: dict | None = None) -> bool:
        """Start Tier 2 session (from Tier 0 or Tier 1)."""
        if self._current_tier == Tier.T0:
            transition = TierTransition.T0_TO_T2_USER
        elif self._current_tier == Tier.T1:
            transition = TierTransition.T1_TO_T2_USER
        else:
            return False  # Already in Tier 2

        return self._transition(Tier.T2, transition, reason, metadata)

    def end_tier2(self, reason: str, metadata: dict | None = None) -> bool:
        """End Tier 2 session, return to previous tier."""
        if self._current_tier != Tier.T2:
            return False

        if self._previous_tier == Tier.T1:
            transition = TierTransition.T2_TO_T1_DONE
        else:
            transition = TierTransition.T2_TO_T0_DONE

        return self._transition(self._previous_tier or Tier.T0, transition, reason, metadata)

    def get_tier1_duration(self) -> float | None:
        """Get current Tier 1 session duration in seconds."""
        if self._t1_start_time is None:
            return None
        return self._clock() - self._t1_start_time

    def get_tier2_duration(self) -> float | None:
        """Get current Tier 2 session duration in seconds."""
        if self._t2_start_time is None:
            return None
        return self._clock() - self._t2_start_time

    def reset(self) -> None:
        """Reset state machine to Tier 0."""
        self._current_tier = Tier.T0
        self._previous_tier = None
        self._transition_history.clear()
        self._t1_start_time = None
        self._t2_start_time = None


class AcquisitionController:
    """High-level acquisition controller coordinating all components.

    Integrates:
    - TierStateMachine
    - ImmobilityDetector
    - NightWindowScheduler
    - PowerBudgetManager
    - SensorPodCoordinator
    """

    def __init__(
        self,
        immobility_detector: ImmobilityDetector | None = None,
        night_scheduler: NightWindowScheduler | None = None,
        power_budget: PowerBudgetManager | None = None,
        pod_coordinator: SensorPodCoordinator | None = None,
        clock_fn: Callable[[], float] | None = None,
        motion_gate: MotionGateConfig | None = None,
    ) -> None:
        self._clock: Callable[[], float] = clock_fn or _default_clock
        self.state_machine = TierStateMachine(
            on_transition=self._on_transition, clock_fn=self._clock
        )
        self.immobility_detector = immobility_detector
        self.night_scheduler = night_scheduler
        self.power_budget = power_budget
        self.pod_coordinator = pod_coordinator
        self.motion_gate = motion_gate or MotionGateConfig()

        self._last_imu_update = 0.0
        self._last_night_check = 0.0
        self._night_check_interval = 60.0  # Check night window every minute

        # Latest motion-quality assessment (None = unmeasured, fail-open).
        self._latest_sqi: float | None = None
        self._latest_map: float | None = None
        self._consecutive_clean = 0
        self._consecutive_contaminated = 0

    def _on_transition(self, event: TransitionEvent) -> None:
        """Handle tier transition - coordinate pods."""
        if self.pod_coordinator:
            self.pod_coordinator.on_tier_change(event)

    def update_motion_quality(
        self,
        ppg_sqi: float | None,
        motion_artifact_prob: float | None,
        timestamp: float | None = None,
    ) -> None:
        """Feed a PPG motion-quality assessment into the promotion gate.

        Architecture.md §74: promotion requires measured-clean signal.
        A streak of required_consecutive_clean assessments arms promotion;
        a streak of contaminated assessments while in Tier 1 demotes back
        to Tier 0 (movement-demotion path). Missing values are fail-open
        (unmeasured, counters untouched).
        """
        if ppg_sqi is None or motion_artifact_prob is None:
            return

        self._latest_sqi = ppg_sqi
        self._latest_map = motion_artifact_prob

        if self._is_motion_clean(ppg_sqi, motion_artifact_prob):
            self._consecutive_clean += 1
            self._consecutive_contaminated = 0
        else:
            self._consecutive_contaminated += 1
            self._consecutive_clean = 0

        if (
            self.state_machine.is_tier1()
            and self._consecutive_contaminated >= self.motion_gate.required_consecutive_clean
        ):
            self.state_machine.demote_to_tier0(
                "movement_detected",
                metadata={
                    "sqi": ppg_sqi,
                    "map": motion_artifact_prob,
                    "consecutive_contaminated": self._consecutive_contaminated,
                },
            )
            self._consecutive_contaminated = 0

    def _is_motion_clean(self, sqi: float, map_score: float) -> bool:
        """Check a single assessment against the motion gate thresholds."""
        return sqi >= self.motion_gate.sqi_min and map_score <= self.motion_gate.map_max

    def _motion_gate_armed(self) -> bool:
        """True when no quality measured yet (fail-open) or streak is clean."""
        if self._latest_sqi is None or self._latest_map is None:
            return True
        return self._consecutive_clean >= self.motion_gate.required_consecutive_clean

    def update_imu(
        self,
        accel_magnitude: float,
        timestamp: float | None = None,
        *,
        ppg_sqi: float | None = None,
        motion_artifact_prob: float | None = None,
    ) -> None:
        """Update IMU-based immobility detection.

        Optional ppg_sqi/motion_artifact_prob are assessed inline through
        the motion gate (Architecture.md §74). When omitted, the legacy
        immobility + power-budget path is preserved (fail-open).
        """
        if self.immobility_detector is None:
            return

        if timestamp is None:
            timestamp = self._clock()

        self._last_imu_update = timestamp

        if ppg_sqi is not None or motion_artifact_prob is not None:
            self.update_motion_quality(ppg_sqi, motion_artifact_prob, timestamp)

        if self.immobility_detector.update(accel_magnitude, timestamp):
            if self.state_machine.is_tier0() and self.power_budget:
                if self.power_budget.can_afford_tier1(duration_h=2) and self._motion_gate_armed():
                    metadata = {}
                    if self._latest_sqi is not None and self._latest_map is not None:
                        metadata = {
                            "sqi": self._latest_sqi,
                            "map": self._latest_map,
                            "consecutive_clean": self._consecutive_clean,
                        }
                    self.state_machine.promote_to_tier1("immobility_detected", metadata)
                else:
                    # Log power budget / motion-gate rejection
                    pass

    def check_night_window(self, timestamp: float | None = None) -> None:
        """Check if we should enter/exit night window."""
        if self.night_scheduler is None:
            return

        if timestamp is None:
            timestamp = self._clock()

        # Throttle checks
        if timestamp - self._last_night_check < self._night_check_interval:
            return

        self._last_night_check = timestamp

        in_window = self.night_scheduler.in_sleep_window(timestamp)

        if in_window and self.state_machine.is_tier0():
            if self.power_budget is None or self.power_budget.can_afford_tier1(
                duration_h=self.night_scheduler.estimate_window_duration(timestamp)
            ):
                self.state_machine.promote_to_tier1("night_window_start")
        elif not in_window and self.state_machine.is_tier1():
            # Check if we're in night window mode (not immobility)
            recent_transitions = [
                e
                for e in self.state_machine.transition_history[-5:]
                if e.transition == TierTransition.T0_TO_T1_NIGHT_WINDOW
            ]
            if recent_transitions:
                self.state_machine.demote_to_tier0("night_window_end")

    def request_tier2(self, reason: str, metadata: dict | None = None) -> bool:
        """Request a Tier 2 session."""
        return self.state_machine.start_tier2(reason, metadata)

    def end_tier2(self, reason: str, metadata: dict | None = None) -> bool:
        """End a Tier 2 session."""
        return self.state_machine.end_tier2(reason, metadata)

    def tick(self, timestamp: float | None = None) -> None:
        """Periodic update - call regularly (e.g., every second)."""
        self.check_night_window(timestamp)

        # Check Tier 1 timeout (if power budget says time's up)
        if self.state_machine.is_tier1() and self.power_budget:
            t1_duration = self.state_machine.get_tier1_duration()
            if t1_duration and not self.power_budget.can_afford_tier1(
                duration_h=t1_duration / 3600
            ):
                self.state_machine.demote_to_tier0("power_budget_exceeded")

    def get_status(self) -> dict:
        """Get comprehensive status."""
        return {
            "current_tier": self.state_machine.current_tier.name,
            "previous_tier": self.state_machine.previous_tier.name
            if self.state_machine.previous_tier
            else None,
            "tier1_duration_s": self.state_machine.get_tier1_duration(),
            "tier2_duration_s": self.state_machine.get_tier2_duration(),
            "transition_count": len(self.state_machine.transition_history),
            "recent_transitions": [
                {
                    "from": e.from_tier.name,
                    "to": e.to_tier.name,
                    "reason": e.reason,
                    "timestamp": e.timestamp,
                }
                for e in self.state_machine.transition_history[-5:]
            ],
            "power_budget": self.power_budget.get_status() if self.power_budget else None,
            "motion_gate": {
                "sqi_min": self.motion_gate.sqi_min,
                "map_max": self.motion_gate.map_max,
                "required_consecutive_clean": self.motion_gate.required_consecutive_clean,
                "latest_sqi": self._latest_sqi,
                "latest_map": self._latest_map,
                "consecutive_clean": self._consecutive_clean,
                "armed": self._motion_gate_armed(),
            },
        }
