# SYNAPSE-24 status — the single source of truth

This file is the authoritative status of the project. README, Roadmap,
AGENTS.md, CONTRIBUTING.md and the compliance documents point here instead of
keeping their own copy. If a number below changes, change it here first.

**Latest increment:** MVP-2.9 (first-device readiness hardening). **Last gate:** see
`docs/increments/MVP2_9_DEVICE_READY_HARDENING.md` for the exact command
sequence and result. The previous increment (MVP-2.8, hardware readiness fixes)
is recorded in `docs/increments/MVP2_8_HARDWARE_READINESS_FIXES.md`; MVP-2.7
(first device onboarding) in `docs/increments/MVP2_7_FIRST_DEVICE_ONBOARDING.md`.
MVP-2.6 (readiness governance) is recorded in
`docs/increments/MVP2_6_READINESS_GOVERNANCE.md`.

## Gate status

| Gate | State | Evidence |
|---|---|---|
| G0 — toolchain | ✅ CLOSED | ESP-IDF v5.5.5; `tools/check_toolchain.py` READY |
| G1 — smoke kit | ⛔ OPEN on hardware | Software side closed; no board, sensor or radio has run |
| G2 — Cerelog EEG | ⛔ Not started | Blocked on G1 |
| G3 — system power target (5 mW) | ⛔ Not started | Kit envelope 425 mW model, not measured |
| Phase 0 baseline | ⚠️ PARTIAL | WESAD local data missing (issue #45), outside the CI gate |

## What is proven

- Contract enforcement: `tools/check_contract.py`, `generate_contracts --check`,
  `check_interfaces`; flashed config is now read back in full and compared
  1:1 with `config/hardware.yaml`. Bench evidence now derives its verdict from
  the measurement (a fresh gate report, a parsed boot log), not from an exit
  code; per-unit profiles are written and verified by one command.
- Firmware: band-full, band-core, **band-bench**, band-full+RR and
  band-product build in CI. The bench image is the core smoke-kit profile with
  the factory shell enabled; the product image does **not** register the raw
  research service.
- Host path: raw-frame parity, research service, XDF/FIT conformance, SDK and
  backend suites; C host gate with zero skips (Linux replay of all 12 modules).
- Per-unit calibration is bound to the DIS serial, versioned (v3) and
  records instrument, firmware, uncertainty and noise. The flashed-config gate
  now compares the *physical* IMU ODR read back by the band with the contract,
  and the manifest names `band-bench`, the only image with factory writes.
- Gate evidence is fail-closed: a short soak, a dry run, an all-skipped Phase-0
  gate and a crashed run can no longer produce a green G1 verdict.

## What is not proven

- No physical sensor, board, BLE link or phone has been exercised.
- Real NimBLE long-read of the full JSON readback (the host uses stubs).
- Human data: no session has been recorded with consent yet.
- Six-hour soak, INA219 known-load check, IMU bias at rest, PPG LED sweep.
- Security: config writes require an encrypted link, but pairing uses Just
  Works (`sm_mitm = 0`, PDR-02) and no bonded key store is initialised. PDR-02
  remains **explicitly accepted for the bench prototype** and open for the
  commercial image; the residual MITM risk is not accepted for a shipped
  product.

## Critical rules

1. No live recording without `--manifest` (pseudonym `S###` and consent).
2. `N/A` is not a pass on a live run; a missing readback field is `FAIL`.
3. Gate reports are append-only: `history.jsonl` plus `gate_report_<run_id>.json`.
4. Nothing in `data/`, `evidence/` or `data/calibration/` is committed.
5. Research platform, not a medical device. No diagnosis or clinical use.

## Documents

- `docs/hardware/PROCUREMENT.md` — what to buy and on which evidence.
- `docs/hardware/BRINGUP_SOP.md` — box-to-XDF procedure.
- `docs/hardware/FIRST_HARDWARE_DAY.md` — what to do before the box arrives.
- `docs/hardware/SENSOR_CONFIG.md` — generated sensor matrix.
- `docs/testing/G1_TEST_MATRIX.md` — reproducible test IDs and criteria.
- `docs/product/DECISIONS.md` — open product risks, deliberately not implemented.
- `docs/onboarding/TESTER_PLAYBOOK.md` — the tester's step-by-step manual.
- `compliance/COMPLIANCE.md`, `compliance/PRIVACY.md` — regulatory position.
