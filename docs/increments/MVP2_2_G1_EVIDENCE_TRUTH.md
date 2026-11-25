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

The host C gate printed skip reasons on this Windows host (6–24 depending on
the run): Application Control intermittently refused to load freshly built
DLLs from the temp directory (the compiles succeeded). A skip is not a pass;
those parity assertions must run in the Linux gate container before the tree
is called green there. The ESP-IDF
`firmware-band` step could not run in this environment (no ESP-IDF toolchain),
so the new C sources are proven only by static guards and the host compiles.

## Still open

- G1 hardware run: serial bind, real ECG/PPG/IMU, INA219 measurement, 6 h soak.
- The firmware readback has not been exercised against a real NimBLE stack.
- Contract 2.4 BLE configuration writing remains deferred; this increment adds
  read-only verification only.
