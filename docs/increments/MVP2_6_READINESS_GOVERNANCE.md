# MVP-2.6 — Readiness governance

**Status:** implementation complete; hardware verification (G1) still open.

This increment turns "ready for hardware" into an auditable, repeatable bench
process. No board, sensor, radio or person has been used in its implementation.

## What changed

- **Serial binding.** `synapse_factory_cli_start()` now copies the MAC-derived
  identity into the calibration record, so `--dump` works on a virgin band.
  `synapse_calibration_load()` rejects a record whose serial differs from the
  device identity; `cal serial` and `factory_calibrate.py --serial` are gone.
- **Full flashed-config truth.** The device-config readback reports every
  active `hardware.yaml` field (IMU ranges and ODRs, PPG codes, I²C, lead-off,
  sync GPIO, INA219 shunt, power profile, calibration identity), and the G1 gate
  compares them 1:1. A missing field is FAIL on a live run, after three reads.
- **Calibration v2.** Versioned record with instrument, firmware, uncertainty
  and noise; new `calibrate_imu.py` and `ppg_sweep.py`; `g1_measure.py` reads
  the nominal gain from the contract and uses the band's record when given
  `--serial`.
- **Bench suite and matrix.** `bench_suite.py` writes evidence for every
  attempt, including failures; `docs/testing/G1_TEST_MATRIX.md` gives each test
  an ID, a command and a pass criterion.
- **Session governance.** Live recordings require `--manifest` with a `S###`
  pseudonym and consent. Gate reports are append-only (`history.jsonl` and
  `gate_report_<run_id>.json`) and carry `verified_by`.
- **Config writes and product build.** Contract 2.4 writes are a strict
  whitelist (LEDs, gain in factory mode, feature rate) with atomic host
  commit; `band-product` never registers the raw research service.
- **One status file.** `docs/hardware/STATUS.md` is authoritative; README,
  Roadmap, AGENTS, CONTRIBUTING and compliance point to it. The tester manual
  lives in `docs/onboarding/TESTER_PLAYBOOK.md`.
- **Firmware target pinned.** `idf.py fullclean` used to drop the target, so a
  later variant silently rebuilt for the generic ESP32 (no octal PSRAM, no
  curve-fitting ADC calibration) and failed to compile. `CONFIG_IDF_TARGET`
  is now in `sdkconfig.defaults`, so every variant stays on the S3.

## Verification

Commands run on this host (2026-11-26), all against the final tree:

- `ruff check .` and `ruff format --check .`: clean.
- `mypy` for `synapse24`, the SDK, `worker` and `backend`: clean.
- `check_contract` (167 checks), `generate_contracts --check`,
  `check_interfaces`: green.
- `phase1 --dry-run`, format conformance, `gcc_probe.py`: green.
- Full suite with coverage over `src`, `worker`, `backend`: **83.72 %**,
  1327 passed / 176 skipped (the skips are the TensorFlow extra, missing local
  datasets and the Windows DLL policy).
- SDK suite: **89.41 %**, 70 passed.
- Host C gate: **177 passed, zero skips** in the Linux replay container
  (`python:3.12` + `uv sync --dev --frozen`), including the new calibration
  host test. On Windows the same sources compile but Application Control
  intermittently blocks loading the built DLL; the Linux replay is the gate
  evidence.
- Firmware builds in the `espressif/idf:v5.5.5` container: all four profiles
  compiled and produced `synapse-band.bin` — **band-full**, **band-core**,
  **band-full+RR** and **band-product** (57 % free, `CONFIG_SYNAPSE_PRODUCT_MODE=y`,
  factory mode off). All four report `CONFIG_IDF_TARGET="esp32s3"`.

Hardware evidence is still open: no board, sensor, radio, long BLE read or
human session has been exercised. Status remains `docs/hardware/STATUS.md`.
