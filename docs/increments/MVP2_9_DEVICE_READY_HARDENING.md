# MVP-2.9 — First-device readiness hardening

**Status:** implementation complete on this tree; hardware verification (G1) still open.
No board, sensor, radio or person was used in its implementation.

This increment removes the ways in which a correctly wired band could still be
refused by the host, and the ways in which an incomplete run could look green.
It also aligns the documentation with the code that is actually in the tree.

## What changed

**The flashed-config gate now compares the same quantity on both sides.**
`config/hardware.yaml:113-114` declares the physical IMU ODR (102 Hz accel,
100 Hz gyro), but the BLE readback echoed the 50 Hz scheduler poll under the
`accel_hz`/`gyro_hz` keys. The divider helper now lives in a shared header
(`firmware/common/sensors/imu_odr.h`), the ICM-20948 driver and the readback
both use it, and `imu_hz` still reports the poll rate the LSL outlets declare.
A correctly flashed band no longer fails T04 on a unit mismatch.

**The image the manifest names can actually be configured.**
Both `config/hardware.yaml` and `config/hardware_bringup.yaml` now select
`band-bench`, the only variant with `CONFIG_SYNAPSE_FACTORY_MODE=y`; a test
pins that agreement. `docs/hardware/SENSOR_CONFIG.md` was regenerated.
The tester playbook now shows the required USB calibration write (with full
provenance and `--reboot`) *before* the BLE profile write, because `verify`
cannot create the host record it is asked to compare against.

**Per-unit writes fail closed at both ends.**
The host refuses a half LED pair and a LED code of 0 (which the firmware reads
as "keep the compiled default", so the readback could never match). BLE
failures are reported as CLI errors instead of tracebacks. The USB factory
shell and `factory_calibrate.py` now apply the same gain, LED, bias and cell
capacity bounds as the BLE whitelist. `factory_calibrate.py --reboot` makes a
written profile apply before recording.

**Evidence is fail-closed.**
T12 requires a **measured** `elapsed_s` of at least 21,600 s, not the
self-reported `duration_s` or just a LabRecorder source. A dry run marks
its artifact `rehearsal: true` instead of leaving a green "live" gate report in
`data/live_validation/gate_report.json`. A run that crashes before building a
report now appends a FAIL entry to `history.jsonl`. The detailed report is
rewritten when hand-filled rows force the gate report to FAIL. An all-skipped
Phase-0 gate is `UNVERIFIED`, never PASS. `test_ina219_host.py` now states the
real reason for a policy skip and is part of the `-rs` host C gate. The empty
Cerelog impedance test now asserts the fail-closed comparison it claims to
test. T03 accepts the IMU at 0x69 (AD0 high) as well as 0x68.

**Review follow-ups (closed here).**
The manual `band-bench` build command now includes `sdkconfig.defaults.core`
(bench = core + bench + base), and the stale `[band-core]` boot log and
"normally band-core" defaults were updated. `g1_measure.py noise` assumes the
nominal gain exactly once and always emits `true_noise_uv`, with a warning that
matches what it does. The shared ODR helper now honours the 8-bit gyro divider
clamp, so a slow-poll readback cannot claim a rate the sensor never produced.

**Data-chain corrections.**
The validator writes channel names and units into its XDF streams (ECG in µV,
PPG in arbitrary units, ACC in g, power in mV/mA/mW/%), so a recovered file is
self-describing. `g1_measure.py` reports `true_noise_uv` (the SOP §4a.1 electrode-referred figure)
and warns when `--serial` has no calibration record. `PROCUREMENT.md` no longer
tells the operator to type hand measurements into a file that is rewritten on
every run, and its live commands carry `--manifest`. The soak procedure uses
`bench_suite.py soak`, the only command that writes `evidence/T12_*.json`.
`data/bench/` and `capture/` are gitignored. Stale claims about build variants,
gate names, firmware job names and the latest increment were corrected in
README, Roadmap, AGENTS, STATUS, DECISIONS, READINESS_GATE, ESP-IDF_SETUP,
PROCUREMENT and BRINGUP_SOP; the status-pointer test now compares version
claims instead of only checking that a link exists.

## Verification

Commands run on this host (2026-11-28) against the final tree, using the same
sequence as `.woodpecker/ci.yml` (on this Windows host it is run in pieces, as
recorded in `AGENTS.md`):

- `ruff check .` and `ruff format --check .`: clean after formatting.
- `python -m mypy` over `synapse24` (64 files), the SDK (9) and `worker`/`backend`
  (5): clean.
- `check_contract` (167 checks), `generate_contracts --check`,
  `check_interfaces`, `check_baseline_claim` and the phase1 dry-run: green.
- Full suite with coverage over `src`, `worker` and `backend`: **1578 passed,
  27 skipped, 83.76 %** (gate ≥ 80 %). SDK suite: **70 passed, 89.41 %**.
  Format conformance (`fitdecode`/`pyxdf`): 17 passed. `gcc_probe.py`:
  "all the host-gate sources compile".
- C host gate on Windows: policy-blocked DLL loads can skip (a skip is **not**
  a pass). Replay of all 13 host modules (12 + the compiled
  per-unit parser test) in `ghcr.io/astral-sh/uv:python3.12-bookworm` with
  `uv sync --dev --frozen`: **202 passed, zero skips**, including the new
  effective-ODR readback pin.
- Firmware builds: the exact `firmware-band` command sequence was replayed in
  `espressif/idf:v5.5.5` against a copy of the tree: band-full, band-core,
  band-bench, band-full+RR and band-product all built and their `sdkconfig`
  assertions passed (**BAND_*_OK** for all five). The local build also found
  a pre-existing `%u`/`uint32_t` mismatch in `build_config_readback()`, now
  fixed. After the review fixes (the 8-bit gyro divider clamp in the shared
  helper), `band-bench` was rebuilt from the final tree and passed again with
  the same `sdkconfig` assertions. The server check is not configured in this
  environment, so the local replay is the evidence here, not the Actions run.

Hardware evidence remains open: no board, sensor, radio, long BLE read or human
session has been exercised. Status stays in `docs/hardware/STATUS.md`.
