# SYNAPSE-24 status — the single source of truth

This file is the authoritative status of the project. README, Roadmap,
AGENTS.md, CONTRIBUTING.md and the compliance documents point here instead of
keeping their own copy. If a number below changes, change it here first.

**Latest increment:** MVP-2.6 (hardware-readiness governance), merged on the
final tree of the 2026-11-26 work. **Last gate:** see the increment document
`docs/increments/MVP2_6_READINESS_GOVERNANCE.md` for the exact command sequence
and result.

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
  1:1 with `config/hardware.yaml`.
- Firmware: band-full, band-core, band-full+RR and band-product build in CI.
  The product image does **not** register the raw research service.
- Host path: raw-frame parity, research service, XDF/FIT conformance, SDK and
  backend suites; C host gate with zero skips.
- Per-unit calibration is bound to the DIS serial, versioned (v2) and
  records instrument, firmware, uncertainty and noise.

## What is not proven

- No physical sensor, board, BLE link or phone has been exercised.
- Real NimBLE long-read of the full JSON readback (the host uses stubs).
- Human data: no session has been recorded with consent yet.
- Six-hour soak, INA219 known-load check, IMU bias at rest, PPG LED sweep.
- Security: config writes require an encrypted link, but pairing uses Just
  Works (`sm_mitm = 0`) and no bonded key store is initialised. Residual MITM
  risk is accepted for the prototype, not for a shipped product.

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
- `docs/onboarding/TESTER_PLAYBOOK.md` — the tester's step-by-step manual.
- `compliance/COMPLIANCE.md`, `compliance/PRIVACY.md` — regulatory position.
