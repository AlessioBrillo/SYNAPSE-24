# SYNAPSE-24 Forearm Hub Firmware

## Overview
Forearm hub (Tier 0 continuous + hub role). Acquires ECG (AD8232) @ 500 Hz + PPG (MAX30102) @ 50 Hz + IMU (ICM-20948) @ 50 Hz.
Drives wired sync pulse to head pod on GPIO 27.

## Role
**SYNC HUB** — originates wired sync pulse (GPIO 27, 10 µs every 60 s).
Runs BLE clock-sync protocol (1 Hz ping-pong) as responder.

## Sensors
| Sensor | Interface | Rate | Channels |
|--------|-----------|------|----------|
| AD8232 | ADC1_CH0 (GPIO 36) | 500 Hz | 1-lead ECG |
| MAX30102 | I²C (GPIO 21/22) | 50 Hz | Red, IR, Green |
| ICM-20948 | I²C (GPIO 21/22) | 50 Hz | 9-axis (accel/gyro/mag) |

## Features
- ECG R-peak detection @ 500 Hz (Pan-Tompkins, host-side for now)
- PPG SQI @ 100 Hz (Karlen 2013) for motion gate (Architecture.md §74)
- PPG features @ 10 Hz: HR, RMSSD, SDNN, SNR, PI
- IMU features @ 1 Hz: motion intensity, spectral entropy, dominant freq, sleep prob
- Triage inference @ 5 Hz (INT8 TFLM or heuristic fallback)
- Wired sync output on GPIO 27 (drives 10 µs pulse every 60 s)
- BLE LSL streaming: ECG, PPG, IMU, Sync markers, Power telemetry

## Build
```bash
cd firmware/esp32_forearm
idf.py set-target esp32s3
idf.py build
idf.py flash monitor
```

## Pinout
| Function | GPIO |
|----------|------|
| I²C SDA | 21 |
| I²C SCL | 22 |
| AD8232 ADC | 36 (ADC1_CH0) |
| AD8232 DRDY | 4 |
| AD8232 LO+ | 39 |
| AD8232 LO- | 34 |
| MAX30102 INT | 5 |
| ICM-20948 INT | 6 |
| Wired Sync (hub) | 27 (output) |

## Hardware
- ESP32-S3 DevKitC-1 (N8R8: 8 MB Flash + 8 MB PSRAM)
- AD8232 breakout
- MAX30102 breakout
- ICM-20948 breakout
- Ag/AgCl snap electrodes (ECG)
- LiPo 3.7 V 500–1000 mAh (JST-PH 2.0 mm)