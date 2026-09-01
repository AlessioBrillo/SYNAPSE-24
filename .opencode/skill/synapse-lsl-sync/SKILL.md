---
name: synapse-lsl-sync
description: LSL stream creation, clock synchronization, XDF writing/validation, multi-stream alignment for SYNAPSE-24 multimodal biosensing platform.
when_to_use: Working with Lab Streaming Layer (LSL) for biosignal acquisition, synchronization across sensor pods/hubs, XDF file I/O, timestamp management, clock drift correction.
user-invocable: true
---

# SYNAPSE LSL Synchronization Skill

This skill governs all Lab Streaming Layer (LSL) and XDF (Extensible Data Format) operations for the SYNAPSE-24 platform. It ensures millisecond-precision synchronization across decoupled sensor pods (head, in-ear, forearm) and the compute hub.

## Core Principles

### 1. LSL as Synchronization Backbone
Per Architecture.md §29 and Roadmap.md §151: **LSL is the single source of timing truth**. All timestamps derive from `pylsl.local_clock()`. No `time.time()`, `time.perf_counter()`, or system clocks in signal paths.

### 2. Tier-Aware Stream Configuration
Stream metadata must encode the acquisition tier (0/1/2) for downstream quality assessment:
- **Tier 0**: Continuous, low-power (PPG, IMU, temp, 1-2ch EEG in-ear) → `stream_type` suffix `_T0`
- **Tier 1**: High-density during rest/sleep (EEG 6-16ch, fNIRS, ECG) → `stream_type` suffix `_T1`
- **Tier 2**: On-demand bursts (cognitive tests, calibration) → `stream_type` suffix `_T2`

### 3. Clock Drift Management
Architecture.md §92 warns: "architettura a hub separato... introduce un problema di sincronizzazione multi-nodo (clock drift tra pod testa, in-ear, fascia braccio)". 

**Mitigation strategy**:
- All pods stream via LSL with `local_clock()` timestamps
- Hub records all streams simultaneously via single LSL inlet
- Post-hoc alignment using XDF's per-stream timestamps
- Optional: periodic sync markers (LSL `Marker` stream) for drift quantification

### 4. XDF as Archival Format
All processed data exports to XDF 1.0 with:
- Per-stream metadata (type, units, sampling rate, device info, tier)
- Embedded `SignalQualityMetrics` as JSON metadata stream
- Chunked format for memory efficiency

## Implementation Patterns

### StreamInfo Factory
```python
from synapse24.utils import create_stream_info

# Tier 0 continuous PPG from forearm hub
info = create_stream_info(
    name="SYNAPSE_PPG_FOREARM",
    stream_type="PPG_T0",
    channel_count=1,
    sampling_rate=64.0,
    channel_names=["PPG_GREEN"],
    channel_units=["a.u."],
    source_id="synapse24_forearm_hub_001",
    tier=0,
    device="MAX30102",
    placement="forearm",
)

# Tier 1 high-density EEG from head pod
info = create_stream_info(
    name="SYNAPSE_EEG_HEAD",
    stream_type="EEG_T1",
    channel_count=16,
    sampling_rate=500.0,
    channel_names=[f"EEG_{i:02d}" for i in range(16)],
    channel_units=["µV"] * 16,
    source_id="synapse24_head_pod_001",
    tier=1,
    device="ADS1299x2",
    placement="frontal_parietal",
)
```

### Multi-Stream Outlet Manager
```python
from synapse24.utils import LSLStreamManager

with LSLStreamManager() as manager:
    # Add all streams for this acquisition session
    manager.add_stream(ecg_info, ecg_data, ecg_timestamps)
    manager.add_stream(ppg_info, ppg_data, pcam_timestamps)
    manager.add_stream(acc_info, acc_data, acc_timestamps)
    manager.add_stream(marker_info, markers, marker_timestamps)
    
    # Stream all synchronously (single clock domain)
    manager.stream_all()
```

### XDF Writer
```python
from synapse24.utils import write_xdf, SignalQualityMetrics

# Write multi-stream XDF with embedded quality metrics
write_xdf(
    output_path=Path("data/processed/S001_session.xdf"),
    streams=streams,  # List of dict: {info, data, timestamps}
    metadata={
        "session_id": "S001",
        "acquisition_tier": 1,
        "quality_metrics": quality_metrics.to_dict(),
        "sync_method": "LSL_local_clock",
    },
)
```

## Quality Gates

Before any PR touching LSL/XDF code:
- [ ] `test_lsl_xdf_roundtrip()` passes (zero dropped samples, monotonic timestamps)
- [ ] Multi-stream sync test: 3+ streams align within 1ms
- [ ] XDF validation: `pyxdf.load_xdf()` reads all streams without error
- [ ] Timestamp domain check: all timestamps from `pylsl.local_clock()`
- [ ] Tier metadata present in all stream `desc/acquisition/tier`

## References
- LSL Paper: Kothe et al., "The Lab Streaming Layer for Synchronized Multimodal Recording", Imaging Neuroscience 2025, DOI 10.1162/IMAG.a.136
- XDF Spec: https://github.com/xdf-modules/xdf
- pylsl docs: https://github.com/labstreaminglayer/liblsl-Python
- Architecture.md §29, §92; Roadmap.md §16, §151