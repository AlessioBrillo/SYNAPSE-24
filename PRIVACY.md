# Privacy and Biosignal Data

SYNAPSE-24 is software and firmware for acquiring continuous physiological signals (PPG, ECG, EEG, EDA, motion, sleep-related features). Data that relates to an identifiable person and reveals health or physiological state is **health data, a special category under GDPR Art. 9**. This document states what this repository does and does not do about that. It is not legal advice and not a certification.

## Roles

This project operates no service and receives no data from you. Whoever deploys SYNAPSE-24 (a lab, a company, a product maker) is the **data controller** for the data they collect, and is responsible for GDPR compliance of their deployment. The maintainer is not a controller or processor of your data.

## Current implementation status

| Capability | Status in this repository |
|------------|---------------------------|
| On-device processing / edge triage (data minimisation by design) | Implemented in firmware tiers and described in [Architecture.md](Architecture.md); not independently audited |
| Encryption of recorded data at rest (XDF files) | **Not implemented** (plain files) |
| Encryption of LSL streams on the network | **Not implemented** (LSL is unencrypted) |
| Pseudonymisation of subject identifiers | **Not implemented** in `src/synapse24`; subject IDs are whatever the operator supplies |
| Consent capture, withdrawal, data-subject export/erasure tooling | **Not implemented** |
| BLE bonding and Wi-Fi/cloud provisioning (CSR flow) in `firmware/esp32_band` | Present; **not security-reviewed** |
| Cloud ingestion | Not part of this repository's released scope; only design documents exist |

Do not describe a deployment as "GDPR compliant" on the basis of this repository.

## What a deployer must do

- Establish a lawful basis (Art. 6) **and** an Art. 9(2) condition (typically explicit consent or a research/medical basis under applicable law).
- Perform a Data Protection Impact Assessment (Art. 35) — continuous monitoring of health data will normally require one.
- Provide transparency notices, retention limits, and procedures for access, rectification and erasure (Arts. 12–17).
- Protect data in transit and at rest (Art. 32) — add what the table above marks as missing, or place the system inside controls that provide it.
- Keep records of processing (Art. 30), contract processors (Art. 28), and cover international transfers (Chapter V).
- Check whether inferring emotional or stress states is restricted in your context (e.g. under the EU AI Act) before using the triage models on people.

## Public datasets

Ingestion code downloads third-party research datasets (WESAD, MIT-BIH, Sleep-EDF, DEAP, …). They carry their own terms and consent conditions; see [THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md#datasets). Do not commit raw datasets to this repository.

## Questions

Open a GitHub issue **without personal or health data**. For vulnerabilities use [SECURITY.md](SECURITY.md).
