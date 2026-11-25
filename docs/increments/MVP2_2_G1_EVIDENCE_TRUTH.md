# MVP-2.2 — G1 evidence truth

This increment makes the first real G1 run auditable. No hardware, board or
radio was run in its implementation: everything below is proven by the local
gate with synthetic hosts. The G1 hardware verdict is still open.

## What changed

- **Gate states.** `ValidationGateResult` now carries `PASS`, `FAIL` or `N/A`
  and a `required` flag from `validation.required_gates` in
  `config/hardware_bringup.yaml`. `overall_pass` is computed over required
  gates only. An absent Tier 1 pod, a disabled Cerelog, a missing POWER stream
  and empty rate checks are N/A on a synthetic run and FAIL on a G1 live run —
  never free passes.
- **The four cardiac rows the SOP promised are now gates.** HR MAE ≤ 2 bpm
  against `validation.manual_hr_bpm`, RMSSD MAE < 5 ms against
  `validation.ground_truth_ecg_path` (`.npz`, key `r_peaks`), PPG SQI ≥ 0.3 and
  MAP ≤ 0.5 sustained > 30 s. Missing references fail a live run.
- **Flashed-configuration proof.** The firmware publishes its live rates,
  gain, LED currents and battery capacity as JSON on the existing device-config
  characteristic; the validator reads it before the recording and fails
  `flashed_config_matches_contract` on any mismatch. This closes the stale
  `sdkconfig` gain trap without a new GATT characteristic.
- **One soak artifact.** `validate_xdf` now runs on the LabRecorder file that
  is fingerprinted and referenced by the report. The spool XDF is no longer a
  second writer of the gated artifact.
- **Identity at creation time.** DIS serial is read before LSL outlets are
  created, and every `source_id` is a `{serial}` template. The configured serial
  is an expected-value check, not the source of identity; synthetic runs are
  tagged `synthetic`.
- **One flashing path.** The validator's broken `--flash` (`esptool
  write_flash 0x0 app.bin`, no bootloader or partition table) was removed; use
  `scripts/build_and_flash_firmware.py`. `--live-hardware` now selects the G1
  profile and the fail-closed preconditions.
- **Single-source hardware values.** Wired sync GPIO and the INA219 front-end
  are generated from `config/hardware.yaml` into the C header and consumed by
  the drivers; `tools/check_contract.py` asserts no stale copies. G1 battery
  capacity is 1000 mAh in the bringup config and in `sdkconfig.defaults.core`;
  the dead `power_monitor` block is gone.
- **Documentation.** `BRINGUP_SOP.md` now describes NVS-only gain calibration,
  the contract-owned INA219 values and the real gate list; `POWER_BUDGET.md`
  states the actual kit cell composition and runtime arithmetic.

## Verification

Local Woodpecker equivalent (`.woodpecker/ci.yml`): ruff check/format, mypy
(package, SDK, worker/backend), `check_contract` (166 checks),
`generate_contracts --check`, `check_interfaces`, `check_baseline_claim`,
phase-1 dry-run, format conformance, full suite with 83.72 % coverage, SDK
suite with 89.41 % coverage, host C gate, `gcc_probe.py`.

The command sequence was then replayed in a Linux container from the PR head:
every step green, including the host C gate (174 tests, **zero skips**) and
full-suite coverage 83.79 % / SDK 89.32 %. `gcc_probe.py` (a Windows-host-only
extra that shells out to Docker) passes on the host, not inside that container.
The ESP-IDF `firmware-band` sequence was also run in the `espressif/idf:v5.5.5`
container: band-full, band-core and band-full with RR all built and produced
`synapse-band.bin`. The Windows host itself cannot run `ci-local` (no usable
bash through WSL) and its Application Control intermittently blocks loading
freshly built DLLs; the Linux replay is the green evidence.

A review found three fail-open paths, all fixed before merge: the recorded-XDF
gate now compares recovered samples with the spool's pushed counts (an empty or
gapped LabRecorder file fails), the sustained PPG gates return N/A when no
window qualifies, and RMSSD MAE returns None for fewer than two R-peaks.
Gate-report JSON now carries `status`/`required`, per-run evidence is reset,
the rehearsal command no longer claims live hardware, and the Python
`SyncConfig` default takes the generated GPIO.

## Still open

- G1 hardware run: serial bind, real ECG/PPG/IMU, INA219 measurement, 6 h soak.
- The firmware readback has not been exercised against a real NimBLE stack.
- Contract 2.4 BLE configuration writing remains deferred; this increment adds
  read-only verification only.
