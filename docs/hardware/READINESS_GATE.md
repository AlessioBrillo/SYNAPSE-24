# Hardware Readiness Gate

The plan and its current state. This file is the answer to: *what has to be true
before I spend money on sensors?*

**Verdict as of this writing:** the software is ready to absorb hardware. The
world's only blocker is that the firmware has never been compiled — which costs
€0 to fix and, left unfixed, would invalidate the entire procurement order.

## Where the project was

Phase 0 (software + public data) was genuinely complete: 440+ tests green,
baseline gates closed on MIT-BIH and WESAD, LSL/XDF and clock-sync models
validated on synthetic and replayed data. The gap was everything on the other
side of the wire:

1. **The firmware had never been compiled.** ~50k lines of C/C++ across four
   parallel ESP32 projects, none through a compiler, and no CI job that could
   have caught it.
2. **The contract was not executable.** Sample rates, I²C pins, BLE UUIDs and
   stream ids disagreed between `hardware.yaml`, the firmware and the host
   validator. Six concrete contradictions, each of which becomes a bench
   session once hardware is on the desk.
3. **The canonical firmware had no raw path.** `esp32_band` exposed only a
   20-byte consumer feature stream (HR/RR/SQI/stress). For a project whose
   asset *is* the multimodal dataset, that is the wrong interface.
4. **Nothing about the live path was tested.** Zero host code exercised a real
   BLE notification, and no bench existed that could catch a clock or gap bug
   without hardware.

## What was done

### Sprint 0 — toolchain and build variants
- `tools/check_toolchain.py` — makes "can this machine build the firmware?" an
  executable question. Currently reports **BLOCKED**: ESP-IDF is not installed.
- `firmware/esp32_band/sdkconfig.defaults.core` + real gating of
  `SYNAPSE_FEATURE_GPS/TEMPERATURE`. Those Kconfig flags were previously
  declared and consulted nowhere, so GPS and TMP117 compiled unconditionally
  and the smoke-kit rig could not boot without a MAX-M10S on the desk.
- CI job `firmware-build` compiles both `band-core` and `band-full` on every
  push, so the build can never silently regress again.
- `scripts/build_and_flash_firmware.py` targets `esp32_band` and resolves both
  variants.

### Sprint 1 — one contract, enforced
- `config/hardware.yaml` is the single source of truth for rates, pins, UUIDs,
  stream ids and stream names. Closed all six contradictions; PPG is now 50 Hz
  everywhere (the rate `ppg_dsp.c` and the triage model are built for), I²C is
  8/9 and stated once, and there is one BLE protocol with distinct UUIDs for
  raw research data versus the consumer feature stream.
- `tools/check_contract.py` cross-checks the manifest against firmware
  registers, host LSL outlets and codec constants, and **exits non-zero on
  drift**. `tests/test_contract_checker.py` proves the checker can fail,
  including the little-endian UUID ordering NimBLE expects.
- Synapse Research Service: raw samples + on-device timestamps, zero derived
  features, dropped frames counted rather than ignored.

### Sprint 3 — host receive path, validated without hardware
- `src/synapse24/hardware/research_client.py`: decoder with 16-bit
  sequence-gap detection and a `DeviceClock` that rebuilds a monotonic 64-bit
  timeline from the pod's 32-bit microsecond stamp (which wraps every
  ~4295 s — without this, a 24 h Tier-0 recording would jump backwards).
- `tests/test_research_service_hil.py`: a synthetic pod that speaks the real
  frame format (ECG 500 Hz, PPG 50 Hz red+IR, IMU sub-streams at 50 Hz) with
  injected faults. **7 tests, and the protocol/clock layer is now covered with
  no hardware attached.**

## What is left

| Gate | Blocker | Cost to clear |
|---|---|---|
| **G0** | ESP-IDF not installed; firmware never compiled | €0, ~2 h |
| **G1** | Smoke kit not bought/measured | ~€100 + a weekend |
| **G2** | Cerelog order | ~€350, after G1 |

## Work explicitly deferred

SDK CLI, FIT writer, signed OTA, provisioning, GPS, TMP117, multi-pod wired
sync, in-ear EEG, compute hub, fNIRS, cloud. Each has its own gate; putting any
of them on the critical path now would delay the hardware that matters.

## How to drive it

```bash
uv run python tools/check_toolchain.py    # is the firmware buildable?
uv run python tools/check_contract.py     # does the contract still hold?
uv run pytest -q                          # is the software still sound?
```

Then `docs/hardware/BRINGUP_SOP.md` for the box-to-XDF procedure, and
`docs/hardware/PROCUREMENT.md` for what to buy and on what evidence.
