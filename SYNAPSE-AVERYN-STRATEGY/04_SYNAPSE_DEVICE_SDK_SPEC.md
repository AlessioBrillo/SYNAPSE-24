# Synapse Device SDK Specification
## "Interfacce per Registrare, Estrarre e Sincronizzare Dati con Qualsiasi Dispositivo"

---

## 🎯 Obiettivo dell'SDK

> **Fornire a qualsiasi developer (ricercatori, app builder, hobbyist) gli strumenti per: scoprire dispositivi Synapse, configurarli (provisioning), registrare sessioni, streammare dati in tempo reale, esportare in formati standard — senza dipendere da Averyn o Synapse Cloud.**

---

## 📦 Distribuzione

| SDK | Package Manager | Install Command | Target |
|-----|-----------------|-----------------|--------|
| **Python** | PyPI | `pip install synapse-device` | Host PC, Raspberry Pi, Server, CI/CD |
| **C/C++** | Git submodule / CMake | `add_subdirectory(synapse-cpp-sdk)` | Embedded Linux, RTOS (Zephyr, FreeRTOS), ESP-IDF |
| **Rust** | crates.io | `cargo add synapse-device` | High-performance tools, CLI, WASM |

---

## 🐍 Python SDK — `synapse-device`

### Installazione
```bash
pip install synapse-device[ble,mqtt,lsl,cli]  # Extra opzionali
# Oppure per sviluppo locale:
pip install -e sdk/python
```

### CLI — `synapse` Command

```bash
# === DISCOVERY ===
synapse discover                          # Scan BLE + Serial + USB
synapse discover --ble --timeout 30
synapse discover --serial /dev/ttyUSB0

# Output:
# Found 2 devices:
#   SYN-BAND-A1B2  (BLE, RSSI -42)  FW: 1.1.0  HW: band-v1.0  Battery: 87%
#   SYN-HEAD-C3D4  (Serial, /dev/ttyACM0)  FW: 0.9.0  HW: headband-v0.9  Battery: 92%

# === PROVISIONING ===
synapse configure SYN-BAND-A1B2                    # Wizard interattivo
synapse configure SYN-BAND-A1B2 --wifi "SSID" "PASS" --cloud-url "https://synapse.mydomain.com" --auto-cert

# === RECORDING ===
synapse record SYN-BAND-A1B2 --duration 3600 --output session.fit
synapse record SYN-BAND-A1B2 --duration 7200 --output session.xdf --format xdf
synapse record SYN-BAND-A1B2 --sport running --output run_20260927.fit

# === STREAMING (Real-time) ===
synapse stream SYN-BAND-A1B2 --protocol lsl       # Lab Streaming Layer
synapse stream SYN-BAND-A1B2 --protocol mqtt --host mqtt.mydomain.com --topic synapse/devices/SYN-BAND-A1B2
synapse stream SYN-BAND-A1B2 --protocol http --endpoint https://synapse.mydomain.com/ingest

# === EXPORT / CONVERSION ===
synapse export session.fit --format xdf --output session.xdf
synapse export session.fit --format csv --output session.csv
synapse export session.fit --format gpx --output route.gpx
synapse export session.xdf --format fit --output session.fit

# === OTA UPDATE ===
synapse ota SYN-BAND-A1B2 --firmware firmware_v1.2.0.bin
synapse ota SYN-BAND-A1B2 --check-only            # Solo verifica versione

# === CALIBRATION ===
synapse calibrate SYN-BAND-A1B2 --type impedance
synapse calibrate SYN-BAND-A1B2 --type led_current --channel ppg_green
synapse calibrate SYN-BAND-A1B2 --type accel_bias
synapse calibrate SYN-BAND-A1B2 --type gps

# === DEVICE INFO ===
synapse info SYN-BAND-A1B2
# Output:
# Device: SYN-BAND-A1B2
# Hardware: band-v1.0
# Firmware: 1.1.0
# MAC: A4:CF:12:34:56:78
# Battery: 87% (Charging: No)
# Sensors: ECG(AD8232), PPG(MAX30102), IMU(ICM-20948), GPS(MAX-M10S), Temp
# Storage: 4.2 MB / 8 MB (5 sessions)
# Cloud: Connected (synapse.mydomain.com)
# Last Sync: 2026-09-27 08:15:22
```

### Library API (Programmatic Use)

```python
# synapse_device/__init__.py
from synapse_device import (
    SynapseDevice,
    discover_devices,
    DeviceType,
    TransportType,
    SessionConfig,
    ExportFormat,
    SignalQuality,
    StressLevel,
)

# --- Discovery ---
devices = discover_devices(ble=True, serial=True, timeout=10.0)
for dev in devices:
    print(f"{dev.serial} ({dev.transport}) FW:{dev.firmware_version} Battery:{dev.battery_level}%")

# --- Connection ---
device = SynapseDevice(serial="SYN-BAND-A1B2")
device.connect(transport=TransportType.BLE)  # or SERIAL, USB

# --- Provisioning (one-time setup) ---
provisioning_result = device.provision(
    wifi_ssid="MyWiFi",
    wifi_password="secret",
    cloud_url="https://synapse.mydomain.com",
    # Device cert generato automaticamente se non fornito
)

# --- Recording ---
session_config = SessionConfig(
    sport_type="running",  # running, cycling, sleep, generic
    duration_seconds=3600,
    enable_gps=True,
    enable_ecg=True,
    ecg_sampling_rate=500,  # Hz
    enable_ppg=True,
    ppg_sampling_rate=64,
    enable_imu=True,
    imu_sampling_rate=100,
)

session = device.record_session(session_config)
# Session object has real-time callbacks:
session.on_heart_rate = lambda hr, rr: print(f"HR: {hr} bpm, RR: {rr} ms")
session.on_stress_triage = lambda level: print(f"Stress: {level.name}")
session.on_signal_quality = lambda sq: print(f"SQI: {sq.ppg_sqi:.2f}, MAP: {sq.ppg_map:.2f}")

# Wait for completion or stop early
session.wait()  # or session.stop()

# --- Access Recorded Data ---
fit_data = session.to_fit()  # FIT file bytes
xdf_data = session.to_xdf()  # XDF file bytes
csv_data = session.to_csv()  # CSV string

# Or save directly
session.save("my_run.fit", format=ExportFormat.FIT)
session.save("my_run.xdf", format=ExportFormat.XDF)

# --- Streaming (Non-blocking) ---
stream = device.create_stream(
    protocol="lsl",  # "lsl", "mqtt", "http", "callback"
    lsl_stream_name="SYNAPSE_BAND",
    mqtt_host="mqtt.example.com",
    mqtt_topic="synapse/devices/SYN-BAND-A1B2",
    http_endpoint="https://cloud.example.com/ingest",
    callback=lambda sample: process_sample(sample),
)
stream.start()
# ... do other things ...
stream.stop()

# --- OTA Update ---
ota_result = device.ota_update("firmware_v1.2.0.bin", verify_signature=True)
print(f"OTA: {ota_result.status}")  # SUCCESS, FAILED, ROLLED_BACK

# --- Calibration ---
cal_result = device.calibrate(
    calibration_type="impedance", parameters={"target_impedance_kohm": 10}
)

# --- Device Settings ---
settings = device.get_settings()
settings.ecg_enabled = True
settings.ppg_led_current_ma = 15
settings.accel_range_g = 8
settings.gps_mode = "continuous"  # continuous, smart, off
device.apply_settings(settings)

# --- Disconnect ---
device.disconnect()
```

### Data Structures

```python
# synapse_device/models.py
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List
from enum import Enum


class TransportType(Enum):
    BLE = "ble"
    SERIAL = "serial"
    USB = "usb"
    TCP = "tcp"


class SportType(Enum):
    RUNNING = "running"
    CYCLING = "cycling"
    SWIMMING = "swimming"
    HIKING = "hiking"
    SLEEP = "sleep"
    GENERIC = "generic"


class StressLevel(Enum):
    BASELINE = 0
    STRESS = 1
    ARTIFACT = 2


class SignalQualityMetrics:
    ecg_sqi: float  # 0.0-1.0
    ppg_sqi: float
    ppg_map: float  # Motion Artifact Probability 0.0-1.0
    ppg_pi: float  # Perfusion Index %
    ecg_rpeak_confidence: float
    imu_motion_level: float  # 0.0-1.0


@dataclass
class DeviceInfo:
    serial: str
    hardware_version: str
    firmware_version: str
    mac_address: str
    battery_level: int  # 0-100
    is_charging: bool
    sensors: List[str]  # ["ECG", "PPG", "IMU", "GPS", "TEMP"]
    storage_used_mb: float
    storage_total_mb: float
    cloud_connected: bool
    cloud_url: Optional[str]
    last_sync: Optional[datetime]


@dataclass
class SessionConfig:
    sport_type: SportType = SportType.GENERIC
    duration_seconds: Optional[int] = None  # None = infinite until stop()
    enable_gps: bool = True
    enable_ecg: bool = True
    ecg_sampling_rate: int = 500
    enable_ppg: bool = True
    ppg_sampling_rate: int = 64
    enable_imu: bool = True
    imu_sampling_rate: int = 100
    enable_temp: bool = True
    auto_pause: bool = True  # Motion-based pause


@dataclass
class SessionSample:
    timestamp: datetime
    heart_rate: Optional[int] = None
    rr_intervals: Optional[List[int]] = None  # ms
    ecg: Optional[List[float]] = None  # Raw ECG samples
    ppg: Optional[List[float]] = None  # Raw PPG samples (dual wavelength)
    accel: Optional[tuple] = None  # (x, y, z) g
    gyro: Optional[tuple] = None  # (x, y, z) deg/s
    gps: Optional[dict] = None  # lat, lon, alt, speed, accuracy
    temperature: Optional[float] = None  # Celsius
    signal_quality: Optional[SignalQualityMetrics] = None
    stress_level: Optional[StressLevel] = None
    battery: Optional[int] = None


class Session:
    """Active recording session with real-time callbacks."""

    def __init__(self, device: "SynapseDevice", config: SessionConfig):
        self.device = device
        self.config = config
        self.samples: List[SessionSample] = []
        self.start_time: datetime
        self.end_time: Optional[datetime] = None

        # Callbacks (set by user)
        self.on_sample: Optional[callable] = None
        self.on_heart_rate: Optional[callable] = None
        self.on_rr_intervals: Optional[callable] = None
        self.on_stress_triage: Optional[callable] = None
        self.on_signal_quality: Optional[callable] = None
        self.on_gps: Optional[callable] = None
        self.on_error: Optional[callable] = None

    def wait(self) -> "Session": ...
    def stop(self) -> "Session": ...
    def to_fit(self) -> bytes: ...
    def to_xdf(self) -> bytes: ...
    def to_csv(self) -> str: ...
    def save(self, path: str, format: ExportFormat): ...

    @property
    def duration(self) -> float: ...
    @property
    def is_active(self) -> bool: ...
```

---

## 🔧 C/C++ Embedded SDK — `synapse-cpp-sdk`

### Struttura
```
synapse-cpp-sdk/
├── include/synapse/
│   ├── synapse.h              # Main header
│   ├── device.h               # Device abstraction
│   ├── ble_gatt.h             # BLE GATT client/server
│   ├── fit_writer.h           # FIT file writer
│   ├── acquisition_fsm.h      # T0/T1/T2 state machine
│   ├── edge_ai.h              # TFLM inference API
│   ├── signal_quality.h       # Embedded SQI/MAP/RMSSD
│   ├── provisioning.h         # Provisioning protocol
│   ├── ota.h                  # OTA update
│   └── config.h               # Hardware config parser
├── src/
│   ├── device.c
│   ├── ble_gatt.c
│   ├── fit_writer.c
│   ├── acquisition_fsm.c
│   ├── edge_ai.c
│   ├── signal_quality.c
│   ├── provisioning.c
│   ├── ota.c
│   └── config.c
├── CMakeLists.txt
└── examples/
    ├── esp32_band_firmware/
    ├── linux_ble_gateway/
    └── zephyr_sensor_node/
```

### API Principale (C)

```c
// include/synapse/device.h
typedef enum {
    SYNAPSE_TRANSPORT_BLE = 0,
    SYNAPSE_TRANSPORT_SERIAL,
    SYNAPSE_TRANSPORT_USB,
} synapse_transport_t;

typedef enum {
    SYNAPSE_SENSOR_ECG = (1 << 0),
    SYNAPSE_SENSOR_PPG = (1 << 1),
    SYNAPSE_SENSOR_IMU = (1 << 2),
    SYNAPSE_SENSOR_GPS = (1 << 3),
    SYNAPSE_SENSOR_TEMP = (1 << 4),
} synapse_sensor_mask_t;

typedef struct {
    uint32_t duration_sec;           // 0 = infinite
    synapse_sensor_mask_t sensors;   // Bitmask of enabled sensors
    uint16_t ecg_sample_rate;        // Hz
    uint16_t ppg_sample_rate;        // Hz
    uint16_t imu_sample_rate;        // Hz
    bool gps_enabled;
    bool auto_pause;
    synapse_sport_t sport_type;
} synapse_session_config_t;

// Callback types
typedef void (*synapse_sample_cb_t)(const synapse_sample_t* sample, void* user_data);
typedef void (*synapse_hr_cb_t)(uint16_t hr, const uint16_t* rr_intervals, size_t rr_count, void* user_data);
typedef void (*synapse_stress_cb_t)(synapse_stress_level_t level, void* user_data);
typedef void (*synapse_sq_cb_t)(const synapse_signal_quality_t* sq, void* user_data);

// Device handle
typedef struct synapse_device_s synapse_device_t;

// Lifecycle
synapse_device_t* synapse_device_create(const char* serial, synapse_transport_t transport);
void synapse_device_destroy(synapse_device_t* dev);
int synapse_device_connect(synapse_device_t* dev);
int synapse_device_disconnect(synapse_device_t* dev);

// Provisioning
int synapse_device_provision(synapse_device_t* dev,
    const char* wifi_ssid, const char* wifi_pass,
    const char* cloud_url, const uint8_t* cert_pem, size_t cert_len,
    const uint8_t* key_pem, size_t key_len);

// Recording
typedef struct synapse_session_s synapse_session_t;
synapse_session_t* synapse_session_start(synapse_device_t* dev, const synapse_session_config_t* config);
int synapse_session_stop(synapse_session_t* session);
int synapse_session_wait(synapse_session_t* session, uint32_t timeout_ms);

// Callbacks
void synapse_session_set_sample_callback(synapse_session_t* session, synapse_sample_cb_t cb, void* user_data);
void synapse_session_set_hr_callback(synapse_session_t* session, synapse_hr_cb_t cb, void* user_data);
void synapse_session_set_stress_callback(synapse_session_t* session, synapse_stress_cb_t cb, void* user_data);
void synapse_session_set_sq_callback(synapse_session_t* session, synapse_sq_cb_t cb, void* user_data);

// Export
int synapse_session_export_fit(synapse_session_t* session, const char* filepath);
int synapse_session_export_xdf(synapse_session_t* session, const char* filepath);

// OTA
int synapse_device_ota_update(synapse_device_t* dev, const uint8_t* firmware, size_t size, const uint8_t* signature, size_t sig_len);

// Calibration
int synapse_device_calibrate(synapse_device_t* dev, synapse_calibration_type_t type, const void* params);

// Settings
int synapse_device_get_settings(synapse_device_t* dev, synapse_settings_t* settings);
int synapse_device_apply_settings(synapse_device_t* dev, const synapse_settings_t* settings);

// Info
int synapse_device_get_info(synapse_device_t* dev, synapse_device_info_t* info);
```

### Esempio: ESP32 Band Firmware (minimal)
```c
// examples/esp32_band_firmware/main.c
#include "synapse/device.h"
#include "synapse/ble_gatt.h"
#include "synapse/acquisition_fsm.h"
#include "synapse/edge_ai.h"
#include "synapse/fit_writer.h"
#include "synapse/provisioning.h"
#include "synapse/ota.h"

static synapse_device_t* g_device = NULL;
static synapse_session_t* g_session = NULL;

void app_main(void) {
    // 1. Initialize hardware drivers
    synapse_hardware_init();  // AD8232, MAX30102, ICM-20948, MAX-M10S
    
    // 2. Create device instance
    g_device = synapse_device_create("SYN-BAND-A1B2", SYNAPSE_TRANSPORT_BLE);
    
    // 3. Setup BLE GATT Server
    synapse_ble_gatt_init();
    synapse_ble_gatt_register_standard_services();  // HR, RSC, CSC, Battery, DIS
    synapse_ble_gatt_register_synapse_service();    // Custom: ECG, PPG, IMU, SQ, Stress
    
    // 4. Start advertising
    synapse_ble_gatt_start_advertising("SYNAPSE_BAND");
    
    // 5. Load TFLM models
    synapse_edge_ai_load_model(SYNAPSE_MODEL_STRESS_TRIAGE, "stress_triage.tflite");
    synapse_edge_ai_load_model(SYNAPSE_MODEL_MOTION_CLASSIFIER, "motion_classifier.tflite");
    
    // 6. Main loop (FreeRTOS task)
    xTaskCreate(main_loop_task, "main_loop", 8192, NULL, 5, NULL);
}

void main_loop_task(void* pvParameters) {
    while (1) {
        // Handle BLE events
        synapse_ble_gatt_process_events();
        
        // Run acquisition FSM (T0/T1/T2)
        synapse_acquisition_fsm_tick();
        
        // If recording, process sensors
        if (g_session && synapse_session_is_active(g_session)) {
            synapse_sample_t sample = synapse_acquisition_read_all();
            
            // Edge AI inference
            synapse_stress_level_t stress = synapse_edge_ai_infer_stress(&sample);
            synapse_signal_quality_t sq = synapse_signal_quality_compute(&sample);
            
            // Call callbacks
            synapse_session_push_sample(g_session, &sample);
            
            // Write to FIT (circular buffer on LittleFS)
            synapse_fit_write_sample(g_session, &sample, stress, sq);
        }
        
        // Handle provisioning if in progress
        synapse_provisioning_process();
        
        // Handle OTA if in progress
        synapse_ota_process();
        
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}
```

---

## 🦀 Rust SDK — `synapse-device` (crates.io)

### Cargo.toml
```toml
[package]
name = "synapse-device"
version = "0.1.0"
edition = "2021"
description = "Synapse Device SDK for Rust"
license = "MIT"
repository = "https://github.com/AlessioBrillo/SYNAPSE-24"

[features]
default = ["ble", "serial", "tokio"]
ble = ["btleplug"]
serial = ["tokio-serial"]
tokio = ["tokio", "tokio-util"]
mqtt = ["rumqttc"]
lsl = ["lsl-rs"]
cli = ["clap", "clap_complete"]

[dependencies]
uuid = { version = "1.0", features = ["serde", "v4"] }
serde = { version = "1.0", features = ["derive"] }
serde_json = "1.0"
thiserror = "1.0"
bytes = "1.0"
# ... feature-specific deps
```

### API Rust (Async/Await)
```rust
// src/lib.rs
use synapse_device::{SynapseDevice, Transport, SessionConfig, SportType, ExportFormat};
use std::time::Duration;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Discovery
    let devices = SynapseDevice::discover(Transport::Ble, Duration::from_secs(10)).await?;
    println!("Found {} devices", devices.len());
    
    for dev in &devices {
        println!("  {} ({}) FW:{} Batt:{}%", dev.serial, dev.transport, dev.firmware_version, dev.battery_level);
    }
    
    // Connect
    let device = SynapseDevice::connect(devices[0].serial.clone(), Transport::Ble).await?;
    
    // Provisioning (if needed)
    if !device.is_provisioned().await? {
        device.provision(ProvisioningConfig {
            wifi_ssid: "MyWiFi".into(),
            wifi_password: "secret".into(),
            cloud_url: "https://synapse.mydomain.com".into(),
            auto_generate_cert: true,
        }).await?;
    }
    
    // Record session
    let session = device.record_session(SessionConfig {
        sport_type: SportType::Running,
        duration: Some(Duration::from_secs(3600)),
        enable_gps: true,
        ecg_sample_rate: 500,
        ppg_sample_rate: 64,
        imu_sample_rate: 100,
        auto_pause: true,
    }).await?;
    
    // Real-time callbacks
    let mut stream = session.sample_stream();
    while let Some(sample) = stream.next().await {
        println!("HR: {:?}, Stress: {:?}", sample.heart_rate, sample.stress_level);
        if let Some(gps) = sample.gps {
            println!("GPS: {:.6}, {:.6} @ {:.1} m/s", gps.lat, gps.lon, gps.speed);
        }
    }
    
    // Export
    session.export(ExportFormat::Fit, "my_run.fit").await?;
    session.export(ExportFormat::Xdf, "my_run.xdf").await?;
    session.export(ExportFormat::Csv, "my_run.csv").await?;
    
    // OTA
    if let Some(update) = device.check_firmware_update().await? {
        println!("Update available: {}", update.version);
        device.ota_update(&update).await?;
    }
    
    Ok(())
}
```

---

## 📡 Protocolli di Trasporto Supportati

| Protocollo | Python SDK | C SDK | Rust SDK | Use Case |
|------------|------------|-------|----------|----------|
| **BLE GATT** | ✅ `btleplug` | ✅ `nimble`/`bluedroid` | ✅ `btleplug` | Mobile apps, gateways, direct phone |
| **Serial/USB** | ✅ `pyserial` | ✅ `termios`/`FreeRTOS` | ✅ `tokio-serial` | Wired, Linux SBC, CI/CD |
| **MQTT** | ✅ `paho-mqtt` | ✅ `lwmqtt`/`esp-mqtt` | ✅ `rumqttc` | Cloud ingestion, multi-device |
| **HTTP/HTTPS** | ✅ `httpx` | ✅ `esp-http-client` | ✅ `reqwest` | Cloud ingestion, REST API |
| **LSL** | ✅ `pylsl` | ❌ (planned) | ❌ (planned) | Research, lab sync, multi-stream |
| **TCP Raw** | ✅ `asyncio` | ✅ `lwIP` | ✅ `tokio` | Custom gateways, high-throughput |

---

## 🔐 Sicurezza & Provisioning

### Device Certificate (mTLS)
```bash
# Ogni dispositivo ha un certificato X.509 unico
# Generato in factory test, private key mai lascia il device

# Python SDK genera CSR + richiede firma a cloud (o CA locale)
synapse provision SYN-BAND-A1B2 --generate-cert --cloud-url https://synapse.mydomain.com

# Cloud risponde con cert firmato + CA chain
# Device verifica catena di fiducia prima di connettersi MQTT/HTTPS
```

### OTA Security
```bash
# Firmware firmato con Ed25519
# Private key: offline HSM / air-gapped
# Public key: embedded in firmware bootloader

# Verifica firma prima di applicare
synapse ota SYN-BAND-A1B2 --firmware firmware.bin --verify-signature

# Rollback automatico se 3 boot falliti consecutivi
```

---

## 🧪 Testing & CI

### Test Matrix
| Platform | Python | C/C++ | Rust |
|----------|--------|-------|------|
| Linux x86_64 | ✅ CI | ✅ CI | ✅ CI |
| Linux ARM64 (RPi) | ✅ CI | ✅ CI | ✅ CI |
| macOS | ✅ CI | ✅ CI | ✅ CI |
| Windows | ✅ CI | ✅ CI | ✅ CI |
| ESP32 (ESP-IDF) | ❌ | ✅ CI | ❌ |
| Zephyr RTOS | ❌ | ✅ CI | ❌ |

### Test Suite
```bash
# Python
pytest tests/ -v --cov=synapse_device
# - test_discovery_ble.py
# - test_provisioning_flow.py
# - test_recording_fit_xdf.py
# - test_streaming_mqtt_lsl.py
# - test_ota_verification.py
# - test_calibration.py

# C/C++ (Unity + CMock)
cd sdk/cpp && cmake -B build -DSYNAPSE_TEST=ON && cmake --build build && ctest

# Rust
cargo test --all-features
cargo test --features "ble,mqtt,lsl,cli"
```

---

## 📚 Documentazione per Developer Terzi

### `docs/device-integration-guide.md` (Contenuto Chiave)

```markdown
# Synapse Device Integration Guide

## Quick Start: Read HR from Synapse Band in 50 lines

```python
from synapse_device import SynapseDevice, discover_devices

devices = discover_devices(ble=True)
device = SynapseDevice(devices[0].serial)
device.connect()

# Enable HR notifications (standard GATT)
device.enable_heart_rate_notifications()

def on_hr(hr, rr_intervals):
    print(f"Heart Rate: {hr} bpm")
    if rr_intervals:
        print(f"  RR intervals: {rr_intervals[:5]}...")

device.set_heart_rate_callback(on_hr)

# Run for 60 seconds
import time
time.sleep(60)
device.disconnect()
```

## BLE GATT Reference

### Standard Services (Compatible con qualsiasi app BLE)
| Service | UUID | Characteristics |
|---------|------|-----------------|
| Heart Rate | 0x180D | HR Measurement (0x2A37), Body Sensor Location (0x2A38) |
| Running Speed & Cadence | 0x1814 | RSC Measurement (0x2A53), SC Control Point (0x2A55) |
| Cycling Speed & Cadence | 0x1816 | CSC Measurement (0x2A5B), SC Control Point (0x2A55) |
| Battery | 0x180F | Battery Level (0x2A19) |
| Device Information | 0x180A | Manufacturer, Model, Serial, FW/HW Rev |

### Synapse Custom Service
**Service UUID:** `53594E41-5053-452D-4241-4E44-000000000000` (ASCII: "SYNAPSE-BAND")

| Characteristic | UUID | Properties | Format |
|----------------|------|------------|--------|
| ECG Raw Stream | `...0001` | Notify | `int16[500]` @ 500Hz (1 sec batches) |
| PPG Raw Stream | `...0002` | Notify | `uint16[128]` @ 64Hz (dual wavelength) |
| IMU Data | `...0003` | Notify | `int16[9]` (acc_x,y,z, gyro_x,y,z, mag_x,y,z) |
| Signal Quality | `...0004` | Notify | `struct {uint8 ecg_sqi, ppg_sqi, ppg_map, ppg_pi, ecg_conf}` |
| Stress Triage | `...0005` | Notify | `uint8` (0=baseline, 1=stress, 2=artifact) |
| Device Config | `...0006` | Read/Write | Protobuf `DeviceConfig` |
| Provisioning Control | `...0007` | Write/Notify | Protobuf `ProvisioningCommand` |

## FIT Developer Data Fields

Quando esporti `.fit`, i campi custom Synapse sono in `developer_data_id` / `field_description`:

| Field Name | Type | Scale | Offset | Units | Description |
|------------|------|-------|--------|-------|-------------|
| `synapse_stress` | uint8 | 1 | 0 | enum | 0=baseline, 1=stress, 2=artifact |
| `synapse_sqi` | uint8 | 1/255 | 0 | ratio | PPG Signal Quality Index |
| `synapse_map` | uint8 | 1/255 | 0 | ratio | Motion Artifact Probability |
| `synapse_ecg_quality` | uint8 | 1/255 | 0 | ratio | ECG quality confidence |
| `synapse_sleep_stage` | uint8 | 1 | 0 | enum | 0=W, 1=N1, 2=N2, 3=N3, 4=REM |
| `synapse_rmssd` | uint16 | 1 | 0 | ms | RMSSD (5-min window) |
| `synapse_sdnn` | uint16 | 1 | 0 | ms | SDNN (5-min window) |
| `synapse_lf_hf` | uint16 | 1/100 | 0 | ratio | LF/HF ratio ×100 |

## MQTT Topic Structure

```
synapse/devices/{device_id}/telemetry          # Device → Cloud (MsgPack)
synapse/devices/{device_id}/status             # Device → Cloud (battery, wifi, etc.)
synapse/devices/{device_id}/events             # Device → Cloud (session_start, session_end, error)
synapse/cloud/{device_id}/commands             # Cloud → Device (config, ota, calibrate)
synapse/cloud/{device_id}/firmware             # Cloud → Device (OTA chunks)
```

## Error Handling

Tutti gli SDK usano error types strutturati:

```python
# Python
from synapse_device import SynapseError, SynapseErrorCode

try:
    device.connect()
except SynapseError as e:
    if e.code == SynapseErrorCode.DEVICE_NOT_FOUND:
        print("Device not in range")
    elif e.code == SynapseErrorCode.PROVISIONING_REQUIRED:
        print("Device needs provisioning first")
    elif e.code == SynapseErrorCode.FIRMWARE_TOO_OLD:
        print(f"Min firmware: {e.details['min_version']}, current: {e.details['current_version']}")
```

```c
// C
typedef enum {
    SYNAPSE_OK = 0,
    SYNAPSE_ERR_NOT_FOUND = -1,
    SYNAPSE_ERR_CONNECTION_FAILED = -2,
    SYNAPSE_ERR_PROVISIONING_REQUIRED = -3,
    SYNAPSE_ERR_FIRMWARE_TOO_OLD = -4,
    SYNAPSE_ERR_OTA_SIGNATURE_INVALID = -5,
    SYNAPSE_ERR_OTA_ROLLBACK = -6,
    SYNAPSE_ERR_CALIBRATION_FAILED = -7,
} synapse_error_t;
```

---

## 🤝 Contribuire all'SDK

1. **Protocollo stabile**: BLE GATT UUIDs, FIT fields, MQTT topics sono **versionati** (v1, v2...)
2. **Backward compatibility**: Mai rompere campi esistenti. Aggiungi nuovi con suffisso `_v2`
3. **Testing**: Ogni PR deve includere test su device reale (ESP32-S3 DevKit)
4. **Documentazione**: Aggiorna `device-integration-guide.md` per ogni nuova feature

---

## 📋 Versioning Policy

| Componente | Versioning | Compatibilità |
|------------|------------|---------------|
| **BLE GATT** | Service UUID v1, v2... | v1 clients leggono v2 (nuove char opzionali) |
| **FIT Profile** | Developer Data v1, v2... | v1 readers ignorano campi sconosciuti |
| **MQTT Schema** | Topic v1, v2... | Cloud supporta v1+v2 simultaneamente |
| **Python SDK** | SemVer (MAJOR.MINOR.PATCH) | MAJOR = breaking; MINOR = nuove feature; PATCH = fix |
| **C SDK** | Header version `SYNAPSE_SDK_VERSION` | Stesso principio |
| **Rust SDK** | SemVer (Cargo) | Standard Rust |

---

*Specifica SDK viva. Ogni nuova feature device richiede aggiornamento SDK + doc + test.*