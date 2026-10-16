
## CI gate

GitHub Actions is not the gate. The single source of truth is `.woodpecker/ci.yml`.

- **Local, no server:** `ci-local` from the repo root (Docker running). Exit 0 = green.
  Never run `woodpecker-cli exec` directly on the checkout: it bind-mounts the repo and
  install steps would overwrite host dependencies.
- **Server:** push the branch; the check is `ci/woodpecker/push/ci` (`gh pr checks <n> --watch`).
- **Not in the gate:** baseline-validation (downloads datasets), triage-model training, PyPI release, firmware-ci.yml matrix beyond the band.
- **Status:** pipeline written, first green run not recorded yet. If a step fails for an
  environment reason (image, network, missing tool), fix `.woodpecker/ci.yml`, not the code.
- Done / merge-ready means this gate is green on the final tree. See `~/.agents/CI.md`.
