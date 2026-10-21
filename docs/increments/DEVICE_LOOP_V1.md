# Increment: Device Loop v1

**Branch:** `feat/device-loop-v1` · **Scope:** software-only, no hardware purchase required · **Gate:** `ci-local` (Woodpecker) verde

## Obiettivo dell'incremento

Chiudere, in software e senza hardware, il loop **device → host → analisi → rappresentazione** della Synapse Band v1, con un SDK host reale (Apache-2.0), contratti machine-readable enforced e i claim del progetto verificabili. È il prerequisito della Phase 1 "Fondamenta" (roadmap strategica) prima del bench G1 (smoke kit ~€140, ordinato in parallelo).

## Decisioni architetturali recepite

| Decisione | Scelta |
|-----------|--------|
| Perimetro | Device Loop v1 completo (gate verde + SDK Python v0 + integrità contratti + e2e sintetico + deploy coerente + baseline verificabile + docs/compliance) |
| Smoke kit G1 | Non ancora ordinato (~€140, `docs/hardware/PROCUREMENT.md`), ordinato in parallelo al PR0 |
| Licenza device-side | Split: firmware **MIT** (`firmware/LICENSE`), SDK **Apache-2.0** (`sdk/python/LICENSE`), `synapse24`/worker/backend **AGPL-3.0-or-later** (`LICENSE`). Il device layer (codec, transport, clock, XDF/FIT) vive nell'SDK; `synapse24` dipende dall'SDK, mai il contrario |
| Data plane | Locale/offline, self-host (`backend/` = Local Data Plane, nessuna auth, CORS solo localhost); il servizio multi-utente sarà di `synapse-cloud` |
| GATT canonico | `contracts/gatt_v1.json` (frozen v1), allineato a `config/hardware.yaml`; UUID research service `A1B2C3D4-…`, feature stream `f9e08ffd-…` |
| Storage firmware | LittleFS dichiarato → codice usa **SPIFFS** (documentato come tale nel firmware README); migrazione a LittleFS rimandata a un incremento dedicato |
| Baseline Phase 0 | Claim gating via `tools/check_baseline_claim.py` vs `docs/baselines/phase0_results.json` (placeholder: WESAD/Sleep-EDF non verificabili su questo host, vedi READINESS_GATE.md Known failures) |
| Frontend | Archiviato in `docs/archive/frontend/` (client legacy, shape rotta, fuori CI; la UI è di Averyn) |

## Deliverable (per PR)

| PR | Contenuto | Stato |
|---|---|---|
| PR0 | Gate verde: ruff/pyproject migrate (+EXE002), fix duplicati GATT e UUID pointer, fit_writer simbolo, firmware band-core+full nel gate, `ci-local` verde registrato | ✅ |
| PR1 | Regression guard su dupliche GATT e simbolo FIT (test compiler-free Python) | ✅ |
| PR2 | SDK Python v0 `synapse-device` (Apache-2.0): codec, clock, transport (synthetic+bleak), FIT writer, XDF reader, session model, `synapse` CLI (discover/info/record/export/index/report/analyze), synthetic XDF recording | ✅ |
| PR3 | `contracts/gatt_v1.json` + check GATT in `check_interfaces.py`; `HRV_TRENDS_KEYS` allineato; `sqi_report` aggregati reali + `window_s` in secondi; `file_sha256` verifica; jsonschema validazione payload worker; test provato-fallire GATT drift | ✅ |
| PR4 | TestClient e2e sulle 6 rotte del data plane (`tests/test_backend_api.py`); Dockerfile backend/worker riparati; `worker` in `docker-compose.yml`; coverage estesa a `sdk/python` e `backend`; fastapi/httpx/jsonschema/uvicorn dev deps; `uv.lock` aggiornato | ✅ |
| PR5 | `tools/check_baseline_claim.py` nel gate; `docs/baselines/phase0_results.json` placeholder; WESAD/Sleep-EDF non verificabili su questo host documentati | ✅ |
| PR6 | README (badge Woodpecker, numeri veri, struttura, nota baseline), firmware README (NimBLE/SPIFFS/26 B), `compliance/LICENSING.md` split, `firmware/LICENSE` (MIT), `sdk/python/LICENSE` (Apache-2.0), frontend archiviato, increment summary | ✅ |

## Correzioni successive (incremento MVP-1, 2026-10-20)

Questo documento resta il registro di quell'incremento, ma alcune sue
affermazioni non reggevano il confronto con l'hardware e con i formati.
L'incremento **MVP-1 — Loop verificabile**
(`docs/increments/MVP1_VERIFIABLE_LOOP.md`) le ha corrette; le note qui
servono a non rileggere le claim come se fossero ancora vere:

- **"SDK Python v0: codec, clock, transport, FIT writer, XDF reader,
  session model, `synapse` CLI (discover/info/record/export/index/report/
  analyze)"**: i quattro comandi di analisi erano stub (`return 2`) e il
  FIT writer non produceva un file leggibile da un parser standard. Ora
  l'SDK è il *device layer* (`discover/info/record/inspect`), i comandi di
  analisi stanno nel pipeline dell'host (`synapse-sessions`,
  `synapse-report`, `synapse-export`) e i formati sono validati da
  `fitdecode` e `pyxdf` nel gate.
- **"Data plane locale/offline, self-host, nessuna auth"**: il piano dati
  ora ha un token bearer opzionale (`SYNAPSE_API_TOKEN`), un allowlist di
  origini CORS e un confine esplicito per i percorsi forniti dal client.
- **"Nessun claim nei doc senza comando/test che lo copra"**: era vero per
  i documenti di quell'incremento, non per il resto del repository: i
  formati e la suite dell'SDK non erano coperti da alcun test nel gate.
  Ora lo sono.

La decisione di struttura (un solo percorso canonico, SDK = device layer)
è in `docs/decisions/ADR-001-canonical-paths.md`.

## Cosa NON è in questo incremento (per design)

- OTA firmata (Ed25519), provisioning mTLS/CSR, GPS, TMP117, sessione FIT on-device, multi-pod wired sync.
- Cloud/TimescaleDB/EMQX/MinIO, Synapse Cloud, auth multi-utente.
- frontend/Averyn UI (archiviata), EEG/fNIRS, training ML.
- Migrazione storage firmware LittleFS (documentata come SPIFFS; migrazione in incremento dedicato).

## Definition of Done

- [x] `ci-local` (Woodpecker) verde sul tree finale (primo run verde registrato in `AGENTS.md`).
- [x] `synapse --help` funzionante via `pip install -e sdk/python`.
- [x] E2E sintetico verde: rotte data plane + validazione payload vs schema.
- [x] `tools/check_interfaces.py` e `tools/check_baseline_claim.py` exit 0 (o warning documentato).
- [x] Nessun claim nei doc senza comando/test che lo copra.
- [x] Primo run verde del gate registrato (`AGENTS.md`).

## Test aggiunti

`tests/test_firmware_gatt_no_duplicates.py`, `tests/test_firmware_fit_record_symbol.py`,
`sdk/python/tests/{test_codec,test_clock,test_fit,test_transport,test_cli}.py`,
`tests/test_backend_api.py`, `tests/test_check_interfaces.py` (GATT drift).

## File nuovi/modificati (sintesi)

Nuovi: `sdk/python/**`, `tests/test_backend_api.py`, `tests/test_firmware_*.py`,
`tests/test_check_interfaces.py` (GATT test), `contracts/gatt_v1.json`,
`docs/baselines/phase0_results.json`, `docs/increments/DEVICE_LOOP_V1.md`,
`firmware/LICENSE`, `sdk/python/LICENSE`.
Modificati: `.woodpecker/ci.yml`, `pyproject.toml`, `scripts/export_session_fit.py`,
`worker/job_contract.py`, `worker/hrv_worker.py`, `tools/check_interfaces.py`,
`firmware/esp32_band/ble/ble_gatt_server.c`, `firmware/esp32_band/fit/fit_writer.c`,
`firmware/esp32_band/README.md`, `README.md`, `compliance/LICENSING.md`,
`docker-compose.yml`, `backend/Dockerfile`, `worker/Dockerfile`, `uv.lock`.
Spostati (archiviati): `frontend/`, `FRONTEND_INTEGRATION.md` → `docs/archive/`.
