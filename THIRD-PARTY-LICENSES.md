# Third-Party Licenses

SYNAPSE-24 itself is [dual-licensed](LICENSING.md). The components below keep their own licences. Versions are those pinned in `uv.lock`; licences are the ones declared in each package's published metadata. Licence texts ship with each package. Direct dependencies only; transitive dependencies are enumerated in `uv.lock`.

This file is informational and is not legal advice. Re-check it when `uv.lock` changes.

## Python runtime dependencies

| Package | Locked version | Licence |
|---------|----------------|---------|
| `numpy` | 1.26.4 | BSD-3-Clause |
| `scipy` | 1.17.1 | BSD-3-Clause |
| `pandas` | 2.3.3 | BSD-3-Clause |
| `scikit-learn` | 1.9.0 | BSD-3-Clause |
| `neurokit2` | 0.2.12 | MIT |
| `mne` | 1.12.1 | BSD-3-Clause |
| `biosppy` | 2.2.4 | BSD (variant not stated in metadata) - PyPI classifier: BSD License |
| `pyhrv` | 0.5.0 | BSD (variant not stated in metadata) - PyPI classifier: BSD License |
| `yasa` | 0.7.0 | BSD-3-Clause |
| `neurodsp` | 2.3.0 | Apache-2.0 |
| `wfdb` | 4.3.1 | MIT |
| `pylsl` | 1.18.2 | MIT |
| `pyxdf` | 1.16.8 | BSD-2-Clause |
| `edfio` | 0.4.16 | Apache-2.0 |
| `brainflow` | 5.22.2 | Not declared in PyPI metadata - verify - Licence not declared in package metadata |
| `tqdm` | 4.70.0 | MPL-2.0 AND MIT - MPL-2.0 is file-level copyleft; unmodified use |
| `requests` | 2.34.2 | Apache-2.0 |
| `pyyaml` | 6.0.3 | MIT |
| `pydantic` | 2.13.5 | MIT |
| `pydantic-settings` | 2.15.0 | MIT |
| `tensorflow-cpu` | 2.16.2 | Apache-2.0 - Per PyPI classifier (not installed locally) |

All runtime dependencies use permissive or file-level-copyleft licences compatible with distributing SYNAPSE-24 under AGPL-3.0-or-later **and** under a commercial licence. `brainflow`, `biosppy` and `pyhrv` should have their exact licence confirmed before the first commercial release.

> `heartpy` (GPL) was declared but never imported and has been removed: a GPL runtime dependency would have prevented granting a proprietary commercial licence.

## Development and optional tooling (not distributed)

| Package | Locked version | Licence |
|---------|----------------|---------|
| `pytest` | 8.4.2 | MIT |
| `pytest-cov` | 5.0.0 | MIT |
| `pytest-xdist` | 3.8.0 | MIT |
| `pytest-mock` | 3.15.1 | MIT |
| `hypothesis` | 6.167.1 | MPL-2.0 - Dev only; not distributed |
| `ruff` | 0.16.5 | MIT |
| `mypy` | 1.20.2 | MIT |
| `pre-commit` | 3.8.0 | MIT |
| `types-requests` | 2.33.0.20260712 | Apache-2.0 |
| `types-pyyaml` | 6.0.12.20260815 | Apache-2.0 |
| `types-tqdm` | 4.70.0.20260827 | Apache-2.0 |
| `esptool` | 4.12.0 | GPL-2.0-or-later - Run as an external CLI via subprocess; never imported or redistributed |
| `bleak` | 0.22.3 | MIT |

## Firmware

Firmware under `firmware/` is built on **ESP-IDF 5.2+** (Apache-2.0, including its bundled FreeRTOS under MIT). `firmware/esp32_band/README.md` references TensorFlow Lite Micro (Apache-2.0); it is not vendored in this repository, so record its version and licence when it is added to a build. Verify the licences of any component fetched at build time before shipping device images.

## Datasets

Ingestion and validation download public research datasets. They are **not** covered by SYNAPSE-24's licence and **are not redistributed** here.

| Dataset | Source | Note |
|---------|--------|------|
| MIT-BIH Arrhythmia, Sleep-EDF | PhysioNet | Check the licence on the dataset page; attribution required |
| WESAD | UCI / original authors | Check terms for commercial use before shipping anything derived from it |
| DEAP | Queen Mary Univ. of London | Access requires accepting an EULA |

Models trained on these datasets (e.g. `firmware/esp32_tier0/main/triage/model_data.h`) may inherit their restrictions. Confirm the terms, or train on data you are licensed to use commercially, **before** shipping a model in a product.
