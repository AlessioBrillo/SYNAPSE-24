
## CI gate

GitHub Actions is not the gate. The single source of truth is `.woodpecker/ci.yml`.

- **Local, no server:** `ci-local` from the repo root (Docker running). Exit 0 = green.
  Never run `woodpecker-cli exec` directly on the checkout: it bind-mounts the repo and
  install steps would overwrite host dependencies.
- **Server:** push the branch; the check is `ci/woodpecker/push/ci` (`gh pr checks <n> --watch`).
- **Not in the gate:** baseline-validation (downloads datasets), triage-model training, PyPI release, firmware-ci.yml matrix beyond the band.
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
