# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| master  | :white_check_mark: |

## Reporting a Vulnerability

SYNAPSE-24 is a research platform for 24/7 multimodal biosensing (ECG, PPG, IMU,
EEG, fNIRS). It is **not a medical device** and has no regulatory clearance.

If you discover a security vulnerability, please report it responsibly:

1. **Do not open a public issue.** Public issues expose the vulnerability before
   it can be fixed.
2. Send details to the maintainer via GitHub Security Advisories
   (https://github.com/AlessioBrillo/SYNAPSE-24/security/advisories/new) or
   email the repository owner directly.
3. Include:
   - A clear description of the vulnerability and its impact
   - Steps to reproduce (firmware, host SDK, backend API, or CI/CD path)
   - Affected versions / commits
   - Possible mitigations or workarounds

## Scope

In scope:
- Firmware (ESP32 band / forearm / head / tier0) — BLE, FIT storage, OTA,
  provisioning, acquisition FSM
- Host SDK and pipeline (`src/synapse24`, `sdk/python`, `scripts`, `worker`,
  `backend`)
- CI/CD pipelines (`.woodpecker/ci.yml`, `.github/workflows`)
- Docker images and `docker-compose.yml`

Out of scope:
- Third-party data imported into the repository (public datasets such as
  WESAD, MIT-BIH, Sleep-EDF, DEAP, fNIRS) — report dataset issues to their
  original authors.
- Physical hardware security (side-channel, JTAG, physical access to the pod).

## Security Features

- **API authentication:** `SYNAPSE_API_TOKEN` bearer token protects all
  `/api/v1/*` endpoints (except `/api/health`). The API is open by default for
  local development — set the token before exposing the port to any network.
- **Path containment:** `backend/service.py` resolves and confines every
  client-supplied file path to allowed roots (`SYNAPSE_SESSIONS_DIR` plus
  `SYNAPSE_DATA_ROOTS`).
- **No hardcoded secrets:** Secrets are injected via environment variables or
  GitHub Actions secrets (`${{ secrets.X }}`). `.env` is gitignored; only
  `.env.example` is tracked.
- **Config files may contain BLE addresses / serial paths:** `config/*.local.yaml`
  and `config/secrets.yaml` are gitignored. Never commit real device addresses.
- **Signed releases:** Firmware releases are built via ESP-IDF CI; verify
  artifacts against the published checksums.

## Dependencies

Python dependencies are pinned in `uv.lock`. Dependabot is not currently
configured; review dependency updates manually before merging.

## Policy

We do not offer bounties or rewards for vulnerability reports at this time.
Reports are acknowledged and fixed on a best-effort basis as a research project.
