
## CI gate

GitHub Actions is not the gate. The single source of truth is `.woodpecker/ci.yml`.

- **Local, no server:** `ci-local` from the repo root (Docker running). Exit 0 = green.
  Never run `woodpecker-cli exec` directly on the checkout: it bind-mounts the repo and
  install steps would overwrite host dependencies.
  On a Windows host the containerized run is not viable (the workspace is a bind mount
  that cannot serve the ~10k files of TensorFlow, and the machine may be running other
  pipelines): `bash scripts/run_gate_locally.sh` runs the **same command sequence** of
  `.woodpecker/ci.yml` directly on the host, step by step, and prints
  `GATE SEQUENCE: ALL GREEN` only if every step passed. It is the local equivalent of the
  gate, not a replacement for the server run.
- **Server:** push the branch; the check is `ci/woodpecker/push/ci` (`gh pr checks <n> --watch`).
- **Real-signal chain (MVP-1.8):** `tests/test_real_signal_chain.py` (ECG MIT-BIH through the C raw-frame encoder, fixture `tests/fixtures/mitbih_ecg_30s.npz`) runs in the suite; it needs the host C codec like the other C host tests, so a "policy blocked" skip there is not a pass. `tests/test_real_ppg_chain.py` does the same for a real finger PPG (BIDMC fixture) through `synapse_rr.c`; the PPG RMSSD is ~5x the ECG's on record 02 with every detector tried (not a filter bug) and is pinned as a known limit, so `CONFIG_SYNAPSE_RR_ENABLE` stays off. `g1_measure.py` computes the SOP 4a bench numbers from the ECG stream (uV on the wire, not ADC counts); first day without hardware: `docs/hardware/FIRST_HARDWARE_DAY.md`. WESAD (wrist, in motion) was not run: `docs/increments/MVP1_8_REAL_SIGNAL.md`.
- **TensorFlow is the `train` extra** (`uv sync --extra train`): a plain `uv sync --dev --frozen`, the gate and the Docker images do not install it (it was a core dependency, ~10k files and 2+ GB, used only by the triage training and quantisation). Every TensorFlow import is guarded, so the tests that need it skip without it; training and `quantize_and_deploy.py` need the extra.
- **GitHub Actions** only holds a notice pointing here (`.github/workflows/ci.yml`); there is no second pipeline to keep aligned.
- **Not in the gate:** baseline-validation (downloads datasets), triage-model training, PyPI release, firmware builds outside the `firmware-band` step, and the radio side of the virtual band (`scripts/virtual_band.py`, `check_phone_capture.py pull`: manual, see `docs/hardware/VIRTUAL_BAND_RUNBOOK.md`; their pure cores `virtual_band_core.py` and `check_phone_capture.py` are tested in the suite).
- **Increment status:** the authoritative state is `docs/hardware/STATUS.md` (latest: MVP-2.9, `docs/increments/MVP2_9_DEVICE_READY_HARDENING.md`). Previous: MVP-2.8 (`docs/increments/MVP2_8_HARDWARE_READINESS_FIXES.md`), MVP-2.7 (`docs/increments/MVP2_7_FIRST_DEVICE_ONBOARDING.md`).
- **Gate steps (2026-11-28):** ruff check/format (unused imports included), mypy (`synapse24`, the SDK, `worker` and `backend`), `check_contract`, `generate_contracts --check`, `check_interfaces`, `check_baseline_claim`, phase1 dry-run, format conformance (`fitdecode`/`pyxdf`), the full test suite with coverage over `src`, `worker`, `backend` (>= 80 %), the SDK suite with its own >= 80 %, the firmware host C gate (raw-frame parity, research service, PPG SQI, BLE contract, compiled per-unit JSON parser and INA219 host gate), and the `firmware-band` ESP-IDF step (band-full, band-core, band-bench, band-full+RR, band-product). Fixture drift checks run inside the suite.
- **Firmware step:** `firmware-band` builds the five configurations above; `run_gate_locally.sh` never runs it (needs the ESP-IDF image and more memory than this host has). GitHub Actions is notice-only: there is no second firmware job to keep aligned.
- **Status:** see `docs/hardware/STATUS.md` and the increment docs. Windows Application Control can block freshly built C DLLs and the ESP-IDF step cannot run on this host: replay the C host gate in the Linux container and read the server `firmware-band` result before calling the gate green.
- **Running the gate on this Windows host (MVP-1.3):** one `run_gate_locally.sh` run is killed when memory is low, so
  run it in pieces (each light step, the suite in chunks of 10 test files with `--cov-append`, then
  `coverage report --fail-under=80`, the SDK suite, `scripts/gcc_probe.py`). Windows Application Control intermittently
  blocks the DLLs the C host tests compile: those tests then **skip**, and a skip is not a pass. The script does not
  fail on them (a skip is not a failure), so the host C gate step runs with `-rs` and prints the reason of every skip:
  **zero skips are expected there**, and any "OS policy blocked" / "host policy blocked" line means rerun those tests in
  the gate's Linux container before calling the gate green. Details: `docs/increments/MVP1_3_FIRST_LIGHT.md`.
- Done / merge-ready means this gate is green on the final tree. See `~/.agents/CI.md`.
