# Third-Party Licenses & Dependencies

SYNAPSE-24 incorporates and builds upon several open-source libraries and frameworks. We gratefully acknowledge the contributors of these projects.

## Python Dependencies

| Package | Version | License | Purpose |
|---------|---------|---------|---------|
| `numpy` | >=1.26.0 | BSD-3-Clause | Numerical computing and matrix operations |
| `scipy` | >=1.11.0 | BSD-3-Clause | Signal processing (signal, filters) |
| `pandas` | >=2.1.0 | BSD-3-Clause | Time-series data handling and tabular datasets |
| `pylsl` | >=1.16.0 | MIT | Lab Streaming Layer C/Python bindings |
| `neurokit2` | >=0.2.6 | MIT | Biosignal processing (ECG, EDA, PPG) |
| `mne` | >=1.6.0 | BSD-3-Clause | EEG analysis and electrophysiology |
| `biosppy` | >=1.0.0 | BSD-3-Clause | Biosignal processing toolbox |
| `pyhrv` | >=0.4.2 | AGPL-3.0 | Heart rate variability analysis |
| `yasa` | >=0.6.3 | BSD-3-Clause | Yet Another Sleep Analysis (EEG sleep staging) |
| `tensorflow` | >=2.15.0 | Apache-2.0 | Edge AI model quantization and training |
| `scikit-learn` | >=1.3.0 | BSD-3-Clause | Machine learning triage classifiers |
| `pytest` | >=7.4.0 | MIT | Testing framework |
| `ruff` | >=0.1.0 | MIT | Linter and code formatter |
| `mypy` | >=1.8.0 | MIT | Static type checking |

## Firmware Dependencies

| Library / Component | License | Purpose |
|---------------------|---------|---------|
| ESP-IDF (Espressif IoT Development Framework) | Apache-2.0 | Microcontroller firmware RTOS and hardware drivers |
| FreeRTOS | MIT (modified) | Real-time operating system kernel |
| CMSIS-DSP | Apache-2.0 | ARM Cortex-M digital signal processing library |
