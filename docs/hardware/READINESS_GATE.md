# Hardware Readiness Gate

The plan and its current state. This file is the answer to: *what has to be true
before I spend money on sensors?*

**Verdict as of this writing:** the software is ready to absorb hardware, and
the firmware now compiles: G0 closed on the dev host on 2026-10-12 (ESP-IDF
v5.5.5, both build variants). The next physical step is the smoke kit (G1).

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
  injected faults.
- `src/synapse24/hardware/research_ble_client.py`: the BLE client, with the
  transport injected so connect, subscribe, decode, clock sync and gap
  detection are all exercised without a Bluetooth adapter.
- **C compile gates.** `tests/firmware_host` compiles the Band research service
  and the raw-frame codec against ESP-IDF/NimBLE stubs with
  `-Wall -Wextra -Werror`. These caught three real defects that would otherwise
  have surfaced on the bench, after the sensors were bought:
  1. `unit_scale` was `uint8_t` while the unit constants reach 1000, so every
     quantised ECG/IMU sample was silently scaled by 232/1000;
  2. `ble_research_service.h` used `bool` without including `<stdbool.h>`;
  3. the `esp_err` stub was missing error codes the firmware returns.

  Two process bugs were also fixed: the gates used to **skip** when no compiler
  was found (a skipped compile gate cannot fail), and a `.cmd` shim was
  mangling the compiler arguments. `ziglang` is now a dev dependency, so the
  gate runs on a machine with no C toolchain at all.

- The BLE client tests found two more real bugs: the NTP RTT sign was inverted,
  and the client double-counted rejected frames.

## What is left

| Gate | Blocker | Cost to clear | State (2026-10-15) |
|---|---|---|---|
| **G0** | ESP-IDF not installed; firmware never compiled | €0, ~2 h | ✅ **CLOSED on the dev host** — ESP-IDF v5.5.5 installed, `tools/check_toolchain.py` READY (with the environment activated; on this machine it reports BLOCKED until `export.bat`/`export.ps1` ran — see the note below), `band-core` and `band-full` both compile locally. CI pins v5.5.5. The first compile found three real defects (see the commit history of `feat/firmware-g0-local-build`); all fixed. |
| **G1** | Smoke kit not bought/measured | ~€140 + a weekend | ⛔ **Software side CLOSED (pre-hardware closure increment)** — the SOP is executable end to end, the research service now *transmits* (it did not: three independent preconditions were dead), the wire convention matches the host codec (it did not: multi-channel frames decoded at 1/channels), measured power rows exist and fill from the recording, the 10-hour wrap/reconnect path runs in CI in milliseconds, and the two energy budgets are separated. **Only the physical kit and the bench measurements remain** — see "Pre-hardware closure" below. |
| **G2** | Cerelog order | ~€350, after G1 | ⛔ Not started |

### Pre-hardware closure increment (this branch)

The remaining risk before spending money was not "does it compile" (G0) but
"does it *work*" on the real path. Four defects, all invisible to the
existing gates, are now fixed and each has a test that fails if it returns:

1. **The research service was silent.** `transmitting` was never set,
   `stream->enabled` was never set for any stream, and `conn_handle` was never
   updated from the GAP events — three independent preconditions, all
   permanently false on the real firmware. The pod registered seven
   characteristics and emitted nothing. Fixed (`ble_gatt_server.c` wiring +
   `init()` arms the service) and pinned by `tests/test_research_service_host.py`,
   which drives the service against NimBLE stubs and decodes the captured
   frames with the host codec.
2. **The ring depth was smaller than the flush threshold.** ECG's
   `flush_at` is 50 samples into a 32-deep ring: the ring overflowed before a
   frame could leave the pod, and ring loss is invisible to the host's
   sequence-gap check. Fixed (depth 64) with an overflow counter the bench
   can read.
3. **The wire format and the host decoder disagreed on `sample_count`.**
   The firmware counted physical samples; the codec counts values
   (channel-minor). PPG, IMU and POWER frames therefore decoded at 1/2, 1/3
   and 1/4 of the payload — ECG (single channel) was the only correct stream,
   which is exactly why the parity tests passed.
4. **The power gate was unmeasurable.** `estimated_bytes()` multiplied the
   frame count by the 244-byte MTU payload (2–6× the real volume, so the BLE
   budget row reported guaranteed false negatives), and `gate_report.json`
   never extracted the POWER stream (the "measured power" row stayed a bench
   TODO). Both fixed; `docs/hardware/POWER_BUDGET.md` separates the
   budget-kit (150–600 mW, what G1 measures) from the budget-system (5 mW
   T0 profile, what the 24 h claim targets and G3 must prove), so G1 can no
   longer fail against a target the rig was never built to meet.

One environment note: `check_toolchain.py` reports BLOCKED unless ESP-IDF's
environment is active in the shell (it looks at `IDF_PATH`/PATH, not at the
default `~/esp/esp-idf` install). G0 is closed — run the exporter first.

### MVP live-path closure (what was added after G0)

The remaining risk was that "the firmware compiles" and "the SOP can run" were
two different statements. The following closed that gap, and each item is
gated in CI rather than asserted in prose:

- **Rate drift removed** — the 64/100 Hz literals are gone from the host
  adapter, the pod coordinator, the validator's gates and the tests; every
  rate comes from `config/hardware.yaml`, and `check_contract.py` (now 83
  checks) fails if they return.
- **One clock domain** — the pod's raw stamps are mapped into
  `pylsl.local_clock()` through a `PodClockModel` estimated from the SYNC
  exchanges, and the Tier-0 sync gate judges *evidence* (fit residual **and**
  the RTT/2 uncertainty floor). An uncalibrated model fails instead of
  claiming synchronization it never measured.
- **The documented command exists** — `--live-hardware` and `--soak` are real
  flags, `gate_report.json` carries the ten measurement-SOP rows (bench rows
  explicitly unfilled), and `tests/test_doc_command_parity.py` parses every
  runbook command.
- **The SOP runs without hardware** — the assembled `run()` is driven against a
  synthetic pod in CI, which is where six never-executed defects surfaced
  (a crash on the first raw sample, a report keyword the dataclass did not
  declare, a shared device clock across streams, ingest/decoder unit
  disagreement, a reconnect read as 65k lost frames, and gates judging the
  config duration instead of the run).
- **Energy as data** — the INA219 streams measured current on the POWER stream
  (`sensors/ina219.c`, verified against a simulated I²C bus), so the 24-hour
  feasibility claim rests on per-session measurement rather than a tier model.

## How the gates are enforced

Every gate above is an executable check, not a document:

```bash
uv run python tools/check_toolchain.py   # is the firmware buildable?
uv run python tools/check_contract.py    # does the hardware contract still hold?
uv run pytest -q                         # is the software still sound?
```

`tests/firmware_host` adds a fourth, quieter one: it compiles the Band research
service, the raw-frame codec and the INA219 driver against ESP-IDF stubs with
`-Wall -Wextra -Werror`. That gate has already earned its keep - see Sprint 3
above for the three firmware defects it caught before any hardware existed.

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
