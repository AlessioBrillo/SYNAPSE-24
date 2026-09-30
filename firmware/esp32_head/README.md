# SYNAPSE-24 Head Pod Firmware (Tier 0)

## Overview
Head-mounted Tier 0 continuous H24 pod. Acquires PPG (MAX30102) + IMU (ICM-20948) at 50 Hz each.
No ECG on this pod — ECG lives on the forearm hub.

## Role
**SYNC POD** — receives wired sync pulse from forearm hub on GPIO 27.
Runs BLE clock-sync protocol (1 Hz ping-pong) with hub.

## Sensors
| Sensor | Interface | Rate | Channels |
|--------|-----------|------|----------|
| MAX30102 | I²C (GPIO 21/22) | 50 Hz | Red, IR, Green |
| ICM-20948 | I²C (GPIO 21/22) | 50 Hz | 9-axis (accel/gyro/mag) |

## Features
- PPG SQI @ 100 Hz (Karlen 2013) for motion gate (Architecture.md §74)
- PPG features @ 10 Hz: HR, RMSSD, SDNN, SNR, PI
- IMU features @ 1 Hz: motion intensity, spectral entropy, dominant freq, sleep prob
- Triage inference @ 5 Hz (INT8 TFLM or heuristic fallback)
- Wired sync reception on GPIO 27 (10 µs pulse, rising edge IRQ)
- BLE LSL streaming: PPG, IMU, Sync markers, Power telemetry

## Build
```bash
cd firmware/esp32_head
idf.py set-target esp32s3
idf.py build
idf.py flash monitor
```

## Pinout
| Function | GPIO |
|----------|------|
| I²C SDA | 21 |
| I²C SCL | 22 |
| MAX30102 INT | 5 |
| ICM-20948 INT | 6 |
| Wired Sync (pod) | 27 (input, IRQ) |

## Hardware
- ESP32-S3 DevKitC-1 (N8R8: 8 MB Flash + 8 MB PSRAM)
- MAX30102 breakout
- ICM-20948 breakout
- LiPo 3.7 V 500–1000 mAh (JST-PH 2.0 mm)