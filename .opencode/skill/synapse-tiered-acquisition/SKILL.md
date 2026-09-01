---
name: synapse-tiered-acquisition
description: Tier 0/1/2 state machine, IMU-based trigger logic, power budget enforcement for SYNAPSE-24 decoupled sensor/hub architecture.
when_to_use: Implementing tiered acquisition logic, IMU-based sleep/rest detection, power budget tracking, automatic tier transitions, sensor pod state management.
user-invocable: true
---

# SYNAPSE Tiered Acquisition Skill

This skill implements the three-tier acquisition strategy from Architecture.md §33-43. It manages the state machine for transitioning between continuous low-power monitoring (Tier 0), high-density neuro acquisition during rest/sleep (Tier 1), and on-demand calibration sessions (Tier 2).

## Tier Definitions (Architecture.md Table)

| Tier | Modality | Channels | When | Power | Purpose |
|------|----------|----------|------|-------|---------|
| **T0** | PPG multi-λ, IMU 9-axis, temp, bioZ, 1-2ch EEG in-ear | Reduced | Always (H24) | µW–low mW | Temporal coverage, context detection |
| **T1** | EEG 6-16ch frontal+post-auricular, fNIRS prefrontal, ECG rest | Max density | Immobility >X min or night window | 10s of mW | Clean neuro data when artifacts minimal |
| **T2** | ECG single-lead, cognitive tasks with fNIRS+EEG, calibration | Targeted | User-initiated or protocol | Burst | Labeled high-quality data for training |

## State Machine

```
                    ┌─────────────────────┐
                    │      TIER 0         │ ◄──────────────────────┐
                    │  (Continuous H24)   │                        │
                    │  PPG + IMU + Temp   │                        │
                    │  1-2ch EEG in-ear   │                        │
                    └──────────┬──────────┘                        │
                               │                                   │
              IMU immobility > │                                   │
              threshold OR     │                                   │
              night window     ▼                                   │
                    ┌─────────────────────┐                        │
                    │      TIER 1         │                        │
                    │  (High-density)     │                        │
                    │  EEG 6-16ch + fNIRS │                        │
                    │  ECG rest           │                        │
                    └──────────┬──────────┘                        │
                               │                                   │
              Movement detected│                                   │
              OR window end     ▼                                   │
                    ┌─────────────────────┐                        │
                    │      TIER 0         │ ───────────────────────┘
                    └─────────────────────┘

TIER 2: User-initiated from any state (parallel, not exclusive)
```

## IMU-Based Trigger Logic

### Immobility Detection (Tier 0 → Tier 1)
```python
from synapse24.acquisition import ImmobilityDetector

detector = ImmobilityDetector(
    accel_sampling_rate=100,  # Hz
    window_duration_s=30,  # 30-second windows
    magnitude_threshold=0.02,  # g-force threshold
    min_immobility_min=5,  # 5 minutes continuous
)

# Called on each IMU sample
if detector.update(accel_magnitude):
    # Trigger Tier 1 activation
    acquisition_controller.promote_to_tier1(reason="immobility")
```

### Night Window (Tier 0 → Tier 1)
```python
from synapse24.acquisition import NightWindowScheduler

scheduler = NightWindowScheduler(
    sleep_window_start="22:00",
    sleep_window_end="07:00",
    timezone="Europe/Rome",
    pre_sleep_buffer_min=30,
    post_wake_buffer_min=15,
)

# Check periodically
if scheduler.in_sleep_window():
    acquisition_controller.promote_to_tier1(reason="night_window")
```

## Power Budget Enforcement

Per Architecture.md §55-62: Energy budget is the **real constraint**, not sensor count.

```python
from synapse24.acquisition import PowerBudgetManager

budget = PowerBudgetManager(
    hub_battery_mah=3000,
    target_lifetime_h=24,
    tier0_avg_mw=5,  # Continuous baseline
    tier1_avg_mw=50,  # During sessions
    tier1_max_h=10,  # Max Tier 1 hours per day
    tier2_burst_mw=100,  # Short bursts
)

# Before promoting to Tier 1
if not budget.can_afford_tier1(duration_h=2):
    logger.warning("Insufficient energy budget for Tier 1 session")
    return False

# Track actual consumption
budget.record_tier1_session(actual_duration_h=2.5)
```

## Sensor Pod Coordination

Architecture.md §27-30: Decoupled pods (head, in-ear, forearm) + hub.

```python
from synapse24.acquisition import SensorPodCoordinator

coordinator = SensorPodCoordinator(
    pods={
        "head": {"type": "EEG_fNIRS", "tier": 1, "ble_address": "..."},
        "in_ear": {"type": "EEG_T0", "tier": 0, "ble_address": "..."},
        "forearm": {"type": "PPG_ECG_IMU", "tier": 0, "ble_address": "..."},
    },
    hub="forearm",  # Hub also a pod (has battery)
)

# Sync all pods to hub clock via LSL
coordinator.sync_all_to_hub()
```

## Quality Gates

- [ ] Tier transitions logged with trigger reason + timestamp
- [ ] Power budget never exceeded (hard limit)
- [ ] IMU detector: <5% false positive rate on validation data
- [ ] Night window respects timezone + DST
- [ ] All pods stream LSL within 10ms of hub clock

## References
- Architecture.md §33-43 (tier definitions), §55-62 (energy budget), §83-86 (next steps)
- Roadmap.md §138-140 (Phase 1-2 milestones)
- Guermandi et al., IEEE EMBC 2022 (600h earbud EEG)
- SleePyCo: ADS1298 + STM32, 150mW, 24.6h on 1000mAh