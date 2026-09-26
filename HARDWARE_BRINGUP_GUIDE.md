# SYNAPSE-24 Phase 1 Hardware Bringup Guide

Complete step-by-step guide to build, flash, and validate the Tier 0 forearm hub prototype.

---

## 1. Shopping List (~€80–120)

| Component | Part | Qty | Est. Cost | Recommended Supplier |
|-----------|------|-----|-----------|---------------------|
| **ECG Frontend** | AD8232 breakout (generic clone) | 1 | €3–7 | AliExpress / eBay / Amazon |
| **PPG Sensor** | MAX30102 breakout (generic) | 1 | €2–6 | AliExpress / eBay / Amazon |
| **IMU** | ICM-20948 (MPU-9250 successor) | 1 | €10–15 | BerryBase / SparkFun / Mouser |
| **MCU** | ESP32-S3 DevKitC-1 (N8R8) | 1 | €10–14 | BerryBase / Mouser / Espressif |
| **Electrodes** | Ag/AgCl snap electrodes (50-pack) | 1 | €8–15 | Amazon / Conrad / Medical supply |
| **Battery** | LiPo 3.7V 2000mAh + JST-PH | 1 | €8–12 | Amazon / hobby store |
| **Prototyping** | 830-point breadboard + jumper kit | 1 | €15–25 | Elegoo / AZDelivery starter kit |
| **Optional** | Raspberry Pi Pico 2 (spare pod) | 1 | €7 | BerryBase / Pimoroni |

**Total: ~€77–110** (excl. shipping)

---

## 2. ESP32-S3 Pinout (from `config/hardware.yaml` + `main.cpp`)

| Signal | GPIO | ESP32-S3 Pin | Notes |
|--------|------|--------------|-------|
| ECG ADC | 1 (ADC1_CH0) | GPIO1 | AD8232 output |
| ECG DRDY | 4 | GPIO4 | AD8232 data ready |
| ECG LO+ | 39 | GPIO39 | Lead-off detect + |
| ECG LO- | 34 | GPIO34 | Lead-off detect - |
| I2C SDA | 21 | GPIO21 | Shared PPG + IMU |
| I2C SCL | 22 | GPIO22 | Shared PPG + IMU |
| PPG INT | 5 | GPIO5 | MAX30102 interrupt |
| IMU INT | 6 | GPIO6 | ICM-20948 interrupt |
| Wired Sync | 27 | GPIO27 | Hub→Head pod (GPIO 21 = I2C SDA reserved) |
| BLE Antenna | — | Keep clear | No metal near PCB antenna |

---

## 3. Wiring Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        ESP32-S3 DevKitC-1                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  3V3 ─────────────────────┬─────────────────────┬─────────────┐ │
│                           │                     │             │ │
│                        ┌──┴──┐             ┌───┴───┐      ┌──┴──┐  │
│                        │AD8232│             │MAX30102│      │ICM-20948│ │
│                        └──┬──┘             └───┬───┘      └──┬──┘  │
│                           │                     │             │    │
│  GPIO1 (ADC) ◄────────────┘                     │             │    │
│  GPIO4 (DRDY) ◄─────────────────────────────────┘             │    │
│  GPIO39 (LO+) ◄───────────────────────────────────────────────┘    │
│  GPIO34 (LO-) ◄────────────────────────────────────────────────────│
│                           │                     │             │    │
│  GPIO21 (SDA) ◄───────────┼─────────────────────┼─────────────┘    │
│  GPIO22 (SCL) ◄───────────┼─────────────────────┼──────────────────│
│                           │                     │                  │
│  GPIO5 (INT)  ◄───────────┘                     │                  │
│  GPIO6 (INT)  ◄─────────────────────────────────┘                  │
│                                                                  │
│  GPIO27 ──► Wired Sync to Head Pod (future Tier 1)              │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

**Electrode Placement (Lead I equivalent):**
- **RA (Right Arm)**: Red snap → AD8232 IN+
- **LA (Left Arm)**: Yellow snap → AD8232 IN-
- **RL (Right Leg)**: Green/Black snap → AD8232 REF (driven right leg)

**PPG Placement**: MAX30102 on volar wrist/forearm with light pressure.

---

## 4. Firmware Build & Flash

### Prerequisites
```bash
# Install ESP-IDF v5.3+ (use official installer)
# https://docs.espressif.com/projects/esp-idf/en/v5.3/esp32s3/get-started/index.html

# Verify installation
. $HOME/esp/esp-idf/export.sh
idf.py --version
```

### Build
```bash
cd firmware/esp32_tier0
idf.py set-target esp32s3
idf.py build
# Output: build/synapse_triage.bin (~200KB)
```

### Flash
```bash
# Option A: Via idf.py (auto-detects port)
idf.py -p COM3 flash monitor

# Option B: Via esptool (explicit, matches hardware_bringup.yaml)
esptool.py --port COM3 --baud 921600 --chip esp32s3 \
  write_flash --flash_mode dio --flash_freq 80m --flash_size 4MB \
  0x0 build/synapse_triage.bin --verify
```

---

## 5. Environment Variables for Live Validation

```powershell
# Windows PowerShell
$env:SYNAPSE_FOREARM_BLE = "aa:bb:cc:dd:ee:ff"  # Your ESP32 BLE MAC (run scanner to find)
$env:SYNAPSE_FOREARM_SERIAL = "COM3"             # Your ESP32 USB serial port
$env:SYNAPSE_HEAD_BLE = "aa:bb:cc:dd:ee:ff"     # Future: Cerelog ESP-EEG MAC
$env:SYNAPSE_HEAD_SERIAL = "COM4"                # Future: Cerelog ESP-EEG USB

# Linux/macOS bash
export SYNAPSE_FOREARM_BLE="aa:bb:cc:dd:ee:ff"
export SYNAPSE_FOREARM_SERIAL="/dev/ttyUSB0"
export SYNAPSE_HEAD_BLE="aa:bb:cc:dd:ee:ff"
export SYNAPSE_HEAD_SERIAL="/dev/ttyUSB1"
```

**Find your ESP32 BLE MAC:**
```bash
uv run python -c "
import asyncio, bleak
async def scan():
    devices = await bleak.BleakScanner.discover(timeout=10)
    for d in devices:
        if d.name and 'SYNAPSE' in d.name.upper():
            print(f'{d.name}: {d.address}')
asyncio.run(scan())
"
```

---

## 6. Validation Commands

### Phase 0 (Software Only — Already Complete)
```bash
# Full test suite
uv run pytest --cov=src --cov-fail-under=80 -v

# Download & process public datasets
uv run python scripts/download_datasets.py --datasets all
uv run python scripts/ingest_datasets.py --dataset both

# Validate against published baselines
uv run python scripts/validate_baseline.py --dataset both
```

### Phase 1 Dry-Run (No Hardware)
```bash
# Validates config, LSL outlets, clock sync, state machine
uv run python scripts/validate_phase1_entry.py --dry-run
```

### Phase 1 Live Validation (With Hardware)
```bash
# 60-second validation: BLE→LSL, sync, promotion/demotion, XDF round-trip
uv run python scripts/validate_phase1_entry.py \
  --config config/hardware_bringup.yaml \
  --duration 60

# Extended live bringup with logging (2 min)
uv run python scripts/hardware_bringup.py \
  --config config/hardware_bringup.yaml \
  --duration 120
```

---

## 7. Expected Live Validation Output

**Success Criteria (Phase 1 Entry Gate):**

| Gate | Threshold | Measured By |
|------|-----------|-------------|
| XDF Zero-Drop | 0 dropped samples | `verify_xdf_roundtrip()` |
| T0 Sync | ≤10ms residual drift | `MultiPodClockSync.get_sync_status(Tier.T0)` |
| T1 Sync | ≤1ms residual drift | `MultiPodClockSync.get_sync_status(Tier.T1)` |
| PPG Completeness | ≥80% of expected 64Hz samples | Sample count vs duration |
| Sample Rates | ±1% of nominal (500/64/100Hz) | Measured over validation window |
| Tier Promotion | T0→T1 on immobility + power + gate | `AcquisitionController` transition log |

**Example PASS Output:**
```
============================================================
PHASE 1 ENTRY GATE RESULT
  [PASS] xdf_zero_drop: 0 (threshold: 0)
  [PASS] tier0_sync_10ms: 3.2ms (threshold: 10.0ms)
  [PASS] tier1_sync_1ms: 0.8ms (threshold: 1.0ms)
  [PASS] ppg_completeness: 98.5% (threshold: 80%)
  [PASS] sample_rate_accuracy: ECG=500.1Hz, IMU=100.0Hz (threshold: ±1%)
  [PASS] eeg_quality_tier1: flatness=0.12, alpha_ratio=0.45 (threshold: ≤0.3, ≥0.3)
OVERALL: PASS
============================================================
```

---

## 8. Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| BLE won't connect | Wrong MAC / not advertising | Scan with `bleak` scanner; check `SYNAPSE_FOREARM_BLE` |
| No ECG data | AD8232 wiring / lead-off | Check GPIO1/4/39/34; verify electrode contact |
| PPG all zeros | I2C address / power | Confirm MAX30102 @0x57 on I2C0; check 3.3V supply |
| IMU not found | ICM-20948 address / INT | Verify 0x68 on I2C0; GPIO6 interrupt |
| Sync drift >10ms | BLE congestion / no markers | Reduce BLE interval; check wired sync GPIO27 |
| Firmware build fails | ESP-IDF version mismatch | Use ESP-IDF v5.3+; `idf.py fullclean && idf.py build` |
| `triage_features_compute_live` returns error | Ring buffer empty | Wait for IMU task to populate (≥10 samples) |

---

## 9. Next Steps After Phase 1 PASS

1. **Collect labeled data**: Wear for 24h+ to build personal dataset
2. **Retrain triage model**: Use `scripts/train_triage.py` with live data
3. **Phase 2**: Add Cerelog ESP-EEG (head pod) for Tier 1 EEG/fNIRS
4. **Phase 3**: Edge AI fusion + DIY fNIRS + 24/7 wearability

---

## 10. Key Architecture References

- **Architecture.md**: Tiered acquisition (§33-43), decoupled pod/hub (§23-31), energy budgets (§55-62), clock sync (§92)
- **Roadmap.md**: Phase budgets (§87-118), validation gates (§8-22)
- **config/hardware.yaml**: Canonical sensor config, sync budgets, power profiles
- **config/hardware_bringup.yaml**: Live validation overrides, BLE UUIDs, triage model path

---

*Generated as part of SYNAPSE-24 Phase 1 deliverables. All code changes tracked in `feature/phase1-hardware-bringup-feature-parity` branch.*