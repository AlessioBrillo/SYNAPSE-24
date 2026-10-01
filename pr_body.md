## Summary

Restructures the SYNAPSE-24 firmware into a clean **common driver layer** + **two Tier 0 variants** (head pod + forearm hub) per Architecture.md §23-31 (decoupled sensor/pod/hub) and Roadmap.md Phase 1.

### Firmware Changes (40 new files in `firmware/common/`)
| Module | Key Fixes |
|--------|-----------|
| **Sensor Scheduler** | Non-static task fn, `peek()` API for non-destructive triage reads |
| **PPG Processor** | **Complete IIR BPF (0.5–40 Hz @ 50 Hz) + 12-bit quantization** |
| **IMU Processor** | Field naming consistency (`gyro_rms_*`) |
| **Clock Sync** | Request stored on send → proper reply matching |
| **BLE LSL Bridge** | Ring-buffer sync history, null guards, correct handle resolution |
| **Triage Inference** | Conditional TFLM (`SYNAPSE_HAS_TFLM`) + heuristic fallback |
| **All headers** | Added `#include "esp_err.h"` where `esp_err_t` used |

### Two Firmware Variants
| Variant | Role | Sensors | Sync | Build Dir |
|---------|------|---------|------|-----------|
| **esp32_head** | Tier 0 POD | PPG + IMU @ 50 Hz | **POD** (GPIO 27 input, IRQ) | `firmware/esp32_head/` |
| **esp32_forearm** | Hub | ECG @ 500 Hz + PPG + IMU @ 50 Hz | **HUB** (GPIO 27 output, 60 s pulse) | `firmware/esp32_forearm/` |

### Host Python Stack
- `src/synapse24/hardware/ble_lsl_client.py` — Async `bleak` client with ping-pong clock sync, `ClockSyncHost` drift model
- `ble_client_demo.py` — Refactored demo using `SynapseBleClient`

### CI/CD
- Matrix build for both variants (`esp32_head`, `esp32_forearm`)
- `cppcheck`/`clang-tidy` on common sources
- Binary size regression check per variant

### Quality Gates (All Passed Locally)
| Check | Status |
|-------|--------|
| **Ruff lint** | ✅ All checks passed |
| **Ruff format** | ✅ 130 files already formatted |
| **Core tests** | ✅ 124 passed, 2 skipped (clock_sync, signal_quality, ingestion) |
| **Full suite** | ✅ 419 passed, 23 skipped |

### Known Limitations (Documented)
1. TFLM not linked by default (`SYNAPSE_HAS_TFLM=0`) → heuristic fallback
2. ECG R-peak on-device stubbed; host-side for MVP
3. Power validation pending hardware
4. Mypy on Python 3.14: NumPy 2.x stub syntax error (CI uses Python 3.11 where this doesn't occur)

---

**Ready for hardware bringup** — flash both boards, run `ble_client_demo.py`, validate ≤1 ms drift + zero-drop XDF recording.