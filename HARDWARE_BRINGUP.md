# SYNAPSE-24 Phase 1 Hardware Bringup Guide

> **Target:** Live ECG+PPG+IMU streams synchronized via LSL, validated HR/HRV, zero-drop XDF export
> **Hardware Cost:** ~€90–120
> **Time to First Valid XDF:** ~2 hours (including wiring)

---

## 1. Bill of Materials (Exact Parts)

| Item | Qty | Spec | Recommended Vendor (EU) | Est. EUR |
|------|-----|------|------------------------|----------|
| ESP32-S3 DevKitC-1 | 1 | **8MB PSRAM**, 16MB Flash, USB-C | BerryBase, Mouser, DigiKey, Amazon.de | €12–15 |
| AD8232 ECG Breakout | 1 | Generic clone, snap-electrode header | eBay, AliExpress, Amazon.de | €3–7 |
| MAX30102 PPG Breakout | 1 | Generic, red/IR dual wavelength | eBay, AliExpress, Amazon.de | €2–6 |
| ICM-20948 9-axis IMU | 1 | Breakout board (SparkFun/Adafruit compatible) | BerryBase, Reichelt, AliExpress | €10–15 |
| Ag/AgCl Snap Electrodes | 1 pack (50) | Single-use, gel-based | Amazon, Conrad, Reichelt | €8–15 |
| Breadboard + Jumpers | 1 | 830-point + Dupont wires (M/M, M/F, F/F) | Elegoo Starter Kit, Amazon | €15–25 |
| LiPo 3.7V 500mAh | 1 | JST-PH 2-pin connector | BerryBase, Reichelt | €5–8 |
| Micro-USB Data Cable | 2 | Must support data (not charge-only) | Any | €3–5 |

**Total: ~€58–86** — well within the €90–120 Phase 1 budget.

> ⚠️ **CRITICAL:** You **must** buy **ESP32-S3** (not ESP32, not ESP32-C3). The 8MB PSRAM is required for the TFLM tensor arena (~60 KB model + activation buffers). ESP32-C3 has no PSRAM and will OOM during triage inference.

---

## 2. Wiring Diagram — Forearm Hub (Tier 0 Pod)

### Pin Mapping (matches `config/hardware.yaml` + `firmware/esp32_tier0/main/main.cpp`)

```
ESP32-S3 DevKitC-1                    Sensor Breakouts
┌─────────────────────────┐           ┌─────────────────────────────────────┐
│                         │           │                                     │
│  GPIO 36  (ADC1_CH0) ───┼──────────▶│ AD8232 OUTPUT                       │
│  GPIO 39                ├──────────▶│ AD8232 LO+ (lead-off detect +)      │
│  GPIO 34                ├──────────▶│ AD8232 LO- (lead-off detect -)      │
│                         │           │ AD8232 VCC ─── 3V3                  │
│  GPIO 21 (I2C SDA) ─────┼──────────▶│ MAX30102 SDA                        │
│  GPIO 22 (I2C SCL) ─────┼──────────▶│ MAX30102 SCL                        │
│                         │     ┌────▶│ MAX30102 INT ─── GPIO 5             │
│                         │     │     │ MAX30102 VCC ─── 3V3                │
│                         │     │     │ MAX30102 GND ─── GND                │
│                         │     │     │                                     │
│                         │     │     │ ICM-20948 SDA ─── GPIO 21 (shared)  │
│                         │     │     │ ICM-20948 SCL ─── GPIO 22 (shared)  │
│  GPIO 6                 ├─────┘     │ ICM-20948 INT ─── GPIO 6            │
│                         │           │ ICM-20948 VCC ─── 3V3               │
│                         │           │ ICM-20948 GND ─── GND               │
│                         │           │                                     │
│  3V3 ───────────────────┼───────────▶│ VCC (all sensors)                   │
│  GND ───────────────────┼───────────▶│ GND (all sensors)                   │
│                         │           │                                     │
│  LiPo JST-PH ───────────┘           │ (battery powers ESP32-S3 directly)  │
└─────────────────────────┘           └─────────────────────────────────────┘
```

### Electrode Placement (Forearm — Lead I Equivalent)

```
    ┌─────────────────────────────────────┐
    │          LEFT FOREARM               │
    │                                     │
    │   ● RA (Right Arm)  ── Red snap     │  ← Proximal (near elbow)
    │                                     │
    │   ● LA (Left Arm)   ── Yellow snap  │  ← Distal (near wrist)
    │                                     │
    │   ● RL (Right Leg)  ── Green snap   │  ← Reference (olecranon/bony)
    │                                     │
    └─────────────────────────────────────┘
    
    AD8232 Header:  RED → RA,  YELLOW → LA,  GREEN → RL
```

> **Note:** This is a **single-lead ECG (Lead I equivalent)** optimized for HR/HRV, not diagnostic 12-lead. For Phase 2, the Cerelog ESP-EEG will provide resting ECG via ADS1299 shared channels.

---

## 3. Firmware Build & Flash

### Prerequisites

```bash
# 1. Install ESP-IDF 5.2+ (Windows)
#    Download: https://dl.espressif.com/dl/esp-idf/esp-idf-tools-setup-1.2.exe
#    Select: ESP32-S3 target, VS Code extension optional

# 2. Clone and enter repo (if not already)
git clone https://github.com/AlessioBrillo/SYNAPSE-24.git
cd SYNAPSE-24

# 3. Activate ESP-IDF environment (new terminal)
#    Windows: %USERPROFILE%\esp\esp-idf\export.bat
#    Linux/Mac: source ~/esp/esp-idf/export.sh
```

### Build & Flash

```bash
# 1. Navigate to firmware
cd firmware/esp32_tier0

# 2. Set target to ESP32-S3
idf.py set-target esp32s3

# 3. Configure (optional - uses sdkconfig.defaults)
idf.py menuconfig
#   → Component config → ESP32S3-specific → Enable PSRAM support (should be ON by default)

# 4. Build
idf.py build

# 5. Flash (adjust COM port to your system)
idf.py -p COM3 flash monitor

#    Expected output:
#    I (xxx) synapse_tier0: SYNAPSE-24 ESP32-S3 Tier 0 Firmware v0.1.0
#    I (xxx) synapse_tier0: Architecture.md: Decoupled sensor pod, Tier 0 continuous H24
#    I (xxx) synapse_tier0: Roadmap.md: Live ECG+PPG+IMU streaming, synchronized in LSL
#    I (xxx) synapse_tier0: MAX30102 PPG initialized (I2C port 0, addr 0x57)
#    I (xxx) synapse_tier0: ICM-20948 IMU initialized (I2C port 0, addr 0x68)
#    I (xxx) synapse_tier0: BLE LSL bridge started, advertising as "SYNAPSE_T0"
#    I (xxx) synapse_tier0: Triage inference initialized (model: triage_int8, 11 features)
#    I (xxx) synapse_tier0: All subsystems started. Entering main loop...
#    I (xxx) synapse_tier0: Stats: ECG=5000, PPG=640, IMU=1000 | Dropped: ECG=0, PPG=0, IMU=0
#    I (xxx) synapse_tier0: PPG SQI: sqi=0.72, pi=1.23%, map=0.15, gate_armed=1, consecutive_clean=2
#    I (xxx) synapse_tier0: Triage: baseline=0.89, stress=0.08, artifact=0.03, class=0, time=1245 us
```

> **If build fails:** Ensure ESP-IDF 5.2+ and `CONFIG_SPIRAM_USE_CAPS_ALLOC=y` in sdkconfig. The `partitions.csv` allocates 1.5MB for app + 2MB for TFLM arena.

---

## 4. Live Validation — `validate_phase1_entry.py`

### Setup Environment

```bash
# 1. Copy env template and edit
cp .env.example .env
# Edit .env with your COM port:
# SYNAPSE_FOREARM_SERIAL=COM3

# 2. Install Python deps (if not already)
uv sync --dev

# 3. Run live validation (60s capture → XDF → HR/HRV gate)
uv run python scripts/validate_phase1_entry.py --live-hardware --config config/hardware_bringup.yaml
```

### Expected Output

```
================================================================================
SYNAPSE-24 Phase 1 Entry Validation — Live Hardware
================================================================================
Config: config/hardware_bringup.yaml
Target Pod: forearm_hub (Tier 0)
Mode: multi_pod
Duration: 60s
Output: data/processed/live_validation/synapse_live_20250926_143022.xdf
--------------------------------------------------------------------------------
[1/6] Resolving LSL streams...
    ✅ SYNAPSE_ECG_T0  (ECG, 1ch, 500Hz, float32)
    ✅ SYNAPSE_PPG_T0  (PPG, 2ch, 64Hz, float32)
    ✅ SYNAPSE_ACC_T0  (ACC, 3ch, 100Hz, float32)
    ✅ SYNAPSE_GYRO_T0 (GYRO, 3ch, 100Hz, float32)
    ✅ SYNAPSE_MAG_T0  (MAG, 3ch, 100Hz, float32)
    ✅ SYNAPSE_Markers (Markers, 1ch, irregular, string)

[2/6] Capturing 60s of live data...
    Streaming... ████████████████████ 60/60s
    Samples captured: ECG=30000, PPG=3840, ACC=6000

[3/6] Validating HR/HRV...
    ✅ HR: 68.2 bpm (manual count: 68 bpm) → MAE = 0.2 bpm ≤ 2 bpm ✓
    ✅ RMSSD: 42.1 ms (reference: 41.8 ms) → MAE = 0.3 ms < 5 ms ✓
    ✅ PPG SQI: 0.72 ≥ 0.30 (bringup threshold) ✓
    ✅ PPG MAP: 0.15 ≤ 0.50 ✓
    ✅ Motion gate: ARMED (2 consecutive clean windows)

[4/6] XDF round-trip verification...
    ✅ Zero dropped samples across all streams
    ✅ Timestamps monotonic, no gaps
    ✅ Stream metadata matches hardware.yaml config

[5/6] Clock sync verification (single pod)...
    ✅ LSL local_clock domain consistent
    ✅ No backward timestamp jumps

[6/6] Triage inference health check...
    ✅ Model loaded: triage_int8.tflite (11 features, 3 classes)
    ✅ Inference latency: 1.2 ms avg (budget: ≤5 ms)
    ✅ TFLM arena used: 48.3 KB / 64 KB

================================================================================
✅ PHASE 1 ENTRY GATE: PASSED
================================================================================
XDF saved: data/processed/live_validation/synapse_live_20250926_143022.xdf
Next step: Analyze XDF → Phase 2 procurement (Cerelog ESP-EEG)
```

---

## 5. XDF Analysis Quickstart

```python
# In Python REPL or Jupyter:
from synapse24.utils import validate_xdf
from pathlib import Path

xdf_path = Path("data/processed/live_validation/synapse_live_20250926_143022.xdf")
summary = validate_xdf(xdf_path)

print(summary)
# Output:
# {
#   'file': 'synapse_live_20250926_143022.xdf',
#   'streams': [
#     {'name': 'SYNAPSE_ECG_T0', 'type': 'ECG', 'channels': 1, 'fs': 500, 'samples': 30000, 'duration_s': 60.0},
#     {'name': 'SYNAPSE_PPG_T0', 'type': 'PPG', 'channels': 2, 'fs': 64, 'samples': 3840, 'duration_s': 60.0},
#     {'name': 'SYNAPSE_ACC_T0', 'type': 'ACC', 'channels': 3, 'fs': 100, 'samples': 6000, 'duration_s': 60.0},
#     {'name': 'SYNAPSE_GYRO_T0', 'type': 'GYRO', 'channels': 3, 'fs': 100, 'samples': 6000, 'duration_s': 60.0},
#     {'name': 'SYNAPSE_MAG_T0', 'type': 'MAG', 'channels': 3, 'fs': 100, 'samples': 6000, 'duration_s': 60.0},
#     {'name': 'SYNAPSE_Markers', 'type': 'Markers', 'channels': 1, 'fs': 0, 'samples': 12, 'duration_s': 60.0}
#   ],
#   'zero_drop': True,
#   'timestamp_monotonic': True,
#   'quality_metadata_present': True
# }
```

### Plot ECG + R-Peaks (Validation)

```python
import pyxdf
import numpy as np
import matplotlib.pyplot as plt
from synapse24.signal_quality import detect_r_peaks_neurokit

streams, header = pyxdf.load_xdf(xdf_path)
ecg_stream = next(s for s in streams if s["info"]["name"][0] == "SYNAPSE_ECG_T0")
ecg_data = ecg_stream["time_series"].flatten()
ecg_ts = ecg_stream["time_stamps"]

r_peaks = detect_r_peaks_neurokit(ecg_data, 500)

plt.figure(figsize=(15, 5))
plt.plot(ecg_ts, ecg_data, "b-", alpha=0.7, label="ECG")
plt.plot(ecg_ts[r_peaks], ecg_data[r_peaks], "ro", label=f"R-peaks ({len(r_peaks)})")
plt.xlabel("Time (s)")
plt.ylabel("Amplitude (µV)")
plt.title("Live ECG with Detected R-Peaks")
plt.legend()
plt.grid(True, alpha=0.3)
plt.show()
```

---

## 6. Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| `ESP_ERR_NOT_FOUND` on MAX30102 PART_ID | I2C wiring wrong / pullups missing | Check SDA/SCL on GPIO 21/22; add 4.7kΩ pullups if breakout lacks them |
| `ECG=0` samples, PPG/IMU working | AD8232 not powered / LO pins floating | Verify 3V3/GND on AD8232; tie LO+/LO- to GND if not using lead-off detect |
| BLE not advertising | Antenna / flash config | Ensure `CONFIG_BT_ENABLED=y`; check `sdkconfig.defaults` has BLE enabled |
| Triage inference fails | TFLM model not embedded | Run `scripts/quantize_and_deploy.py` first to generate `model_data.h` |
| HR reads 0 or 200+ bpm | Poor electrode contact / motion | Clean skin with alcohol; ensure snug electrode placement; minimize arm movement |
| PPG SQI < 0.3 | Ambient light / loose sensor | Shield MAX30102 from light; tighten strap; increase LED current (0x1F → 0x3F) |
| `idf.py flash` fails | Wrong COM port / driver | Check Device Manager → Ports; install CP210x/CH340 driver if needed |

---

## 7. Phase 1 → Phase 2 Transition Checklist

After Phase 1 validation passes:

- [ ] XDF file validates with `validate_xdf()` — zero drops, monotonic timestamps
- [ ] HR MAE ≤ 2 bpm vs manual count
- [ ] RMSSD MAE < 5 ms
- [ ] PPG SQI ≥ 0.3 sustained for >30s
- [ ] Triage inference running @ ≤5 ms latency
- [ ] Power draw measured: ~5–10 mA average (projects to 24h+ on 500mAh LiPo)

**Then procure Phase 2 hardware:**
1. **Cerelog ESP-EEG ($349.99)** — 8-ch ADS1299, ESP32, BrainFlow/LSL native → `head_pod`
2. **EmotiBit (~$500)** — Validated PPG/EDA/IMU/Temp reference → ground truth for DIY boards
3. **Muse S (~$300)** — Consumer dry-EEG + PPG for Tier 0 continuous sleep

---

## 8. References

- **Architecture.md** — Decoupled sensor/hub, tiered acquisition, edge triage (§23–53)
- **Roadmap.md** — Phase-based execution, budget, dataset references (§138–158)
- **config/hardware.yaml** — Canonical hardware configuration (pods, sync, power, motion)
- **config/hardware_bringup.yaml** — Phase 1 overrides (firmware, BLE, validation params)
- **firmware/esp32_tier0/** — ESP32-S3 Tier 0 firmware (sensor scheduler, BLE LSL bridge, triage)
- **scripts/validate_phase1_entry.py** — Live hardware validation entry point

---

*Generated for SYNAPSE-24 Phase 1 — ECG+PPG+IMU Tier 0 Bringup*
*Next: Phase 2 — Real EEG (Cerelog) + Synchronized Multimodal Sleep Capture*