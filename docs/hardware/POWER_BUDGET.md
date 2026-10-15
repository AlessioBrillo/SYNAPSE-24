# POWER BUDGET — kit measured, system targeted, and why they are different numbers

Architecture.md makes the energy budget a functional requirement: **the 24 h
Tier-0 feasibility claim must rest on measurement, not on a tier model**. This
document separates the two numbers that used to be conflated, so that the G1
measurement cannot fail against a target the rig was never built to meet, and
the target cannot quietly borrow credibility from a rig that was never meant
to demonstrate it.

## Two budgets, never one column

| | **Budget KIT** (what G1 measures) | **Budget SYSTEM** (what the 24 h claim targets) |
|---|---|---|
| Scope | Smoke-kit rig as bought: ESP32-S3 + AD8232 + MAX30102 + ICM-20948 + INA219 | Optimised pod per Architecture.md: on-device processing, compress-before-transmit |
| Streaming | Raw, uncompressed, BLE connected continuously (~2.3–2.4 kB/s) | Compressed / duty-cycled radio (§55-62, gate **G3**) |
| Expected draw | **200–500 mW** (see derivation below) | **5 mW avg / 10 mW peak** (T0 profile) |
| Proven by | `data/live_validation/gate_report.json` → `measured_tier0_power_mw` (POWER stream, INA219) | Not yet — G3 after G1 |
| Gates | G1 rows 2 and 11 (vs. the kit range) | G3 (post-G1), separate gate, separate criterion |

The G1 gate compares the measured draw against the **kit range** (derivation
below, ±tolerance). The `hardware.yaml` T0 profile (5 mW) stays the target of
record and is labelled *"target, not demonstrated by G1"* everywhere it
appears — README, PROCUREMENT, BRINGUP_SOP.

## Derivation of the kit range (component level, @3.7 V)

| Block | Profile | Estimated current | Basis |
|---|---|---|---|
| ESP32-S3, CPU active + BLE connected | continuous, ~2.4 kB/s sustained, MTU 247 + DLE | 60–100 mA | typical S3 active+radio figures; the radio, not the analogue, dominates — Architecture.md §55-62 says exactly this |
| MAX30102 | red+IR, LED current 0x1F (≈51 mA), 411 µs pulse @50 Hz, ~2 % duty per LED | 4–7 mA | LED peak × duty + AFE |
| ICM-20948 | 9-axis @50 Hz | 2–3 mA | datasheet all-axis-active current |
| AD8232 | analog front-end | 0.5–1 mA | datasheet |
| INA219 | continuous bus+shunt conversion | 0.5–1 mA | datasheet |
| **Total** | | **≈70–115 mA → 250–425 mW** | |

**Budget-KIT envelope: 150–600 mW (≈40–160 mA @3.7 V).** A measurement
outside the envelope fails the row and demands an explanation (a forgotten
LED current setting, a bench mistake, or a genuine defect). This is the
number G1 judges. Numbers from the earlier 5 mW figure are *not* a
disqualifier for the kit — they are simply not what this rig demonstrates.

## Battery arithmetic for the SOP (the honest constraint)

`PROCUREMENT.md` buys a 500 mAh LiPo for the soak. At the kit envelope:

| Draw | 500 mAh runtime | 6 h SOP soak? |
|---|---|---|
| 250 mW | ~7.4 h | yes |
| 425 mW | ~4.3 h | **no — the soak would die mid-run** |

So the SOP now states the runtime explicitly before the soak starts, and the
kit carries **one 1000–2000 mAh LiPo** (≈€8–12) for the 6-hour session. A
soak that dies from battery is not a failed gate; it is a gate that never
ran — both wasted, one of them silently.

## What the measurement feeds

1. **Row 11 is automatic.** The POWER stream travels inside the XDF, so
   `gate_report.json` gets `measured_tier0_current_ma` / `measured_tier0_power_mw`
   / `measured_battery_pct` (mean/min/max) from the recording itself. The
   bench PSU row stays a hand-recorded cross-check, not the only source.
2. **G2 criterion #4 is rewritten.** It no longer asks "does the measured T0
   current agree with ≤5 mW" (unanswerable for this rig). It asks:
   *does the kit's measured draw validate the `power_budget` *model* within a
   factor the battery math can absorb?* Concretely: measured mW per recorded
   hour → projected runtime on the hub's declared capacity vs. the 24 h target,
   with the gap explicitly attributed to compression (G3) and duty-cycling.
3. **The 24 h claim** stays a model statement until G3 measures the
   compressed path. The model's inputs are the measured numbers from G1 —
   which is exactly what a model is for.
