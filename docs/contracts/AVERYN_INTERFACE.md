# Averyn × Synapse — Interfaccia del Data Plane (v1)

> **Averyn non tocca il firmware.** Averyn consuma le interfacce standard che Synapse espone (FIT developer data, XDF, LSL) e, tramite il Data Plane locale di SYNAPSE-24, trasforma una sessione registrata in Readiness, Sleep Quality, Stress Trend e HRV pronti per la UI.

Questo documento è il contratto macchina-leggibile tra **SYNAPSE-24 Data Plane** e **Averyn** (backend Ktor + web React). È la specifica di riferimento per l'integrazione; i file sorgente di verità sono in `contracts/` e `tools/check_interfaces.py`.

## Endpoint HTTP (Data Plane, localhost di default `:8000`)

Base path: `/api/v1`

| Endpoint | Metodo | Descrizione |
|---|---|---|
| `/sessions` | GET | Elenco sessioni indicizzate (ID stabile, dispositivo, finestra, stream, durata). |
| `/sessions/{session_id}` | GET | Dettaglio sessione (record completo, stream, validità XDF/FIT). |
| `/sessions/{session_id}/report` | GET | **SessionReport v1**: metadati, canali+unità, decimation policy, metriche per finestra, eventi, quality summary, riferimento al raw (FIT/XDF). |
| `/metrics` | GET | Metriche aggregate (n sessioni, per formato, campioni totali). |
| `/jobs/types` | GET | Tipi di job di analisi supportati dal worker. |
| `/jobs/run` | POST | Esegue inline un job del worker stateless su un file già memorizzato. |

Corpo di `POST /api/v1/jobs/run`:
```json
{ "computation_type": "hrv_trends\|stress_daily\|sqi_report\|sleep_staging", "file_path": "/percorso/sessione.fit" }
```

## Contratti machine-readable (`contracts/`)

| File | Contenuto |
|---|---|
| `contracts/fit_developer_fields.json` | Descrittore canonico degli 8 FIT developer field (id, nome, tipo, unità, enum/range, sentinella invalida). |
| `contracts/schemas/worker_job_result.json` | JSON Schema dei payload dei 4 job del worker (`hrv_trends`, `stress_daily`, `sqi_report`, `sleep_staging`). |
| `contracts/schemas/session_report.json` | JSON Schema del SessionReport v1. |

Strumento di guardia: `tools/check_interfaces.py` verifica che i field ID FIT siano coerenti tra `firmware/esp32_band/fit/fit_writer.h`, `scripts/export_session_fit.py`, `worker/hrv_worker.py` e il descrittore, e che gli schemi JSON siano validi. Esce non-zero su drift (proveto-fallire via `tests/test_check_interfaces.py`).

## Payload dei job (worker stateless)

I payload rispettano `worker/job_contract.py` (`*_KEYS`) e `contracts/schemas/worker_job_result.json`:

| Job | Input | Output chiave |
|---|---|---|
| `hrv_trends` | FIT o XDF | finestre: `rmssd_ms`, `sdnn_ms`, `mean_rr_ms`, `hr_mean_bpm`, fonte |
| `stress_daily` | FIT (campi `synapse_stress`) | `stress_level`, `stress_prob`, `baseline_prob`, `artifact_prob`, `daily_average`, `peak_stress`, `recovery_periods`, `stress_episodes` |
| `sqi_report` | FIT (campi `synapse_sqi`/`map`/`ecg_quality`) | `sqi`, `map`, `ecg_quality`, `per_channel`, `window_s` |
| `sleep_staging` | XDF con stream **EEG** (YASA) | `sleep_stage`, `confidence`, `epoch_s`, `hypnogram`, `input_modality: "eeg"` |

Nota: `sleep_staging` accetta **solo EEG**; un file PPG/IMU-only restituisce errore e non rivendica mai "YASA sleep staging".

## SessionReport v1 (rappresentazione per Averyn)

Restituito da `GET /sessions/{id}/report`. Struttura principale:

- `report_version`: `"session-report-v1"`
- `session_id`, `device_serial`, `format` (`xdf\|fit`)
- `window`: `started_at`, `ended_at`, `duration_s`
- `streams[]`: `name`, `type`, `sampling_rate_hz`, `n_samples`, `unit`
- `decimation`: `factor`, `policy` (`min_max_envelope`), `applied_to`
- `series`: serie decimata preview (v1: vuota; il raw rimane nel file FIT/XDF)
- `metrics`: oggetto indicizzato per tipo di job (es. `metrics.stress_daily`, `metrics.sleep_staging`)
- `events[]`: episodi (stress episode, transizioni stadio sonno)
- `quality`: `overall` (`good\|warning\|poor`), `xdf_valid`, `fit_valid`, `notes`
- `raw_ref`: `path`, `sha256`, `size_bytes` (riferimento al file sorgente)

I dati grezzi (waveform complete) **non sono embeddati** nel report; restano nei file FIT/XDF di origine, consultabili tramite `raw_ref.path`.

## Come Averyn consuma

1. L'utente registra una sessione con la Synapse Band (file `.fit` o `.xdf` sul dispositivo).
2. Il file viene portato sul host che esegue il Data Plane (o indicizzato direttamente).
3. `GET /api/v1/sessions` espone il catalogo; Averyn importa la sessione come dato con `source: "synapse-data-plane"`.
4. `GET /api/v1/sessions/{id}/report` fornisce Readiness/Sleep/Stress/HRV già calcolati (o Averyn lancia `POST /api/v1/jobs/run` per calcolarli al volo).
5. Averyn mostra le metriche nelle sue pagine (`/synapse/readiness`, `/synapse/sleep`, `/synapse/stress`, `/synapse/hrv`) senza conoscere il firmware.

## Consenso e privacy

Il Data Plane è stateless e non gestisce identità utente: il consenso (HRV, sonno, stress, qualità segnale, esportazione raw, personalizzazione, condivisione ricerca) è gestito da Averyn (system of record). Il worker non riceve mai identificativi diretti dell'utente (`job_contract.py`).

## Versioning

Tutti i contratti sono versionati (`fit_developer_fields.json` v1.0, `worker_job_result.json` v1.0, `session_report.json` v1.0, GATT v1 già congelato). I field ID FIT sono immutabili una volta che un consumatore importa i file. I drift vengono rilevati da `tools/check_interfaces.py`.
