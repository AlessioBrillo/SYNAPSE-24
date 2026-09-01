---
name: synapse-architecture-guardian
description: Corporate architectural governance for SYNAPSE-24. Enforces 8 foundational principles, tiered acquisition, hardware abstraction, LSL synchronization, energy budgets, signal quality, and phased delivery.
when_to_use: Any implementation, code generation, database schema, API contract, service boundary, technology selection, provider integration, deployment, observability, security, or documentation decision for SYNAPSE-24.
user-invocable: false
---

# SYNAPSE Architecture Governance Framework

Binding engineering standards for the SYNAPSE-24 24/7 multimodal bio-sensing wearable platform. Every implementation MUST conform to these rules derived from Architecture.md and Roadmap.md.

---

## 1. Foundational Architecture Principles

### Principle 1: Hardware Abstraction Layer (BrainFlow)
- **Rule**: NEVER import, call, or reference any hardware SDK/API directly from signal processing, fusion, or application logic.
- **Enforcement**: All hardware interactions go through `synapse24.hardware.BoardManager`. Adding a new sensor pod means writing exactly one `BoardConfig` adapter. Zero changes in ingestion, quality, or fusion layers.
- **Violation**: Calling `brainflow.BoardShim()` directly in `ingestion/wesad.py`.
- **Compliant**: `board_manager = BoardManager(config); data = board_manager.get_data()`

### Principle 2: Tiered State Management
- **Rule**: Every sample MUST carry its acquisition tier (0/1/2) metadata. Tier transitions are explicit, logged events.
- **Enforcement**: `SignalQualityMetrics` includes `tier: Tier`. LSL stream `desc/acquisition/tier` populated. `AcquisitionController` emits `TierTransitionEvent` on every change.
- **No implicit tier assumptions**: Code cannot assume "EEG exists" without checking `tier >= 1`.

### Principle 3: LSL as Synchronization Backbone
- **Rule**: ALL timestamps derive from `pylsl.local_clock()`. No `time.time()`, `time.perf_counter()`, `datetime.now()` in signal paths.
- **Enforcement**: `LSLStreamManager` and `BrainFlowToLSL` are the ONLY timestamp sources. XDF writer validates monotonic timestamps per stream.
- **Clock drift**: Quantified via periodic sync markers; post-hoc correction in XDF.

### Principle 4: Idempotent Sample Processing
- **Rule**: Every sample processing function is pure: same input → same output. No internal state.
- **Enforcement**: `compute_ecg_quality()`, `compute_ppg_quality()`, `compute_eeg_quality()` are stateless. `AcquisitionController` handles buffering, not processing functions.

### Principle 5: Orchestration (Hub) Separated from Execution (Pods)
- **Rule**: Hub decides WHAT/WHEN (tier transitions, fusion scheduling). Pods execute acquisition only.
- **Enforcement**: `AcquisitionController` (hub) sends commands via LSL Marker stream. Pods (`BoardManager`) only acquire and stream. No business logic in pod firmware.

### Principle 6: Defense in Depth — Signal Validation
- **Rule**: Every sample validated at ingestion. Invalid samples flagged, not crashed.
- **Enforcement**: 
  - Input range checks (ECG: ±5mV, PPG: 0-65535, EEG: ±500µV)
  - NaN/Inf detection and interpolation
  - Quality metrics computed BEFORE fusion
  - Agent/ML outputs validated against JSON Schema

### Principle 7: Observability = LSL + Structured Logs
- **Rule**: Every service emits LSL streams + structured JSON logs with mandatory fields.
- **Enforcement**:
  - Logs: `{"timestamp", "level", "service", "operation", "correlation_id", "duration_ms", "message", "context"}`
  - LSL: All streams have `source_id`, `tier`, `quality_metadata`
  - Correlation ID propagated from acquisition → processing → fusion → storage

### Principle 8: Energy Budget as Functional Requirement
- **Rule**: µJ/sample tracked for every modality. Tier 1 promotion requires budget check.
- **Enforcement**: `PowerBudgetManager` consulted before every Tier 0→1 transition. Actual consumption logged per session. Hard limit: 24h on 3000mAh hub battery.

---

## 2. Logical Layers (Adapted from Ergodix)

```
Layer 1: Sensor Pods (Execution)
  - BoardManager, firmware, BLE/Serial transport
  - ONLY: Acquire → LSL Stream
  
Layer 2: Hub Orchestration (Control)
  - AcquisitionController, TierStateMachine, PowerBudgetManager
  - Decides: Tier transitions, fusion scheduling, calibration
  
Layer 3: Signal Processing (Transform)
  - ingestion/, signal_quality/, utils/
  - Pure functions: raw → features → quality metrics
  
Layer 4: Fusion & AI (Intelligence)
  - Edge triage (Tier 0), Fusion (Tier 1), Personalization
  - Models: TFLM (pods), ONNX/TF (hub)
  
Layer 5: Storage & Export (Persistence)
  - XDF writer, LSL recorder, dataset management
  - XDF, CSV, NPZ, SQLite for metadata
  
Layer 6: Infrastructure (Platform)
  - CI/CD, GitHub Actions, pre-commit, uv, ruff, mypy
  - GitOps: main branch = deployable state
```

**Dependency Flow**: 1 → 2 → 3 → 4 → 5. Layer 6 is foundational.

---

## 3. Python Coding Conventions

| Element | Convention | Example |
|---------|------------|---------|
| Class | PascalCase | `AcquisitionController`, `QualityThresholds` |
| Function/Variable | snake_case | `compute_ecg_quality()`, `tier_state_machine` |
| Constant | UPPER_SNAKE_CASE | `MAX_TIER1_DURATION_H`, `LSL_CLOCK_DOMAIN` |
| File | snake_case | `acquisition_controller.py` |
| Test file | `test_` prefix | `test_acquisition_controller.py` |
| Private member | `_` prefix | `_budget`, `_validate_timestamps()` |
| Type hints | Required for all public APIs | `def process(data: np.ndarray) -> QualityMetrics:` |
| Dataclasses | `@dataclass(frozen=True)` for value objects | `@dataclass(frozen=True) class TierConfig:` |
| Exceptions | Custom per domain | `ECGQualityError`, `TierTransitionError` |

### Mandatory Patterns
- **No `any` type** — use `Union`, `Protocol`, or generics
- **Async for I/O** — `async def stream_lsl()`, `async def download_dataset()`
- **Context managers for resources** — `with BoardManager(...) as board:`
- **Structured logging** — `logger.info("msg", extra={"correlation_id": cid, "tier": tier})`

---

## 4. Documentation Standards

### Module README (every module directory)
```markdown
# Module Name

## Purpose
One paragraph: what this module does and why it exists.

## Architecture
Internal structure, key classes, data flow diagram.

## Public API
- `function_name(args) -> ReturnType` — Description
- `ClassName` — Description

## Configuration
- Environment variables / config keys with defaults

## Error Codes
- `ERROR_CODE` — Meaning + recovery

## Examples
```python
# Minimal working example
from synapse24.module import main_function
result = main_function(config)
```
```

### Function Docstrings (Google Style)
```python
def promote_to_tier1(self, reason: TierTransitionReason, duration_h: float) -> bool:
    """Promote acquisition from Tier 0 to Tier 1.
    
    Args:
        reason: Trigger for promotion (immobility, night_window, manual).
        duration_h: Expected session duration in hours.
        
    Returns:
        True if promotion succeeded, False if budget insufficient.
        
    Raises:
        TierTransitionError: If already in Tier 1 or invalid reason.
        PowerBudgetExceededError: If hub battery cannot sustain session.
    """
```

---

## 5. Testing Pyramid (Adapted)

| Test Type | Proportion | Scope | Command |
|-----------|-----------|-------|---------|
| Unit | 70% | Single function, synthetic data | `pytest tests/unit -x` |
| Integration | 20% | Multi-module, real datasets | `pytest tests/integration -x` |
| Baseline | 5% | Published benchmark validation | `pytest tests/baseline -x` |
| E2E | 3% | Full pipeline: synthetic → LSL → XDF → quality | `pytest tests/e2e -x` |
| Hardware | 2% | Real board (manual, tagged) | `pytest tests/hardware -x -m hardware` |

### Rules
- **Synthetic data only in unit tests** — fixtures in `tests/fixtures/`
- **Real datasets in integration/baseline** — cached in CI
- **Mock only at boundaries** — `BoardManager`, `LSLStreamManager`, file I/O
- **Property-based testing** — `hypothesis` for signal processing functions
- **Deterministic** — fixed seeds, no time-dependent tests

---

## 6. Error Handling

### Typed Exception Hierarchy
```
SynapseError (base)
  ├── HardwareError
  │   ├── BoardConnectionError
  │   ├── BoardConfigError
  │   └── StreamError
  ├── SignalQualityError
  │   ├── ECGQualityError
  │   ├── PPGQualityError
  │   ├── EEGQualityError
  │   └── FNIRSQualityError
  ├── TierTransitionError
  │   ├── InvalidTransitionError
  │   └── PowerBudgetExceededError
  ├── SyncError
  │   ├── ClockDriftError
  │   └── TimestampError
  └── FusionError
      ├── ModelLoadError
      └── InferenceError
```

### Error Contract
Every exception carries:
- `code`: Machine-readable (e.g., `POWER_BUDGET_EXCEEDED`, `CLOCK_DRIFT_DETECTED`)
- `message`: Human-readable
- `context`: `Dict[str, Any]` — pod_id, tier, timestamp, budget_remaining
- `cause`: Wrapped original exception
- `recovery_hint`: Suggested action

---

## 7. Logging Standards

```json
{
  "timestamp": "2026-09-01T10:30:00.123Z",
  "level": "INFO",
  "service": "acquisition-controller",
  "operation": "promote_to_tier1",
  "correlation_id": "c7a8b9c0-d1e2-4f3a-5b6c-7d8e9f0a1b2c",
  "duration_ms": 12,
  "message": "Tier 0 → Tier 1 promotion successful",
  "context": {
    "pod_id": "head_001",
    "trigger": "immobility",
    "duration_h": 2.5,
    "budget_remaining_mah": 2847,
    "tier": 1
  }
}
```

**Levels**: ERROR (failed, intervention needed), WARN (degraded, recovered), INFO (milestone), DEBUG (diagnostic)

**Rules**: No secrets, no PII, no log-and-throw, correlation_id always present.

---

## 8. Security & Compliance Checklist

- [ ] No hardcoded BLE MACs, WiFi credentials, API keys
- [ ] All device configs loaded from encrypted config / env vars
- [ ] Raw biosignal data never logged (only quality metrics)
- [ ] Subject IDs pseudonymized in XDF metadata
- [ ] BrainFlow board configs validated against allowlist
- [ ] Firmware signatures verified before OTA update
- [ ] HIPAA/GDPR considerations documented (Phase 2+)

---

## 9. Phased Delivery Workflow

### Phase 0: Software Foundation (Current)
- All development on `main` via short-lived feature branches
- CI: lint → typecheck → test → baseline validation
- Merge: squash, linear history

### Phase 1: Hardware Integration
- `hardware/` branch for firmware development
- Hardware-in-loop tests tagged `@pytest.mark.hardware`
- Separate CI runner with physical devices

### Phase 2+: Production
- GitOps via ArgoCD (per Ergodix ADR 0014)
- Canary deployments for firmware
- Rollback = revert commit

---

## References
- Architecture.md (governing)
- Roadmap.md (governing)
- Ergodix architecture-guardian skill (pattern source)
- LSL Paper: Kothe et al., Imaging Neuroscience 2025