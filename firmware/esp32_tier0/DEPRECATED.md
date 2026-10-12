
# DEPRECATED — firmware for this pod now lives in irmware/esp32_band`n
**Archived during the Hardware Readiness Gate.** The Band firmware (`firmware/esp32_band`) is the`ncanonical ESP32-S3 project and covers this rig through its `band-core` build variant`n(ECG + PPG + IMU, no GPS/TMP117/FIT/provisioning/OTA).`n`nDo not build or flash this project. The shared library `firmware/common/` is **not**`ndeprecated -- `esp32_band` links it, as do the host parity tests in`n`tests/firmware_host/`. This directory is kept only as a reference for the port.`n`nSee `docs/hardware/READINESS_GATE.md`.`n
