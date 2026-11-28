# Synapse Band v1 — Sports Wrist Wearable Firmware

> **Target:** ESP32-S3, ECG (AD8232) + PPG (MAX30102) + IMU (ICM-20948) + GPS (MAX-M10S) + Temp (TMP117)
> **Stack:** FreeRTOS + ESP-IDF 5.5.5, NimBLE BLE GATT, SPIFFS FIT storage, TFLM Edge AI

## Architecture

Tiered acquisition (T0 continuous / T1 rest-sleep / T2 sport session) per `Architecture.md §33-43`.
Reuses `firmware/common` drivers (scheduler, SQI, triage, power, sync) + Band-specific GPS/Temp/FIT/Provisioning/OTA.

```
esp32_band/
├── config/         NVS-backed HW config (Kconfig defaults + env expansion)
├── sensors/        ECG+PPG+IMU (via common) + GPS MAX-M10S (UBX) + TMP117
├── acquisition/    T0/T1/T2 FSM, motion gate, immobility, night window
├── signal_quality/ PPG SQI/MAP/PI, ECG quality, IMU motion/sleep
├── edge_ai/        TFLM stress triage (3-class) + motion classifier
├── power/          Battery ADC, tier affordability, 24h budget
├── ble/            NimBLE GATT: HR/Battery/DIS + Synapse custom service
├── fit/            FIT writer (SPIFFS, profile 21.138, dev fields)
├── provisioning/   BLE provisioning (mTLS CSR flow only — stub; no Wi-Fi/cloud path)
├── ota/            Signed OTA with rollback — stub
└── main/           app_main entry, event group, main loop
```

## Build & Flash

```bash
cd firmware/esp32_band
idf.py set-target esp32s3
idf.py build
idf.py -p COM3 flash monitor
```

Requires: **ESP-IDF v5.5.5 (pinned in CI)**, ESP32-S3 **N8R8** with 8 MB PSRAM
(TFLM arena). See `docs/hardware/ESP-IDF_SETUP.md`; delete a stale `sdkconfig`
before switching profile, or Kconfig defaults (gain, PSRAM, MTU) will not apply.

The canonical build profiles are `band-full`, `band-core`, `band-bench`
(smoke kit + USB factory shell), `band-bench-ad0` (the same bench image with
the ICM-20948 AD0 high, 0x69), `band-full+RR` and `band-product`. CI builds all
six; `docs/hardware/BRINGUP_SOP.md` uses `band-bench` or `band-bench-ad0`.

## BLE GATT

- Standard: Heart Rate (0x180D), Battery (0x180F), Device Info (0x180A)
- Consumer v2 (contract 2.3): feature stream (notify, 27 B), status, stream
  metadata, control point; device config (encrypted write, W/R)
- Research service: raw ECG/PPG/IMU/POWER + SYNC, six notify characteristics.
  It is compiled in every profile but registered **only outside `band-product`**.
- Contract UUIDs: `contracts/gatt_v2.json` (generated); `gatt_v1.json` is a
  frozen historical record, no longer registered by the firmware.

## FIT

On-device FIT recording is compiled only in `band-full` (the G1 `band-core` and
`band-bench` rigs stream raw data over BLE and record XDF on the host).
Sessions are stored on SPIFFS `session_<utc>.fit`, developer fields:
`synapse_stress`, `synapse_sqi`, `synapse_map`, `synapse_ecg_quality`,
`synapse_sleep_stage`, `synapse_rmssd`, `synapse_sdnn`, `synapse_lf_hf`.
`band-product` disables the raw research service and the factory shell.

## References

- `Architecture.md` — tiered acquisition, power budget
- `docs/hardware/BRINGUP_SOP.md` — wiring, bringup, validation
- `docs/contracts/AVERYN_INTERFACE.md` and `contracts/gatt_v2.json` — the BLE/FIT contracts
