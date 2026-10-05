# Medical Software Compliance & Traceability Framework (IEC 62304 Aligned)

SYNAPSE-24 is engineered following professional medical software standards (inspired by **IEC 62304** for medical device software lifecycle and **ISO 14971** for risk management) to ensure reliability in continuous health monitoring, biosignal acquisition, and edge AI classification.

## 1. Software Safety Classification
In accordance with IEC 62304 guidelines:
- **Classification:** **Class B** (Non-critical diagnostic support / continuous physiological monitoring where faults could result in minor injury or delayed clinical intervention).
- **Risk Mitigation:** Redundant signal quality checks (SQI), automated fallback states in firmware, and rigorous test coverage ($\ge80\%$ unit test coverage, automated validation against benchmark datasets like WESAD and MIT-BIH).

## 2. Requirements Traceability Matrix (RTM)
To ensure complete verification and validation (V&V), every requirement maps directly to test suites:
- **Clock Synchronization:** `src/synapse24/acquisition/clock_sync.py` $\leftrightarrow$ `tests/` & `scripts/validate_sync.py`
- **Signal Quality Assurance:** `src/synapse24/signal_quality/` $\leftrightarrow$ `scripts/validate_baseline.py`
- **Edge AI Triage Model:** `src/synapse24/edge_ai/` $\leftrightarrow$ `scripts/train_triage.py` & quantization closure tests.

## 3. Anomaly & Incident Logging
Any anomalous sensor behavior, clock drift exceeding $\pm5\text{ms}$, or signal dropouts are logged with timestamps and metadata for auditability.
