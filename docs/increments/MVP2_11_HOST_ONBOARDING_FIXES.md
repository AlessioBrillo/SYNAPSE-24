# MVP-2.11 — Host onboarding fixes for the first device

**Status:** implementation complete on this branch; hardware verification (G1)
still open. No board, sensor, radio or person was used. This increment is an
immutable archive; `docs/hardware/STATUS.md` is the single source of truth.

MVP-2.10 made the live path fail-closed but left the tester without a legitimate
way to satisfy two required gates, and left an anchor that the export could not
read. This increment closes those workflow gaps without relaxing any gate.

## What changed

**Per-session references now have an untracked path.** `hr_accuracy` and
`rmssd_accuracy` used to read `validation.manual_hr_bpm` and
`validation.ground_truth_ecg_path` from the *tracked*
`config/hardware_bringup.yaml`, which the tester is forbidden to edit;
`--bench-values` accepted only the two current rows. The new
`--session-overrides <yaml>` reads `data/sessions/<date>/session_overrides.yaml`
with a strict allowlist (`manual_hr_bpm`, `ground_truth_ecg_path`,
`verified_by`). A file that tries to change a rate, a pin, a gate list or any
other key is refused, not merged. Relative reference paths resolve against the
overlay file, and `manual_hr_bpm` requires `verified_by`. The verifier is copied
into the gate report and the append-only history.

**A first calibration write cannot create an uneditable record.** The firmware
lets a BLE client edit only a record with complete provenance; the tester
playbook however showed a `--led`/`--operator`-only USB write, which produced a
record the radio path would always refuse. `factory_calibrate.py` now replays
pending writes on a fake copy of the band's record and refuses a *result* that
lacks serial, date, operator, instrument, firmware version or gain uncertainty.
An already-provenanced band can still be edited with a single value.

**The UTC anchor is canonical and actually read.** The validator wrote `lsl0_s`,
while the FIT export looked for `lsl_t0_s`, so a gate report's anchor was
silently ignored and the FIT was dated from the first sample. New reports write
`lsl_t0_s`; old reports with `lsl0_s` stay exportable through a reader alias.
The anchor itself is now bracketed between two LSL readings around the Unix
reading, and — the latent defect — the validator reads it from the
`LSLStreamManager` that captured it instead of from its own (always-empty)
attributes. Without that fix `utc_anchor_recorded` would fail on every live run.

**Operational truth.** The SOP, tester playbook, test matrix and bringup config
document the overlay and the full-provenance command. The host-C module count is
13 everywhere; the second XDF writer uses the canonical ASCII `uV` unit; the
firmware README no longer claims ESP-IDF 5.2+ or a 26-byte feature frame.
PDR-07 (Cerelog impedance fail-open) and PDR-08 (no real in-ear source) record
the two deferred G2 gaps explicitly instead of leaving them implicit.

## Verification

Gate sequence replayed on the final tree (2026-11-28): ruff check/format clean
(333 files); mypy clean over `synapse24`, SDK, `worker` and `backend`;
`check_contract` 188 checks OK; contract codegen and interfaces OK; baseline
claim consistent; phase-1 dry-run, format conformance, onboarding coverage and
SDK coverage (89.41 %) green. Full suite on the final tree: **1625 passed,
71 skipped, 84.45 % coverage** (>= 80 %). The skips are the host-C tests
refused by Windows Application Control (the count varies between runs); they
are not a pass and were replayed separately.

No physical hardware was used. The Windows host-C step is RED by design (skip
reasons printed by `scripts/host_c_gate.py`); the Linux container replay is
**204 passed, zero skips** across the 13 host-C modules plus **2 passed** for
the compiled parser step. The `firmware-band` step was replayed in
`espressif/idf:v5.5.5`: **BAND_ALL_OK** for all six configurations. The server
`ci/woodpecker/push/ci` remains the authoritative gate evidence.
