# MVP-2.1 — Hardware readiness of the forearm hub (H1)

**Status:** implementation complete; hardware verification (G1) still open.

This increment turns the forearm-hub rig from a bench idea into a device whose
recordings can be attributed to one unit and whose numbers can be checked.
It does **not** claim that any sensor, radio or board has been exercised.

## What changed

- **One contract, actually enforced.** `config/hardware.yaml` now declares an
  explicit `adc_channel`/`adc_gpio` pair, PPG FIFO averaging and effective
  rate, IMU poll vs sensor ODR, the optional band-full parts, and a `status`
  per pod. `head_pod` and `in_ear_satellite` are `planned` with a `blocked_on`
  reason. `tools/check_contract.py` compares every field of every active pod
  with the firmware Kconfig (152 checks) and fails on drift. The build metadata
  and `docs/hardware/SENSOR_CONFIG.md` are generated from the same manifest.
- **Device identity.** A stable `SYN-` + 12-hex serial derived from the full
  MAC is stored in NVS and exposed on DIS `2A25`; discovery binds to a serial
  and refuses an ambiguous scan; LSL `source_id` carries the serial.
- **Per-unit calibration.** A versioned, CRC-checked NVS record holds ECG gain,
  PPG LED currents and IMU biases. The USB-serial factory shell and
  `scripts/factory_calibrate.py` write it; Kconfig is only the default for an
  uncalibrated band. The record is manufacturing data under
  `data/calibration/` and is not committed.
- **Power truth.** `power_budget` separates the system target (5 mW, G3) from
  the conservative kit envelope (425 mW). `band-core` selects the kit profile;
  POWER frames carry `power_is_model` and the G1 gate fails a model-based power
  row. The raw-frame flag is mirrored in the SDK codec and the generator.
- **Temp guard parity.** `synapse_sensors_get_temp()` now checks `s_temp_ok`
  like `get_gps()`.

## What is deliberately not in this increment

- No head-pod or in-ear firmware/contract: both need hardware decisions first.
- No change to the nominal ECG/PPG/IMU settings: measure, then decide.
- No phone app, cloud path or on-device storage: G1 records on the PC via
  LSL/XDF.
- No security/OTA in the firmware image: chosen for the product trade study.

## Verification

- `tools/check_contract.py --verbose`: green.
- `tools/generate_contracts.py --check`: green.
- `scripts/validate_phase1_entry.py --dry-run`: green.
- Python, SDK, format-conformance, firmware host C and ESP-IDF builds: see the
  Woodpecker gate on the final tree.

## Open for the bench

Measure ECG gain and noise, PPG perfusion index across LED currents, IMU
clipping in T2, lead-off with both electrodes, and the INA219 reading against a
known load. Until those rows in `gate_report.json` have measured numbers, HR,
RMSSD and the 24 h claim remain unverified.
