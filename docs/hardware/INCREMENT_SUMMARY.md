# Hardware Readiness Gate — Increment Summary

**Branch:** `feat/hardware-readiness-gate` · **Commits:** 9 · **Tests:** 483 passing, 0 failing

## What this increment was for

Reaching an MVP state where the sensor purchase is justified by evidence rather
than by a roadmap estimate. The project's software foundation (Phase 0) was
genuinely complete; the gap was everything on the far side of the wire, and the
four things that would have made an order a gamble.

## The four gaps, and what closed them

### 1. The firmware had never been compiled
~50k lines of C/C++ across four parallel ESP32 projects, none through a
compiler, and no CI job that could have caught it.

- `tools/check_toolchain.py` makes "can this machine build it?" executable, and
  reports honestly that ESP-IDF is absent. That is the one remaining gate (G0),
  and it costs €0 to clear.
- CI gained a `firmware-build` job compiling both variants on every push.
- **`firmware-host-gate`**: `tests/firmware_host` compiles the Band research
  service and the raw-frame codec against ESP-IDF/NimBLE stubs with
  `-Wall -Wextra -Werror` — no toolchain required. It found three real defects
  (see below) that would otherwise have surfaced on the bench.

### 2. The contract was not executable
Six concrete contradictions between `hardware.yaml`, the firmware and the host
validator: PPG at 64/50/100 Hz, IMU at 100 vs 50 Hz, two different BLE
protocols, and an I²C bus documented one way and coded another.

- `config/hardware.yaml` is now the single source of truth, and
  `tools/check_contract.py` cross-checks it against the firmware registers, the
  host LSL outlets and the codec constants, exiting non-zero on drift.
  `tests/test_contract_checker.py` proves the checker can fail — including the
  little-endian UUID ordering NimBLE expects.

### 3. The canonical firmware had no raw path
`esp32_band` exposed only a 20-byte consumer feature stream. For a project
whose asset *is* the multimodal dataset, that is the wrong interface.

- **Synapse Research Service**: raw samples + on-device timestamps, zero derived
  features, dropped frames counted rather than ignored.
- Host side: frame decoder with 64-bit clock reconstruction (the pod's 32-bit µs
  stamp wraps every ~4295 s, so a 24 h recording would otherwise jump
  backwards), a BLE client with an injected transport, and the live validator
  wired to consume the real format.

### 4. Nothing about the live path was tested
- A synthetic pod speaks the real frame format with injected faults.
- The C gates, the BLE client and the validator's raw path are all exercised
  with no hardware attached.

## Bugs this increment found (before any hardware existed)

| # | Bug | Consequence if shipped |
|---|---|---|
| 1 | `unit_scale` was `uint8_t` while unit constants reach 1000 | every quantised ECG/IMU sample silently scaled by 232/1000 |
| 2 | missing `<stdbool.h>` in the research service header | compile failure on the first `idf.py build` |
| 3 | `esp_err` stub missing codes the firmware returns | misleading host-side diagnostics |
| 4 | off-by-one in `synapse_raw_frame_get_sample` length check | every frame rejected as too short |
| 5 | NTP RTT sign inverted in the BLE client | clock model fed a negative delay |
| 6 | client double-counted rejected frames | healthy session reported as lossy |
| 7 | `esp32_band`'s Kconfig feature flags were consulted nowhere | GPS/TMP117 compiled unconditionally; smoke kit could not boot |
| 8 | test asserted `0x10B` for a NOT_FINISHED return (actually `0x10C`) | a correct firmware looked broken |

Two process problems were also fixed: the C gates used to **skip** when no
compiler was found (a skipped compile gate cannot fail), and a `.cmd` shim was
mangling the compiler arguments. `ziglang` is now a dev dependency so the gate
runs on a machine with no C toolchain at all.

## Gate state

| Gate | Unlocks | State |
|---|---|---|
| G0 | Any firmware work | ⛔ ESP-IDF not installed on the build host |
| G1 | Phase 2 order (Cerelog) | ⛔ smoke kit + measurement SOP |
| G2 | In-ear, hub, fNIRS, EmotiBit | ⛔ after G1 |

**G0 is the only thing standing in front of the rest, and it costs €0.**

## Files that matter

```
tools/check_toolchain.py        is the firmware buildable?
tools/check_contract.py         does the hardware contract still hold?
firmware/common/ble/synapse_raw_frame.[ch]   the wire format (host-parity tested)
firmware/esp32_band/ble/ble_research_service.[ch]  raw sample streaming
src/synapse24/hardware/raw_codec.py          host mirror of the C codec
src/synapse24/hardware/research_client.py    decoder + gap detection + 64-bit clock
src/synapse24/hardware/research_ble_client.py BLE client
src/synapse24/hardware/raw_ingest.py         validator ingestion path
docs/hardware/READINESS_GATE.md   where the project was and what changed
docs/hardware/PROCUREMENT.md      what to buy, on what evidence
docs/hardware/BRINGUP_SOP.md      box-to-XDF procedure
```

## What was deliberately not done

SDK CLI, FIT writer, signed OTA, provisioning, GPS, TMP117, in-ear EEG, compute
hub, fNIRS, multi-pod wired sync — all out of scope until G1, each with its own
gate. Adding any of them now would have been the reliable way to never reach
the hardware.
