# Synapse Band v1 — Sports Wrist Wearable Firmware

> **Target:** ESP32-S3, ECG (AD8232) + PPG (MAX30102) + IMU (ICM-20948) + GPS (MAX-M10S) + Temp (TMP117)
> **Stack:** FreeRTOS + ESP-IDF 5.2+, Bluedroid BLE GATT, LittleFS FIT storage, TFLM Edge AI

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
├── ble/            Bluedroid GATT: HR/Battery/DIS + Synapse custom service
├── fit/            FIT writer (LittleFS, profile 21.138, dev fields)
├── provisioning/   BLE provisioning (WiFi + cloud + mTLS CSR flow)
├── ota/            Signed OTA with rollback
└── main/           app_main entry, event group, main loop
```

## Build & Flash

```bash
cd firmware/esp32_band
idf.py set-target esp32s3
idf.py build
idf.py -p COM3 flash monitor
```

Requires: ESP-IDF 5.2+, ESP32-S3 with 8MB PSRAM (TFLM arena 64KB).

## BLE GATT

- Standard: Heart Rate (0x180D), Battery (0x180F), Device Info (0x180A)
- Custom Synapse service (128-bit): feature stream (notify, 20B), device config (R/W), provisioning (W/N)

## FIT

Sessions stored on LittleFS `/storage/session_<utc>.fit`, developer fields:
`synapse_stress`, `synapse_sqi`, `synapse_map`, `synapse_ecg_quality`,
`synapse_sleep_stage`, `synapse_rmssd`, `synapse_sdnn`, `synapse_lf_hf`.

## References

- `Architecture.md` — tiered acquisition, power budget
- `HARDWARE_BRINGUP.md` — wiring, bringup, validation
- `SYNAPSE-AVERYN-STRATEGY/04_SYNAPSE_DEVICE_SDK_SPEC.md` — BLE/FIT/MQTT contracts
