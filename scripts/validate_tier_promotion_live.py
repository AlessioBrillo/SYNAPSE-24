#!/usr/bin/env python3
"""Live validation of T0<->T1 promotion cycle with motion-quality gate.

Architecture.md 33-43: Three-tier acquisition with IMU-triggered promotion
gated by measured PPG quality (SQI >= 0.5, MAP <= 0.5).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from synapse24.acquisition import (
    AcquisitionController,
    ImmobilityDetector,
    MotionGateConfig,
    NightWindowScheduler,
    PowerBudgetManager,
    Tier,
    TierStateMachine,
)
from synapse24.hardware import SyntheticBoardAdapter


class TestClock:
    """Mutable test clock for simulation."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, dt: float) -> None:
        self.value += dt


def _create_controller(test_clock: TestClock) -> AcquisitionController:
    """Create acquisition controller with test-friendly parameters."""
    return AcquisitionController(
        immobility_detector=ImmobilityDetector(
            accel_sampling_rate=1,
            window_duration_s=1.0,
            magnitude_threshold=0.02,
            min_immobility_min=1.0,
        ),
        night_scheduler=None,
        power_budget=PowerBudgetManager(
            hub_battery_mah=3000,
            target_lifetime_h=24,
            tier0_avg_mw=5.0,
            tier1_avg_mw=50.0,
            tier1_max_h=10.0,
            tier2_avg_mw=100.0,
            tier2_max_burst_min=30.0,
            reserve_mah=300,
            clock_fn=test_clock,
        ),
        motion_gate=MotionGateConfig(
            sqi_min=0.5,
            map_max=0.5,
            required_consecutive_clean=2,
            sqi_staleness_max_s=30.0,
            fail_open=False,
        ),
        clock_fn=test_clock,
    )


def _run_phase(
    controller: AcquisitionController,
    test_clock: TestClock,
    report: dict[str, Any],
    steps: int,
    accel_magnitude: float,
    ppg_sqi: float,
    motion_artifact_prob: float,
    target_tier: Tier | None = None,
) -> bool:
    """Run a simulation phase and return True if target tier reached."""
    for step in range(steps):
        controller.update_imu(
            accel_magnitude=accel_magnitude,
            timestamp=test_clock.value,
            ppg_sqi=ppg_sqi,
            motion_artifact_prob=motion_artifact_prob,
        )
        controller.tick(test_clock.value)

        # Snapshot motion gate state every 10s
        if step % 10 == 0:
            report["motion_gate_snapshots"].append({
                "t": test_clock.value,
                "armed": controller.get_status()["motion_gate"]["armed"],
                "consecutive_clean": controller.get_status()["motion_gate"]["consecutive_clean"],
                "latest_sqi": controller.get_status()["motion_gate"]["latest_sqi"],
                "latest_map": controller.get_status()["motion_gate"]["latest_map"],
            })

        # Snapshot power budget every 30s
        if step % 30 == 0:
            report["power_budget_snapshots"].append(
                controller.power_budget.get_status().__dict__
            )

        if target_tier is not None and controller.state_machine.current_tier == target_tier:
            event_name = "T0->T1" if target_tier == Tier.T1 else "T1->T0"
            reason = "immobility_detected" if target_tier == Tier.T1 else "movement_detected"
            report["transitions"].append({
                "t": test_clock.value,
                "event": event_name,
                "reason": reason,
                "metadata": controller.state_machine.transition_history[-1].metadata,
            })
            return True

        test_clock.advance(1.0)

    return False


def run_promotion_validation() -> dict[str, Any]:
    """Run complete T0<->T1 promotion validation scenario.

    Returns:
        Dict with transitions, power budget, motion gate state, and pass/fail.
    """
    test_clock = TestClock()

    # 1. Setup synthetic hardware (CI-safe)
    board = SyntheticBoardAdapter()

    # 2. Configure acquisition controller per Architecture.md
    controller = _create_controller(test_clock)

    # 3. Simulate scenario: stationary+clean -> movement+dirty -> stationary+clean
    report: dict[str, Any] = {
        "transitions": [],
        "power_budget_snapshots": [],
        "motion_gate_snapshots": [],
        "checks": {},
    }

    # --- Phase 1: Stationary + clean PPG (should arm motion gate, then promote) ---
    phase1_promoted = _run_phase(
        controller, test_clock, report,
        steps=400, accel_magnitude=0.01, ppg_sqi=0.8, motion_artifact_prob=0.1,
        target_tier=Tier.T1,
    )

    # --- Phase 2: Movement + dirty PPG (should demote T1->T0) ---
    phase2_demoted = _run_phase(
        controller, test_clock, report,
        steps=200, accel_magnitude=1.5, ppg_sqi=0.2, motion_artifact_prob=0.8,
        target_tier=Tier.T0,
    )

    # --- Phase 3: Stationary + clean again (should re-promote T0->T1) ---
    phase3_repromoted = _run_phase(
        controller, test_clock, report,
        steps=400, accel_magnitude=0.01, ppg_sqi=0.8, motion_artifact_prob=0.1,
        target_tier=Tier.T1,
    )

    # --- Phase 4: Test motion gate blocks promotion when SQI < 0.5 ---
    controller.state_machine.reset()
    controller._consecutive_clean = 0
    controller._consecutive_contaminated = 0
    controller._latest_sqi = 0.3
    controller._latest_map = 0.7
    controller._latest_sqi_timestamp = test_clock.value

    gate_blocked = True
    for _ in range(100):
        controller.update_imu(
            accel_magnitude=0.01,
            timestamp=test_clock.value,
            ppg_sqi=0.3,
            motion_artifact_prob=0.7,
        )
        controller.tick(test_clock.value)

        if controller.state_machine.current_tier == Tier.T1:
            gate_blocked = False
            break

        test_clock.advance(1.0)

    # Final status
    report["final_tier"] = controller.state_machine.current_tier.name
    report["final_power_budget"] = controller.power_budget.get_status().__dict__
    report["final_motion_gate"] = controller.get_status()["motion_gate"]
    report["transition_history"] = [
        {
            "from": e.from_tier.name,
            "to": e.to_tier.name,
            "reason": e.reason,
            "timestamp": e.timestamp,
            "metadata": e.metadata,
        }
        for e in controller.state_machine.transition_history
    ]

    # Pass/fail checks
    report["checks"] = {
        "phase1_promoted_on_clean_immobility": phase1_promoted,
        "phase2_demoted_on_movement": phase2_demoted,
        "phase3_repromoted_after_recovery": phase3_repromoted,
        "motion_gate_blocks_low_sqi": gate_blocked,
        "all_transitions_recorded": len(report["transitions"]) >= 3,
    }

    return report


def main() -> int:
    """Run validation and exit with appropriate code."""
    print("=" * 60)
    print("SYNAPSE-24 Tier Promotion Live Validation")
    print("Architecture.md 33-43, 74")
    print("=" * 60)

    report = run_promotion_validation()

    # Save report
    output_path = Path("tier_promotion_report.json")
    output_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nReport saved to: {output_path}")

    # Print summary
    print("\n" + "=" * 60)
    print("VALIDATION SUMMARY")
    print("=" * 60)
    for check, passed in report["checks"].items():
        status = "[PASS]" if passed else "[FAIL]"
        print(f"  {status}: {check}")

    print(f"\nFinal tier: {report['final_tier']}")
    print(f"Total transitions: {len(report['transition_history'])}")
    for t in report["transition_history"]:
        print(f"  {t['from']} -> {t['to']} @ t={t['timestamp']:.1f}s ({t['reason']})")

    all_passed = all(report["checks"].values())
    print("\n" + "=" * 60)
    print("OVERALL: " + ("[ALL CHECKS PASSED]" if all_passed else "[SOME CHECKS FAILED]"))
    print("=" * 60)

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
