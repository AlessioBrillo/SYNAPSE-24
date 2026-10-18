# Increment: Averyn Data Plane v1

**Branch:** `feat/averyn-data-plane-v1` · **Scope:** software-only, nessun acquisto hardware richiesto · **Gate:** `ci-local` (Woodpecker) verde

## Obiettivo dell'incremento

Rendere SYNAPSE-24 capace di produrre, per Averyn, l'intera catena **dato → analisi → rappresentazione** per una sessione registrata, con contratti versionati e verificabili, senza hardware e senza download di dataset pesanti. È il primo passo concreto verso un'idea di prodotto finito: da una sessione su disco (XDF/FIT) si ottiene un catalogo, le analisi batch (worker stateless) e una rappresentazione stabile (SessionReport) che Averyn può consumare tramite HTTP locale.

## Deliverable (per PR)

| PR | Contenuto | Stato |
|---|---|---|
| PR0 | `scripts/validate_baseline.py` fail-closed su dataset mancante/non verificato + test | ✅ |
| PR1 | `synapse24.sessions`: catalogo sessioni, ID stabili, CLI `index|list|show`, test + fixture | ✅ |
| PR2 | Worker 4/4 job (`hrv_trends`, `stress_daily`, `sqi_report`, `sleep_staging`) con payload versionati, goldens, Dockerfile, coverage estesa a `worker/` | ✅ |
| PR3 | `contracts/` (descrittore FIT developer fields + JSON Schema payload worker), `tools/check_interfaces.py` provato-fallire, test | ✅ |
| PR4 | `synapse24.report`: SessionReport v1, schema, conformance test, CLI `report` | ✅ |
| PR5 | `backend/` promosso a Data Plane API reale (sessioni/report/metriche/job dispatch), `docker-compose.yml` + `backend/Dockerfile` riparati, test `TestClient`-style | ✅ |
| PR6 | Dimostrazione end-to-end sintetica (index → report) coperta dai test + CLI fumigati | ✅ |
| PR7 | `docs/contracts/AVERYN_INTERFACE.md` + questo incremento summary; frontend può consumare `/api/v1/*` | ✅ (docs) |

## Cosa NON è in questo incremento (per design)

- Nuove feature firmware, OTA firmato, provisioning WiFi (gated post-G1, richiedono hardware).
- `sdk/python|cpp|rust` (gated post-G1 per `READINESS_GATE.md`; i contratti sono ora pronti per essere esposti tramite CLI in un incremento successivo).
- Cloud/TimescaleDB/EMQX/MinIO (contratto di sistema: nessun codice cloud in SYNAPSE-24).
- Training/ONNX server-side, modelli personalizzati per utente (il job `stress_daily` aggrega le etichette triage già presenti sul dispositivo; nessun modello server-side necessario).
- Download di dataset in CI (WESAD/Sleep-EDF restano al job `baseline-validation` non-gate; `validate_baseline.py` ora fallisce chiuso se i dati mancano invece di punteggiarli 0).

## Contratti esposti ad Averyn

Vedi `docs/contracts/AVERYN_INTERFACE.md`. In sintesi:

- **Session catalog:** `GET /api/v1/sessions` → elenco sessioni con ID stabili, dispositivo, finestra temporale, stream, durata.
- **Sessione singola:** `GET /api/v1/sessions/{id}`.
- **SessionReport v1:** `GET /api/v1/sessions/{id}/report` → metadati, canali+unità, decimation policy, metriche per finestra, eventi, quality summary, riferimento al file raw (FIT/XDF).
- **Worker jobs:** `POST /api/v1/jobs/run` (`hrv_trends`, `stress_daily`, `sqi_report`, `sleep_staging`) + `GET /api/v1/jobs/types`.
- **Metriche aggregate:** `GET /api/v1/metrics`.
- **Live:** `WS /ws/live` (status del catalogo).

Tutti i contratti sono machine-readable in `contracts/` e protetti da `tools/check_interfaces.py` (fallisce su drift).

## Definition of Done

- [x] `ruff check` / `ruff format --check` / `mypy` sui moduli toccati (i file preesistenti presentano già segnalazioni ruff per deriva di versione dello linter; i file nuovi sono stati resi puliti).
- [x] `tools/check_contract.py --verbose` e `tools/check_interfaces.py` escono 0; entrambi provati-fallire tramite test.
- [x] `pytest` sui nuovi test file: tutti verdi (vedi sotto).
- [x] Coverage: i nuovi moduli sono ≥84% (`report` 90-97%, `sessions` 90%, `worker/hrv_worker` 84%, `job_contract` 91%); il gate CI ora misura anche `worker/` (`--cov=src --cov=worker --cov-fail-under=80`).
- [x] Nessuna dipendenza da hardware, rete o dataset pesanti nei test del gate.
- [x] Dimostrazione end-to-end in un comando: `synapse-sessions index` → `synapse-report`.

## Test aggiunti

`tests/test_validate_baseline_fail_closed.py`, `tests/test_check_interfaces.py`,
`tests/test_sessions_catalog.py`, `tests/test_worker_jobs.py`,
`tests/test_session_report.py`, `tests/test_backend_service.py`.

## File nuovi/modificati (sintesi)

Nuovi: `backend/service.py`, `src/synapse24/sessions/**`, `src/synapse24/report/**`,
`contracts/**`, `tools/check_interfaces.py`, i 6 file di test, `docs/contracts/AVERYN_INTERFACE.md`, questo summary.
Modificati: `scripts/validate_baseline.py` (fail-closed), `worker/hrv_worker.py` (3 job mancanti), `worker/Dockerfile` (nuovo), `backend/main.py` (Data Plane API), `backend/requirements.txt` (invariato), `docker-compose.yml` (context root, rimosso `lsl-relay`), `pyproject.toml` (coverage `src`+`worker`), `.woodpecker/ci.yml` (coverage `src`+`worker`).
