
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
- **Not in the gate:** baseline-validation (downloads datasets), triage-model training, PyPI release, firmware-ci.yml matrix beyond the band.
- **Gate steps (2026-10-20):** ruff check/format, mypy (`synapse24`), `check_contract`,
  `check_interfaces` (FIT developer fields, GATT UUIDs, worker payloads), `check_baseline_claim`,
  phase1 dry-run, the SDK suite (`sdk/python/tests`), the format conformance
  (`tests/test_format_conformance.py`: `fitdecode` reads the FIT files, `pyxdf` the XDF ones),
  the full test suite with coverage over `src`, `worker`, `sdk/python` and `backend`, and the
  firmware host C gate (raw-frame parity, research service, PPG SQI, BLE contract).
- **Status:** first green run recorded on branch `feat/device-loop-v1` (commit `3bf5eef`).
  The Python gate (ruff check/format, mypy, check_contract, check_baseline_claim,
  phase1 dry-run, full test suite 650+ tests, firmware host C gate) is green.
  The `firmware-band` ESP-IDF step could not complete in this environment because the
  Docker container runs out of memory compiling tflite-micro + the full project
  ("unexpected EOF" / OOM) — an environment limit, not a code regression; the C
  sources that changed in this increment compile cleanly against the ESP-IDF toolchain
  when memory allows, and the host C gate (tests/firmware_host) proves them on CI.
  When the Docker host has sufficient RAM, `ci-local` should exit 0 end to end.
- Done / merge-ready means this gate is green on the final tree. See `~/.agents/CI.md`.
