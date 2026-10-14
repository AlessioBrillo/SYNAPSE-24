# Hardware Bringup SOP — from box to first valid XDF

The step-by-step procedure for the smoke kit (G1). Every step has an explicit
check; do not proceed past a failure, because a failure here is invisible later
as corrupted data rather than as a broken build.

> **Before anything else:** `uv run python tools/check_toolchain.py` must pass.
> If ESP-IDF is missing, stop and read `ESP-IDF_SETUP.md`. Building firmware
> without a toolchain check is how you burn an afternoon on a driver bug that
> was never installed.

## 0. Wiring

ESP32-S3 DevKitC-1 ← sensor breakouts. **I²C on GPIO 8 (SDA) / 9 (SCL)** —
these values are asserted by `tools/check_contract.py` against both the
manifest and the firmware. Do not substitute 21/22: those pins are unusable on
octal-PSRAM S3 modules.

| Signal | GPIO |
|---|---|
| AD8232 OUTPUT | 36 (ADC1_CH0) |
| AD8232 LO+ / LO− | 39 / 34 |
| MAX30102 + ICM-20948 + INA219 SDA | **8** |
| MAX30102 + ICM-20948 + INA219 SCL | **9** |
| MAX30102 INT | 5 |
| ICM-20948 INT | 6 |
| All VCC | 3V3 |
| All GND | GND |

Add 4.7 kΩ pull-ups on SDA/SCL if your breakouts do not include them — a
missing pull-up presents as `ESP_ERR_NOT_FOUND` on the PPG PART_ID.

Electrodes: RA (red) proximal near the elbow, LA (yellow) distal near the
wrist, RL (green) on a bony prominence as reference. Clean the skin with
alcohol; a wet electrode is the difference between a clean QRS and noise.

## 1. Build and flash

```bash
uv run python tools/check_contract.py
uv run python scripts/build_and_flash_firmware.py --variant band-core \
    --port COM3 --clean monitor
```

Expected first-boot sequence:

```
I (xxx) SYNAPSE_BAND: === Synapse Band v1 Starting ===
I (xxx) SYNAPSE_SENSORS: Sensors initialized (ECG:500Hz, PPG:50Hz, IMU:50Hz) [band-core]
I (xxx) SYNAPSE_RESEARCH: research service registered (6 streams, raw samples only)
I (xxx) INA219: INA219 ready at 0x40 (bus 3.70 V, shunt 0.100 ohm)
I (xxx) SYNAPSE_POWER: INA219 power monitor active: measured current telemetry enabled
I (xxx) SYNAPSE_POWER: power telemetry sink attached
I (xxx) SYNAPSE_BAND: Entering main loop
```

`[band-core]` must appear. If GPS/Temp lines appear instead, the wrong variant
was built. The INA219 lines appear only when the monitor is stocked and
answering at 0x40; without it the rig still boots and reports the current
figure as a tier-model estimate rather than a measurement — the SOP's power
row then says so instead of inventing a number.

## 2. Confirm the pod is discoverable

Point `nRF Connect` (or the host client) at the research service
`A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C70` and confirm seven characteristics exist:
ECG, PPG, ACC, GYRO, MAG, POWER, SYNC. Enable notifications on the ones you want.

If the service is absent, the firmware is not the one you think it is: the
research service is registered only when `CONFIG_SYNAPSE_FEATURE_RESEARCH_SERVICE=y`,
which `sdkconfig.defaults.core` sets.

## 3. Capture and validate

```bash
export SYNAPSE_FOREARM_BLE="aa:bb:cc:dd:ee:ff"   # or leave as auto
uv run python scripts/validate_phase1_entry.py --live-hardware \
    --config config/hardware_bringup.yaml --duration 60
```

The validator must report, in this order:
1. streams resolved with the expected rates (500 / 50 / 50 / 50 / 50),
2. HR within tolerance of a manual pulse count,
3. RMSSD within tolerance of a NeuroKit2 reference on the same recording,
4. PPG SQI ≥ 0.3 and MAP ≤ 0.5 sustained,
5. **zero dropped frames**,
6. XDF round-trip valid and timestamps monotonic.

Step 5 is the one people skip. A recording with a hole is worse than a
recording that stopped, because the hole is silent. `ResearchStreamDecoder`
counts it; do not override it.

### 3a. Staged stream subscription (BLE budget triage)

The full rig streams ~2,3–2,4 kB/s sustained (ECG 1,0 + PPG 0,42 + IMU 0,95;
see `config/hardware.yaml` `raw_frame.*`). That fits the physical link with
MTU 247 + DLE, but if measurement 4 (dropped frames) is non-zero it means the
negotiated link is under-performing. Debug in stages by commenting
characteristics out of `config/hardware_bringup.yaml` `bringup.ble`:

1. First run: **ECG only** (~1,0 kB/s). Zero drops here → the link and the
   frame path are sound; the fault is capacity, not correctness.
2. Second run: ECG + PPG (~1,4 kB/s).
3. Third run: everything, including the three IMU sub-streams.

Before blaming capacity, check that **DLE was negotiated** (link-layer
packets of 251 B, not the default 27 B): `nRF Connect` shows it under the
connection ("Data Length"). With 27-byte PDUs the ~244-byte notifications
fragment into 9 link-layer packets each and the budget collapses. If DLE is
missing on the pod side, set the preferred data length after connect
(`esp_ble_gap_set_data_length`) — one function call in the research service.

## 4. Record the numbers

Write every measured value into `data/live_validation/gate_report.json` —
current per tier, throughput, drift, HR/RMSSD errors, SQI/MAP. `PROCUREMENT.md`
treats those numbers as the only legitimate basis for the Phase 2 order.

### 4a. Bench characterization (before wearing the rig)

Two cheap bench numbers that make the G1 dataset interpretable later:

1. **AD8232 noise floor**: electrodes unplugged, RA/LA/RL shorted together,
   60 s of recording. Record the std of the raw stream in µV-equivalent units
   (after the fixed gain of 6 and the vref scaling from `hardware.yaml`). This
   is the number every future ECG SNR claim hangs on — a generic clone varies
   unit to unit.
2. **I²C sanity**: if the PPG or IMU is absent from the boot log, add 4,7 kΩ
   pull-ups on SDA/SCL before replacing sensors — see the troubleshooting
   table.

### 4b. Motion session (informational, not a gate)

Record 5 minutes of walking plus desk-tapping while wearing the rig. This is
**not** a G1 pass criterion (the gate is seated-and-still per `PROCUREMENT.md`),
but it is the first real data the motion gate (`Architecture.md §74`) will ever
have seen. Save it alongside the gate report; do not let it annul a green run.

## 5. Soak test — the MVP bar

The SOP's 30-minute zero-drop run is a proxy. The project promises 24/7
Tier-0, and the pod's 32-bit microsecond timestamp wraps every ~71,6 minutes —
so only a multi-hour run exercises `DeviceClock` for real.

**MVP criterion: one 6-hour continuous recording (overnight is fine) with
zero dropped frames, monotonic XDF, and the pod on battery.** Passing that,
plus the ten SOP numbers in `data/live_validation/gate_report.json`, is what
unlocks the G2 order.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ESP_ERR_NOT_FOUND` on PPG PART_ID | I²C wiring or missing pull-ups | Verify GPIO 8/9, add 4.7 kΩ pull-ups |
| `[band-core]` missing from the boot log | Wrong build variant | Rebuild with `--variant band-core` |
| ECG reads 0, PPG/IMU fine | AD8232 unpowered, or LO pins floating | Verify 3V3/GND; tie LO± to GND if unused |
| Research service absent | `RESEARCH_SERVICE` disabled or wrong firmware | Check `sdkconfig.defaults.core` |
| HR reads 0 or 200+ bpm | Electrode contact / motion | Clean skin, snug electrodes, hold still |
| Dropped frames > 0 | BLE budget exceeded | Reduce the streams you subscribe to |
| Timestamps jump backwards | 32-bit wrap mishandled | Confirm `DeviceClock` is in the path |

## References

- `ESP-IDF_SETUP.md` — toolchain and build variants
- `PROCUREMENT.md` — the gates and the measurement SOP
- `../hardware.yaml` — the contract every pin and rate comes from
