# Regulatory Position and Compliance Gaps

**SYNAPSE-24 is a research and experimental platform. It is not a medical device, has no regulatory clearance or CE marking, and must not be used for diagnosis, treatment or clinical decisions.** The same disclaimer appears in [Roadmap.md](Roadmap.md). This document records what would be needed to change that, so that commercial plans start from facts. It is not legal advice; engage regulatory counsel before placing anything on the EU market.

## Intended purpose

Acquisition, signal-quality scoring and on-device triage of physiological signals for research and wellness experimentation. Claims about diagnosis, monitoring of disease or clinical decision support are **not** supported by this project.

## Engineering evidence that exists today

| Evidence | Where |
|----------|-------|
| Automated tests with an 80% line+branch coverage gate | `tests/`, `.github/workflows/ci.yml` |
| Lint, format and strict type checks in CI | `.github/workflows/ci.yml` |
| Validation against public benchmarks (WESAD, MIT-BIH, Sleep-EDF) | `scripts/validate_baseline.py`, `tests/test_*_closure*.py` |
| Clock-sync and tier-promotion validation | `scripts/validate_sync.py`, `tests/test_clock_sync.py`, `tests/test_tier_promotion_cycle.py` |
| Reproducible dependency lock | `uv.lock` |
| Software bill of materials | [docs/sbom.spdx.json](docs/sbom.spdx.json) |

This is good engineering practice. It is **not** a regulated quality system: there is no documented requirements-to-test traceability matrix, no formal risk file and no controlled release process.

## Gaps by regime (EU market)

| Regime | Applies when | Status |
|--------|--------------|--------|
| **MDR (EU) 2017/745** and **IEC 62304 / ISO 14971 / ISO 13485 / IEC 62366** | Product is marketed with a medical purpose (software and wearable). Software safety class and device class must be derived from a risk analysis, not assumed | Not started: no intended-use statement for medical purpose, risk file, QMS, clinical evaluation or CE marking |
| **GDPR** | Any processing of personal data, health data included | See [PRIVACY.md](PRIVACY.md); technical controls and DPIA are deployer work and mostly missing |
| **Radio Equipment Directive 2014/53/EU**, EMC, LVD, RoHS, WEEE, Batteries Reg. (EU) 2023/1542 | Selling the physical wearable (BLE/Wi-Fi radio, battery) | Not started; applies to hardware, outside this repository |
| **Cyber Resilience Act (EU) 2024/2847** | Products with digital elements made available commercially; open-source software supplied in a commercial activity is in scope. Reporting duties start Sept 2026, main obligations Dec 2027 | Not started; [SECURITY.md](SECURITY.md) is a first step; SBOM exists; no vulnerability-handling process or secure-development evidence yet |
| **EU AI Act (EU) 2024/1689** | AI features placed on the market; stress/emotion inference has use-case restrictions and medical-device AI is high-risk | Not assessed |

## Recommended next steps

1. Decide with counsel whether the first product is positioned as **wellness/research** (no medical claims) or **medical**; every other choice follows from that.
2. If medical: appoint regulatory lead, define intended use, open a risk-management file (ISO 14971), and establish a QMS before further design freeze.
3. In either case: run a CRA gap analysis, add a CI-generated SBOM and dependency/licence scan, and close the technical gaps listed in [PRIVACY.md](PRIVACY.md).
