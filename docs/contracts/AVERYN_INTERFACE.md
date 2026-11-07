# Averyn × Synapse — Interfacce (v2)

> **Stato di questo documento.** Descrive le interfacce che SYNAPSE-24 espone e **a chi servono davvero**. Una versione precedente presentava il Data Plane HTTP come il canale con cui Averyn "importa le sessioni" etichettandole con una sorgente propria del Data Plane e mostra pagine dedicate a readiness, sonno, stress e HRV. Non è così: gli ADR di Averyn (0010, 0011, 0014, 0026) stabiliscono che Averyn è l'unico *system of record*, **non chiama mai il Data Plane** e non ha un token condiviso per interrogare i dati di tutti, e nessun punteggio "readiness" viene calcolato da nessuna parte di questo repository.

Averyn non tocca il firmware: consuma interfacce standard e contratti versionati. Le fonti di verità sono in `contracts/` e in `tools/check_interfaces.py`.

## Che cosa consuma Averyn (livelli di integrazione)

| Livello | Canale | Contratto | Stato in SYNAPSE-24 |
|---|---|---|---|
| **L0** | BLE standard: Heart Rate `0x180D` (`0x2A37`, con RR), Battery `0x180F`, Device Information `0x180A` | Bluetooth SIG. L'RR è in **1/1024 s** | Pubblicato dalla band. RR oggi sempre assente (`0xFFFF`) |
| **L1** | BLE: feature stream sul servizio consumer `f9e08ffd-…` | `contracts/gatt_v1.json` (**congelato**, sha256 fissato da Averyn) e `contracts/gatt_v2.json` (nuovo, generato) | v1 operativo in codice; v2 in codice, non provato con un telefono |
| **L2** | Import di un file FIT con i campi sviluppatore `synapse_*` | `contracts/fit_developer_fields.json` | Scrittura/lettura nell'SDK; Averyn non lo legge ancora |
| **L3** | Job di analisi (worker stateless) con credenziale per job | `contracts/schemas/worker_job_result.json` | Worker esiste; la credenziale per job (ADR-0011) **non è implementata** |

Il **Data Plane HTTP** descritto sotto è uno strumento di ricerca **locale**: serve a chi sviluppa o analizza sessioni sulla propria macchina. Averyn non lo usa.

## Contratto GATT del servizio consumer

- **v1 (congelato).** `contracts/gatt_v1.json` non si modifica: Averyn ne fissa lo sha256 (`0c6b9e38…88a9`, LF) e `tools/check_interfaces.py` lo impone. Il frame di feature v1 è di **23 byte** (non 26 come si leggeva in passato; misurato con `sizeof` dal vero header) e richiede un MTU ATT di almeno 26.
- **v2.1 (nuovo, non ancora fissato da nessun consumatore).** Quattro characteristic nuove sullo stesso servizio, quindi un client v1 non le vede: *Feature v2* (notify, 27 B), *Status* (read + notify, 40 B), *Stream Metadata* (read: id, formato, canali, unità, frequenza, scala di ogni stream) e *Control Point* (write: start / stop / frequenza / `set_time`). Layout, offset, sentinelle, unità e scala di ogni campo sono generati da `config/hardware.yaml` in `contracts/gatt_v2.json`; i golden vector (feature e status) sono in `contracts/vectors/consumer_v2.json`, generati con l'encoder `synapse_device.consumer` da `scripts/make_consumer_vectors.py`. La 2.1 corregge la 2.0 prima che qualcuno la fissasse: aggiunge `motion_artifact` (il `ppg_map` della v1, che l'L1 di Averyn mostra), un'unica definizione dei bit di `flags` per feature e status, le sentinelle di `stress_level` e `sport_type` (255), `ppg_skipped` e `power_dropped` nello Status, e per ogni stream l'unità da mostrare (`display_unit`, `display_per_wire`: il giroscopio viaggia in mrad/s ma si mostra in °/s, il magnetometro in 0,01 µT ma si mostra in µT).
- **Un valore uguale alla sentinella "non disponibile" non è una misura**: va letto come `null`, mai come `0`.
- **Start/Stop** governano gli stream *raw*; feature e status continuano e riportano lo stato nel flag `transmitting`. Una nuova connessione parte con gli stream attivi.
- **`set_time`** (`[4][epoch_ms u64 LE]`): la band non ha né RTC né rete; il telefono le dà l'ora a ogni connessione. Un valore precedente al 2020-01-01 è rifiutato (`bad_argument`).
- Il flag `charging` vale solo se il bit POWER di `sensor_health` è acceso; altrimenti la band non lo sa e il bit è 0.
- Il feature v2 richiede un MTU ≥ 30: i client Android devono chiederlo esplicitamente (la band lo chiede a sua volta alla connessione).
- Il servizio *research* (raw: ECG, PPG, IMU, power) è sottoscritto da Averyn **solo con lo scope di consenso `raw_signals`** (livello L1R, ADR-0030 di Averyn, proposto): è così che si vedono le forme d'onda, e solo in Averyn. Contratto del frame: `contracts/raw_frame_v1.json`; vettori: `contracts/vectors/raw_frame_v1.json`; sessioni sintetiche con risposta attesa: `contracts/fixtures/band_scenarios_v1.json`.

## Endpoint HTTP (Data Plane locale, `127.0.0.1:8000`)

Base path: `/api/v1`. Gli errori sono HTTP veri: una sessione inesistente è **404** (prima era `200` con un corpo `{"error": …}`).

| Endpoint | Metodo | Descrizione |
|---|---|---|
| `/sessions` | GET | Elenco sessioni indicizzate (ID, dispositivo, finestra, stream, durata). |
| `/sessions/{session_id}` | GET | Dettaglio sessione. `path` è **relativo** alla directory delle sessioni. |
| `/sessions/{session_id}/report` | GET | **SessionReport v1** (vedi sotto). |
| `/metrics` | GET | Metriche aggregate (n sessioni, per formato, campioni totali). |
| `/jobs/types` | GET | Tipi di job supportati dal worker. |
| `/jobs/run` | POST | Esegue inline un job su un file della directory delle sessioni. |

Altri percorsi: `/api/health` (liveness, aperta), `/api/status` (n. sessioni; `current_tier` è `null` in modalità solo-software), `/api/config` (manifest hardware). Il viewer (`/live`, `/ws/live`) non esiste più (vedi sotto). `/api/live/*`, `/api/control/*` rispondono **501**: richiedono un dispositivo. Anche queste sono dietro il token: con `SYNAPSE_API_TOKEN` impostata una chiamata senza credenziale riceve **401** prima di arrivare al 501.

Corpo di `POST /api/v1/jobs/run`:

```json
{ "computation_type": "hrv_trends|stress_daily|sqi_report|sleep_staging", "file_path": "sessione.fit" }
```

`file_path` è il percorso relativo restituito dall'API (o un percorso assoluto dentro una root consentita).

## Sicurezza del Data Plane (locale)

Il Data Plane è **local-only**: il compose lo pubblica su `127.0.0.1:8000`, e avviato direttamente ascolta su loopback (`SYNAPSE_API_HOST` per cambiarlo; il container imposta `0.0.0.0` solo perché la mappatura della porta funzioni).

### Bearer token (opzionale)

Se `SYNAPSE_API_TOKEN` è impostata, **ogni** route `/api/*` richiede `Authorization: Bearer <token>`; mancante o errato = HTTP 401. `/api/health` resta aperta (non contiene dati). Senza token l'API è aperta per lo sviluppo locale (WARNING in avvio).

Questo token è un controllo d'accesso a uno strumento locale. **Non è** la credenziale che Averyn userebbe per un utente (ADR-0011: nessun token condiviso che possa interrogare i dati di qualunque utente; per L3 Averyn conia una credenziale per job).

### Nessun viewer in questo repository

Il viewer locale (`/live`, `/ws/live`) è stato **rimosso**: tutta la visualizzazione e l'analisi visiva di ciò che la band misura vive in Averyn ([ADR-0030](https://github.com/AlessioBrillo/averyn/blob/main/docs/adr/0030-synapse-contract-v2-1-and-raw-signals.md) lì). Qui restano firmware, contratti, vettori e test; l'API HTTP sotto è uno strumento di test locale.

### CORS

Origini esplicite, mai il wildcard: di default il dev server Vite (`http://localhost:5173`, `http://127.0.0.1:5173`), estese con `SYNAPSE_CORS_ORIGINS`.

### Confini dei path (`POST /api/v1/jobs/run`)

Ogni `file_path` è risolto (`Path.resolve()`) e deve cadere in una *root consentita*: la directory delle sessioni più le root di `SYNAPSE_DATA_ROOTS`. Un percorso relativo è risolto rispetto alla directory delle sessioni e **non può uscirne** (`../x` è rifiutato). Un percorso esterno viene rifiutato **senza mai essere aperto** (`ok=false`, `error="path outside allowed roots"`). Nessuna risposta contiene percorsi assoluti del server.

## Contratti machine-readable (`contracts/`)

| File | Contenuto |
|---|---|
| `contracts/gatt_v1.json` | Contratto GATT v1, **congelato** (sha256 fissato). |
| `contracts/gatt_v2.json` | Contratto consumer v2 (UUID, layout con offset e sentinelle, metadati degli stream, control point). **Generato** da `config/hardware.yaml`. |
| `contracts/vectors/consumer_v2.json` | Golden vector del feature v2, generati dall'encoder dell'SDK. |
| `contracts/fit_developer_fields.json` | Descrittore canonico degli 8 FIT developer field. `synapse_sleep_stage`: **0=W 1=N1 2=N2 3=N3 4=REM, 255 = non disponibile** (prima descritto in tre modi diversi). |
| `contracts/schemas/worker_job_result.json` | JSON Schema dei payload dei 4 job. |
| `contracts/schemas/session_report.json` | JSON Schema del SessionReport v1. |

Strumenti di guardia: `tools/check_interfaces.py` (id dei field FIT tra firmware, SDK, export e worker; **significato** dell'enum del sonno; UUID confrontati per intero; congelamento del v1; v2) e `tools/generate_contracts.py --check` (gli artefatti generati non possono divergere dal manifest).

## Payload dei job (worker stateless)

Tutti gli istanti `window_start` sono **secondi Unix**, qualunque sia il file di origine (prima erano secondi FIT per un input FIT).

| Job | Input | Output chiave |
|---|---|---|
| `hrv_trends` | FIT o XDF | finestre: `rmssd_ms`, `sdnn_ms`, `mean_rr_ms`, `hr_mean_bpm`, fonte. Da XDF: stesse finestre dell'export (`utils.windows.window_bounds`); il flusso PPG ha un rilevatore proprio |
| `stress_daily` | FIT (`synapse_stress`) | `stress_level`, probabilità, `daily_average`, `peak_stress`, `recovery_periods`, `stress_episodes`. Lo stato 3 (*unknown*, il triage non ha girato) **non è un'etichetta**: un file senza etichette risponde `insufficient_data` |
| `sqi_report` | FIT (`synapse_sqi`/`map`/`ecg_quality`) | `sqi`, `map`, `ecg_quality` (**null** se nessuna finestra è valida), `per_channel`, `window_s` |
| `sleep_staging` | XDF con stream **EEG** (YASA) | `sleep_stage` (0..4, o 255/null se non classificato), `confidence` (null senza stadio), `epoch_s`, `hypnogram`, `input_modality: "eeg"` |

`sleep_staging` accetta **solo EEG**; un file PPG/IMU-only restituisce errore e non rivendica mai "YASA sleep staging". Un consumatore deve gestire `null` e `255`.

## SessionReport v1

Restituito da `GET /sessions/{id}/report`: `report_version` (`"session-report-v1"`), `session_id`, `device_serial`, `format`, `window`, `streams[]` (nome, tipo, frequenza, campioni, unità), `decimation`, `series` (in v1 **vuota**: le forme d'onda restano nel file FIT/XDF), `metrics`, `events[]`, `quality`, `raw_ref` (`path` relativo, `sha256`, `size_bytes`). Il report **non include ancora i risultati dei job**: `metrics` e `events` derivati dai job sono vuoti finché non si decide dove memorizzarli.

Le waveform complete non sono embeddate: si vedono in Averyn (livello L1R), non qui.

## Chi produce cosa

L'SDK `synapse` è il layer device (`discover`, `info`, `record` — sintetico o da un pod BLE —, `inspect`): non esporta in FIT e non indicizza. L'indicizzazione del catalogo, il SessionReport v1 e l'export XDF → FIT con HRV stanno nel pipeline host (`synapse-sessions index`, `synapse-report`, `synapse-export`).

## Consenso e privacy

Il Data Plane è stateless e non gestisce identità: il consenso (HRV, sonno, stress, qualità segnale, esportazione raw, personalizzazione, condivisione ricerca) è gestito da Averyn (system of record), che oggi non ha ancora gli scope `signal_quality` e `stress_monitoring` richiesti dall'integrazione L1. Il worker non riceve mai identificativi diretti dell'utente (`job_contract.py`).

## Versioning

`fit_developer_fields.json` (id immutabili; il *significato* dell'enum del sonno è cambiato in MVP-1.3, nessun consumatore noto lo leggeva), `worker_job_result.json`, `session_report.json`, GATT v1 (congelato) e GATT v2. I drift sono rilevati da `tools/check_interfaces.py` e `tools/generate_contracts.py --check`.
