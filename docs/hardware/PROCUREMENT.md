# PROCUREMENT — What to buy, when, and on what evidence

This document is the output of the Hardware Readiness Gate. It exists so that
every euro spent is justified by a measurement already taken, not by a
prediction in `Roadmap.md`.

Scope is deliberately limited to **G1 and G2** (the smoke kit and the first EEG
rig). In-ear EEG, the forearm band, the compute hub, fNIRS and the EmotiBit
reference are deliberately excluded: each one has its own gate, and buying it
before the previous gate closes is how a project ends up with hardware nobody
can use.

## Gate summary

| Gate | What it unlocks | Evidence required | State |
|---|---|---|---|
| **G0** | Any firmware work at all | `tools/check_toolchain.py` green; `firmware/esp32_band` compiles `band-core` in CI | ✅ **CLOSED 2026-10-12** — ESP-IDF v5.5.5 on the dev host; `band-core` and `band-full` both compile locally; CI pins v5.5.5 |
| **G1** | Order the Phase 2 rig (Cerelog) | Smoke kit passes the measurement SOP below | ⛔ Not started |
| **G2** | Anything beyond EEG (in-ear, hub, fNIRS, EmotiBit) | G1 + a full night of Cerelog co-recording in XDF | ⛔ Not started |

**G0 is the only thing standing in front of G1 and G2.** It was a software
problem with a zero-euro fix: install ESP-IDF and let the CI build job prove
the firmware. **Closed 2026-10-12** — see `docs/hardware/ESP-IDF_SETUP.md`
(pinned v5.5.5, reproducible download/submodule scripts under
`scripts/tools/`). Buying sensors before the firmware compiles means paying to
discover bench problems that cost nothing to find now — the first local
compile found three real defects (see the commit that closed the gate).

---

## G1 — Smoke kit (order now)

Deliberately the *minimum* that proves the sensor → ADC → BLE → LSL → XDF
chain. GPS, temperature, FIT and OTA are explicitly **not** part of this: they
are product features, and needing them to boot would block bringup on parts
you have not bought. The `band-core` build variant exists for exactly this
reason.

| # | Part | Qty | Spec that matters | Est. EUR | Why this one |
|---|---|---|---|---|---|
| 1 | ESP32-S3 DevKitC-1 | 1 | **N8R8: 8 MB Flash + 8 MB PSRAM** | 12–15 | PSRAM is mandatory for the TFLM triage arena (~60 KB model + activations). ESP32-C3 has none and will OOM. ESP32-S3 also has no usable GPIO 21–25, which is why the I²C bus is on 8/9. N8R8 (WROOM-1) is the mainstream module — do **not** buy N16R8V: that is WROOM-2 with 1.8 V octal flash/PSRAM, a different part for the same price. `partitions.csv` uses a 6 MB layout, which fits the 8 MB chip as-is. |
| 2 | AD8232 ECG breakout | 1 | Generic clone, snap-electrode header | 3–7 | The de-facto DIY single-lead front-end. Fine for HR/HRV; not diagnostic. |
| 3 | MAX30102 PPG breakout | 1 | Red + IR photodiode, I²C | 2–6 | Cheapest credible PPG. Shield from ambient light. |
| 4 | ICM-20948 breakout | **2** | 9-axis, I²C | 10–15 ea | ⚠️ **EOL**: TDK PCN-000570 (Mar 2026) — last-time-buy Apr 2026, last-time-ship **30 Oct 2026**. Distributor and clone stock remains fine for a one-off bringup rig, so buy 2 (one spare). A **firmware migration to ICM-42688-P (+ AK09918 magnetometer) or ICM-45608+ICT-15318 is a Phase-2 gated task** — no pin-compatible replacement and no driver exists in-tree yet. |
| 5 | Ag/AgCl snap electrodes | 50-pack | Gel, single-use | 8–15 | Skin prep is the single biggest determinant of ECG SNR. |
| 6 | Breadboard 830-pt + Dupont jumpers (M/M, M/F, F/F) | **2** | — | 15–25 ea | One for the Tier-0 rig, one for the two-pod sync bench (see the addendum below). |
| 7 | Micro-USB **data** cable | **3** | Must carry data, not charge-only | 3–5 | Charge-only cables are the #1 cause of "flash failed"; the third is for the second pod. |
| 8 | LiPo 3.7 V 500 mAh, JST-PH 2-pin | **2** | With protection PCB | 5–8 | One for the untethered power measurement, one for the second pod's battery soak. |
| 9 | **INA219 current/power monitor breakout** | 1 | I²C, 0.1 Ω shunt, address 0x40 | 2–5 | Closes the energy budget: per-session measured current, streamed on the research service's POWER stream (`firmware/esp32_band/sensors/ina219.c`). A bench PSU reading is a one-off; the 24-hour feasibility claim needs the *actual* consumption logged with the recording. |
| 10 | **ESP32-S3 DevKitC-1 (second pod)** | 1 | N8R8, same as row 1 | 12–15 | The G1→G2 sync criterion is **pod↔pod** (head pod vs forearm hub) — impossible with one board. A second devkit running the same `band-core` firmware, GPIO 27 wired between them, exercises the real scenario and produces the first two-pod XDF *before* the €350 Cerelog is ordered. |

**Subtotal: ~€80–125.**

### Two additions to the kit, and the evidence for them

**A second ESP32 (row 10).** G2's criterion #1 is *≤10 ms residual drift over
30 minutes*, and the figure that matters for the Cerelog is pod↔pod alignment,
not pod↔host. Buying the €12 board now and rehearsing the two-pod sync bench
is the readiness-gate logic one level down: the problem costs €12 today
instead of €350 plus a weekend later.

**The INA219 (row 9).** Architecture.md §55-62 makes the energy budget a
functional requirement. Without the monitor the pod reports a tier *model*;
with it, the recording carries the measurement, and the SOP's power rows
become data instead of a single spot check. At 1 Hz it costs ~28 B/s against
its declared 64 B/s budget.

Budget: the two rows above add ~€20 to the original €100 contingency:
**budget €125**, ordered in one shipment (CN shipping is ~€5–7 per item, 2–4
weeks lead time).

### Not needed yet (deliberately excluded)

TMP117, MAX-M10S GPS, soldering gear, oscilloscope, a Raspberry Pi. The
instrumentation you already own (bench PSU, multimeter, scope) covers every
measurement in the SOP below. (A *second* ESP32 **is** included — see row 10 —
because the sync criterion that gates the Cerelog order needs two pods.)

---

## G2 — Phase 2 rig (order only after G1 passes)

### The EEG decision

This is the most expensive and least reversible purchase. The matrix below is
evaluated on criteria that **G1 will have measured**, not on vendor claims.

| Option | EUR (landed) | Channels | Fits our architecture | Why consider it |
|---|---|---|---|---|
| **Cerelog ESP-EEG** | ~350 | 8 (ADS1299) | **Yes — it is an ESP32 pod**, same BLE research-service contract, BrainFlow/LSL native | Same price as PiEEG with no Raspberry Pi requirement. Its ESP32 core means the research service and the sync protocol are already ours; `hardware/cerelog.py` exists in-tree. |
| PiEEG 8-ch | ~325 + Pi 4/5 (~65) | 8 (ADS1299) | Partially — needs a Pi hub, and the firmware runs on the Pi, not on a pod | Good if you want the Pi as the long-term compute hub anyway. |
| OpenBCI Ganglion | ~625 + duty | 4 | Weakly — 4 channels only, US import tax can reach ~25% | Best validation pedigree, worst channel count per euro. |
| Muse S | ~295–495 | Consumer | No — different SDK, not raw BLE | Only for continuous sleep Tier-0 EEG, and its SDK needs a commercial licence if you monetise. |

**Recommendation: Cerelog ESP-EEG.** It is the only option that is both an
ADS1299 8-channel AFE *and* an ESP32, which means the exact firmware, BLE
contract and clock-sync path built for the Band can be reused for the head
pod instead of rebuilt around a Pi.

**Decision criteria to re-check at G1** (all measured, all must pass):
1. Does the smoke kit hold ≤10 ms residual drift over 30 min, **pod↔host and
   pod↔pod** (two pods, GPIO 27 wired)? If not, the EEG rig's 1 ms Tier-1
   budget is unreachable and the sync design must be fixed *before* spending
   €350 on a second pod.
2. Does raw PPG at 50 Hz fit the per-stream BLE budget with ECG at 500 Hz
   included (aggregate ~2.4 kB/s, per `hardware.yaml` `raw_frame`)? If not,
   either the rates or the framing change now — not after the Cerelog arrives.
3. Does the firmware hold zero dropped samples for 30 min continuous?
4. Does the measured Tier-0 current (INA219, POWER stream) agree with the
   ≤5 mW `hardware.yaml` T0 profile? If the rig draws materially more, the
   24-hour target is a budget problem, not a software one, and it must be
   known before committing to the EEG build.

### Explicitly deferred past G2

| Item | Est. EUR | Blocked on |
|---|---|---|
| EmotiBit (validated PPG/EDA/IMU/temp reference) | ~470 | G2. Its HRV and EDA agreement is contested in the independent Frontiers 2025 study, so treat it as infrastructure, not ground truth. |
| In-ear EEG satellite | ~200+ | G2 + the earbud paper's 600 h power claim reproduced on our own pod. |
| Compute hub (Pi 5) | ~65 | G2, unless you pick PiEEG above. |
| fNIRS (DIY-fNIRS ~$215 / OpenNIRScap ~$419) | 200–400 | Unbounded. Highest-effort, lowest-maturity modality; a research spike, not a deliverable. |

---

## Measurement SOP for G1

This is what turns "it works" into a gate. Instrumentation already on hand.

| # | Measurement | Instrument | Pass criterion |
|---|---|---|---|
| 1 | Idle current, deep sleep between BLE events | Bench PSU + series R / USB power meter | As low as achievable; record the number, this is the number that decides 24 h feasibility. |
| 2 | Average Tier-0 current, streaming raw | Same | ≤5 mW average (the `hardware.yaml` T0 budget). |
| 3 | BLE throughput, the raw research service | Host + `decoder.estimated_bytes()` | Per-stream budgets: ECG ≤1,1 kB/s, PPG ≤0,45 kB/s, each IMU sub-stream ≤0,32 kB/s; aggregate ≤3 kB/s sustained. The old "≤600 B/s" figure only ever applied to the PPG stream alone (raw volume of the full rig is ~2,3–2,4 kB/s — see `config/hardware.yaml` `raw_frame.*`). |
| 4 | **Dropped frames over 30 min** | `ResearchStreamDecoder.drops` | **Zero.** Any gap fails the recording — see `research_client.py`. |
| 5 | HR vs manual pulse count | Stopwatch + timer | MAE ≤ 2 bpm. |
| 6 | RMSSD vs NeuroKit2 reference on the same recording | NeuroKit2 | MAE < 5 ms. |
| 7 | PPG SQI / MAP, seated and still | `signal_quality.ppg` | SQI ≥ 0.3 sustained >30 s, MAP ≤ 0.5. |
| 8 | Clock residual drift vs host (pod↔host) **and pod↔pod** | `PodClockModel.status()` via the SYNC characteristic; ACC cross-correlation for pod↔pod | ≤10 ms (T0), ≤1 ms (T1). The pod↔pod number is the one that gates the Cerelog. |
| 9 | XDF integrity | `utils.validate_xdf` | Zero dropped samples, monotonic timestamps. |
| 10 | On-device triage latency | Serial log | ≤5 ms per inference. |
| 11 | Measured Tier-0 power over the soak | POWER stream / `gate_report.json` (`clock_residual_drift_ms`, `ble_throughput_bytes_per_s`, `dropped_frames` are filled automatically) | Average ≤5 mW per the `hardware.yaml` T0 profile; the per-session figure is the input to the 24-hour claim. |

The validator writes rows 3, 4, 8, 9, 11 itself into
`data/live_validation/gate_report.json`; rows 1, 2, 5, 6, 7, 10 are bench
measurements recorded by hand into the same file (`"source": "bench"` marks
the unfilled ones, so a half-filled report is visibly a half-filled report).

Run it as:

```bash
uv run python tools/check_toolchain.py
uv run python tools/check_contract.py
uv run python scripts/validate_phase1_entry.py --live-hardware \
    --config config/hardware_bringup.yaml
```

For the overnight MVP soak (6 h, battery, zero drops) add `--soak`, which
records continuously through LabRecorderCLI:

```bash
uv run python scripts/validate_phase1_entry.py --live-hardware --soak \
    --duration 21600 --config config/hardware_bringup.yaml
```

## Order of operations

1. **Today:** work the G0 blocker. Zero cost, and it is the only thing that
   can invalidate the entire kit order. (**G0 is CLOSED — 2026-10-12.**)
2. **Then:** order the smoke kit (rows above, one shipment).
3. **On arrival:** follow `docs/hardware/BRINGUP_SOP.md`, then run the SOP
   table above and record every number in `data/live_validation/`
   (`gate_report.json` is written by the validator; the bench rows are typed
   into it by hand).
4. **When G1 passes:** order the Cerelog ESP-EEG with the criteria above
   re-checked — including the pod↔pod sync number and the measured power.
5. **When G2 passes:** the readme/roadmap status flips and the next gate's
   hardware becomes orderable.

## Caveats

- Prices are indicative EU street prices and move; generic-module pricing is
  volatile. Treat all figures as ranges.
- **Not a medical device.** None of this hardware has regulatory clearance, and
  nothing recorded with it may be used for diagnosis or clinical decisions.
- Shipping from CN adds ~€5–7 per item and roughly 2–4 weeks of lead time;
  order the full smoke kit in one shipment rather than piecemeal.
