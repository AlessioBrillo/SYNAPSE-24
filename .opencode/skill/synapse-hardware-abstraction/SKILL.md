---
name: synapse-hardware-abstraction
description: BrainFlow board abstraction, synthetic board for CI, device config management for SYNAPSE-24 sensor pods and hub.
when_to_use: Adding new hardware boards, configuring BrainFlow for different devices, writing board-agnostic acquisition code, CI testing with synthetic data.
user-invocable: true
---

# SYNAPSE Hardware Abstraction Skill

This skill provides a unified interface for all biosignal acquisition hardware via BrainFlow, enabling board-agnostic code and CI testing with synthetic data.

## Board Abstraction Layer

### Supported Boards (Phase 0-2)

| Board | BrainFlow ID | Modalities | Channels | Sample Rates | Phase |
|-------|--------------|------------|----------|--------------|-------|
| **Synthetic** | `SYNTHETIC_BOARD` | All (simulated) | Configurable | Configurable | 0 (CI) |
| **Playback** | `PLAYBACK_FILE_BOARD` | All (from file) | From file | From file | 0 (CI) |
| **EmotiBit** | `EMOTIBIT_BOARD` | PPG(3λ), EDA, IMU(9), Temp | 1+3+1 | 15/4/25/1 Hz | 1 (Ref) |
| **Muse S** | `MUSE_S_BOARD` | EEG(4-5), PPG, IMU(9) | 4-5+1+3 | 256/64/52 Hz | 1 (Tier 0) |
| **Cerelog ESP-EEG** | `CERELOG_BOARD` | EEG(8), IMU(9) | 8+3 | 250/100 Hz | 2 (EEG) |
| **PiEEG** | `PIEEG_BOARD` | EEG(8/16), IMU(9) | 8/16+3 | 250-16k/100 Hz | 2 (EEG) |
| **OpenBCI Ganglion** | `GANGLION_BOARD` | EEG(4), IMU(9) | 4+3 | 200/100 Hz | 2 (EEG) |
| **OpenBCI Cyton** | `CYTON_BOARD` | EEG(8/16), IMU(9) | 8/16+3 | 250-16k/100 Hz | 2 (EEG) |
| **DIY ESP32+ADS1299** | `CUSTOM_BOARD` | Configurable | Configurable | Configurable | 2-3 |

### Unified Board Interface

```python
from synapse24.hardware import BoardManager, BoardConfig

# Configuration-driven board selection
config = BoardConfig(
    board_id="EMOTIBIT_BOARD",  # or "SYNTHETIC_BOARD" for CI
    serial_port="/dev/ttyUSB0",  # or None for BLE/WiFi
    mac_address="00:11:22:33:44:55",  # for BLE boards
    sampling_rate=100,  # override if needed
    channels=[0, 1, 2],  # select subset
)

manager = BoardManager(config)

# Board-agnostic acquisition
with manager.session() as board:
    board.prepare_session()
    board.start_stream()
    
    while running:
        data = board.get_board_data()  # Returns (n_channels, n_samples)
        timestamps = board.get_board_timestamp()  # LSL-compatible
        
        # Process regardless of board type
        process_data(data, timestamps)
    
    board.stop_stream()
    board.release_session()
```

### Device Configuration Management

```python
from synapse24.hardware import DeviceRegistry, SensorPodConfig

# Registry of all known sensor pods
registry = DeviceRegistry()

# Head pod: EEG + fNIRS
registry.register(
    SensorPodConfig(
        pod_id="head_001",
        name="SYNAPSE Head Pod v1",
        board_type="CERELOG_BOARD",
        modalities=["EEG", "fNIRS"],
        tier=1,
        channels=16,
        sampling_rate=500,
        ble_address="AA:BB:CC:DD:EE:FF",
        placement="frontal_parietal",
        electrode_type="dry_gold_pin",
    )
)

# In-ear pod: Ultra-low-power EEG
registry.register(
    SensorPodConfig(
        pod_id="in_ear_001",
        name="SYNAPSE In-Ear Pod v1",
        board_type="CUSTOM_BOARD",  # Custom ESP32+AFE
        modalities=["EEG_T0"],
        tier=0,
        channels=2,
        sampling_rate=250,
        ble_address="11:22:33:44:55:66",
        placement="in_ear",
        electrode_type="dry_gold_pin",
    )
)

# Forearm hub: PPG + ECG + IMU + Temp + Battery
registry.register(
    SensorPodConfig(
        pod_id="forearm_001",
        name="SYNAPSE Forearm Hub v1",
        board_type="EMOTIBIT_BOARD",  # Or custom MAX86141+AD8232+ICM20948
        modalities=["PPG", "ECG", "IMU", "Temp"],
        tier=0,
        channels={"ppg": 3, "ecg": 1, "imu": 9, "temp": 1},
        sampling_rates={"ppg": 64, "ecg": 500, "imu": 100, "temp": 1},
        ble_address="AA:BB:CC:DD:EE:00",
        placement="forearm",
        is_hub=True,  # Has battery, computes, aggregates
    )
)

# Get configuration for acquisition
pod_config = registry.get("head_001")
board_config = pod_config.to_board_config()
```

### Synthetic Board for CI

```python
from synapse24.hardware import SyntheticBoardConfig

# Configure synthetic data matching real board characteristics
synthetic_config = SyntheticBoardConfig(
    board_id="SYNTHETIC_BOARD",
    n_channels=16,
    sampling_rate=500,
    signal_types={
        0: "ecg",  # Channel 0: synthetic ECG
        1: "ppg",  # Channel 1: synthetic PPG
        2: "eeg",  # Channel 2: synthetic EEG (alpha rhythm)
        3: "eeg",  # Channel 3: synthetic EEG
        # ... rest noise
    },
    noise_level=0.1,
    artifact_probability=0.05,
    artifact_types=["motion", "disconnect", "baseline_wander"],
)

# Use in CI for end-to-end testing without hardware
manager = BoardManager(synthetic_config.to_board_config())
```

## BrainFlow Integration Patterns

### Stream to LSL
```python
from synapse24.hardware import BrainFlowToLSL

bridge = BrainFlowToLSL(
    board_manager=manager,
    stream_config={
        "ECG": {"channels": [0], "type": "ECG", "unit": "µV"},
        "PPG": {"channels": [1], "type": "PPG", "unit": "a.u."},
        "EEG": {"channels": list(range(2, 10)), "type": "EEG", "unit": "µV"},
        "ACC": {"channels": [-3, -2, -1], "type": "ACC", "unit": "g"},
    },
)

bridge.start()  # Creates LSL outlets, pushes data in real-time
```

### Multi-Board Synchronization
```python
from synapse24.hardware import MultiBoardCoordinator

coordinator = MultiBoardCoordinator(
    boards={
        "head": BoardManager(head_config),
        "in_ear": BoardManager(in_ear_config),
        "forearm": BoardManager(forearm_config),
    },
    sync_method="lsl_clock",  # All use pylsl.local_clock()
    master="forearm",  # Hub is clock master
)

coordinator.start_all()
# All boards stream to LSL with synchronized timestamps
```

## Quality Gates

- [ ] All board interactions via `BoardManager` — no direct BrainFlow calls in pipelines
- [ ] Synthetic board produces realistic signals (ECG R-peaks, EEG alpha, PPG pulses)
- [ ] CI uses `SYNTHETIC_BOARD` or `PLAYBACK_FILE_BOARD` — zero hardware dependency
- [ ] Device registry loaded from YAML config (not hardcoded)
- [ ] BLE/WiFi/Serial transport abstracted behind `BoardManager`
- [ ] LSL timestamps from `pylsl.local_clock()` only
- [ ] Board-specific quirks handled in adapter, not in pipeline code

## Configuration File: `config/hardware.yaml`

```yaml
boards:
  synthetic:
    board_id: "SYNTHETIC_BOARD"
    n_channels: 16
    sampling_rate: 500
    signal_types: {0: "ecg", 1: "ppg", 2: "eeg", 3: "eeg"}

pods:
  head_001:
    pod_id: "head_001"
    name: "SYNAPSE Head Pod v1"
    board_type: "CERELOG_BOARD"
    modalities: ["EEG", "fNIRS"]
    tier: 1
    channels: 16
    sampling_rate: 500
    ble_address: "AA:BB:CC:DD:EE:FF"
    placement: "frontal_parietal"
    electrode_type: "dry_gold_pin"
  
  in_ear_001:
    pod_id: "in_ear_001"
    name: "SYNAPSE In-Ear Pod v1"
    board_type: "CUSTOM_BOARD"
    modalities: ["EEG_T0"]
    tier: 0
    channels: 2
    sampling_rate: 250
    ble_address: "11:22:33:44:55:66"
    placement: "in_ear"
    electrode_type: "dry_gold_pin"

  forearm_001:
    pod_id: "forearm_001"
    name: "SYNAPSE Forearm Hub v1"
    board_type: "EMOTIBIT_BOARD"
    modalities: ["PPG", "ECG", "IMU", "Temp"]
    tier: 0
    channels: {ppg: 3, ecg: 1, imu: 9, temp: 1}
    sampling_rates: {ppg: 64, ecg: 500, imu: 100, temp: 1}
    ble_address: "AA:BB:CC:DD:EE:00"
    placement: "forearm"
    is_hub: true
```

## References
- BrainFlow: https://brainflow.readthedocs.io/
- Board IDs: https://brainflow.readthedocs.io/en/stable/SupportedBoards.html
- Architecture.md §27-30 (sensor pods), §67-70 (max config)
- Roadmap.md §52-57 (EEG options), §63-66 (MCU), §148-149 (Cerelog/PiEEG)