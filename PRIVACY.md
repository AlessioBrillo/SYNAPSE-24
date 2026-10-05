# Privacy & Biosignal Data Compliance (GDPR)

SYNAPSE-24 handles continuous multimodal biosignal data (PPG, ECG, EEG, electrodermal activity, movement telemetry, and sleep states). Because biomedical data is classified as **Special Category Data** under Article 9 of the General Data Protection Regulation (GDPR), strict privacy and data minimization principles govern this platform.

## 1. Data Minimization & Local-First Processing
- **Edge Processing:** Feature extraction, SQI (Signal Quality Index), and real-time triage classifiers run locally on the ESP32 firmware and local Python acquisition nodes.
- **Data Minimization:** Raw high-frequency continuous signals are filtered, anonymized, and segmented locally. Only necessary derived metrics or encrypted streams are transmitted or logged.

## 2. Pseudonymization and Encryption
- **Subject IDs:** All data streams in Lab Streaming Layer (LSL) and XDF recordings must use cryptographic pseudonyms or random subject identifiers rather than Personally Identifiable Information (PII).
- **In-Transit & At-Rest:** Telemetry streams and exported XDF datasets must be encrypted when stored or transmitted over enterprise networks.

## 3. User Consent & Rights (GDPR Articles 6 & 9)
- Deployments of SYNAPSE-24 in research or clinical trials require explicit, informed, and revocable consent from data subjects.
- **Right to Erasure (Art. 17):** Raw biosignal recordings and derived features must be fully purgable upon request by the data controller or subject.

## 4. Contact
For data protection inquiries or Data Protection Officer (DPO) contact: privacy@synapse24.io
