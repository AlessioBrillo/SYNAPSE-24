# Increment: MVP-1 — Loop verificabile

**Branch:** `feat/mvp1-verifiable-loop` · **Scope:** software-only, nessun acquisto hardware richiesto · **Gate:** `ci-local` (Woodpecker)

## Obiettivo dell'incremento

Chiudere i P0 dell'audit ingegneristico: un loop
**device → host → analisi → rappresentazione** che un parser esterno può
leggere, con la perdita misurata, la qualità dichiarata onestamente e le
transizioni di tier realmente eseguite. Nessuna nuova feature: la release
successiva è il banco G1 (smoke kit ~€140).

La misura di successo è una frase sola, verificabile:

> una sessione attraversa device → host → XDF → FIT → report, e i file
> risultanti sono letti da `fitdecode` e `pyxdf`, cioè da parser che non
> sono di questo progetto.

## Cosa è stato trovato (audit) e cosa è stato fatto

| # | Difetto trovato | Correzione | Prova |
|---|---|---|---|
| P0-1 | Il research service veniva registrato **dopo** l'avvio del task NimBLE (`ble_gatts_add_svcs` → `BLE_HS_EBUSY`, boot abortito) | registrazione prima di `nimble_port_freertos_init()` | `tests/test_firmware_ble_contract.py` |
| P0-2 | Il pacchetto advertising era di 39 byte (limite 31) e il valore di ritorno di `ble_gap_adv_set_fields` era ignorato | adv 21 B + scan response, return code controllati, `_Static_assert` sul limite | idem |
| P0-3 | La perdita era invisibile: la sequenza avanzava solo dopo una notify riuscita e i contatori non venivono trasmessi | sequenza consumata alla *costruzione* del frame + byte `dropped` nel frame header | `tests/test_raw_frame_parity.py` |
| P0-4 | Il caratteristico HR dichiarava RR assenti e inviava un byte non inizializzato; HR non valido inviato come 0 | flag calcolati sui dati inviati, `0xFFFF` per l'invalido, quality/motion `0xFF` | `tests/test_firmware_ble_contract.py` |
| P0-5 | Nessun writer produceva FIT standard (header `0x80`, CRC per messaggio, numeri di messaggio 206/207 scambiati, campi non dichiarati) | writer unico nell'SDK, header `0x40`, CRC header+file, 206/207/209 secondo il profilo | `tests/test_format_conformance.py` (fitdecode) |
| P0-6 | L'HRV del worker mascherava tempi relativi contro limiti assoluti → finestre vuote; lo stream era scelto per numero di campioni | tempi assoluti, stream scelto **per tipo** (ECG poi PPG) | `tests/test_worker_jobs.py` |
| P0-7 | L'SDK era una facciata: `export/index/report/analyze` erano stub `return 2`, l'XDF era un HDF5 finto, il FIT non valido | device layer reale (codec allineato al firmware, XDF 1.0, FIT standard), comandi di analisi spostati nel pipeline dell'host con un messaggio esplicito | `sdk/python/tests/`, `docs/decisions/ADR-001-canonical-paths.md` |
| P0-8 | I campi di un frame erano spaziati di 1 µs invece di 1/fs | spaziatura al periodo dello stream, dal manifest | `tests/test_research_service_hil.py` |
| P0-9 | Le transizioni di tier erano solo *flag*: lo stato non cambiava; il motion gate non era mai alimentato; il validator bufferizzava ogni campione in liste Python | transizioni eseguite sullo state machine, qualità PPG reale alimentata al gate, spool su disco | `tests/test_sop_end_to_end.py`, `src/synapse24/acquisition/stream_spool.py` |
| P0-10 | `/api/v1/jobs/run` leggeva un path arbitrario senza autenticazione; CORS `*` con credentials | token bearer opzionale, allowlist di origini, confinement dei path, cache di catalogo e hash | `tests/test_backend_api.py` |
| P0-11 | La suite dell'SDK non girava mai nel gate; `check_interfaces.py` non era nel gate; nessuna conformità di formato | gate esteso: test SDK, `check_interfaces`, conformità `fitdecode`/`pyxdf` | `.woodpecker/ci.yml` |

Oltre ai P0, l'incremento ha chiuso:

- **La SQI premiava il rumore** (kurtosis ≈ 3 → score 1.0): il firmware e
  l'host usano ora una feature SNR spettrale, con test di regressione su
  entrambi i lati.
- **La SQI del firmware non produceva mai un valore**: `ppg_sqi_init()` non
  impostava `initialized`, quindi ogni campione azzerava la finestra e il
  motion gate del pod non vedeva mai la qualità. Trovato compilando il
  modulo sul host (`tests/test_firmware_ppg_sqi_host.py`).
- **Il MAP ignorava l'accelerometro** quando la sua lunghezza differiva: ora
  l'accelerometro viene risampionato sulla base temporale del PPG.
- **LF/HF su finestre da 30 s** non è interpretabile: sotto i 120 s la chiave
  non viene prodotta e `lf_hf_status` lo dichiara.
- **Il backend indicizzava e rihashava il mondo a ogni richiesta**: ora il
  catalogo si ri-indicizza solo quando l'albero cambia, e `file_sha256` è
  il digest reale del file.
- **Il contratto aveva copie manuali**: UUID, stream id e rate vivevano
  nel manifest *e* hardcoded nel firmware (con un regex checker che poteva
  solo notare il drift). Ora `tools/generate_contracts.py` genera gli
  artefatti (header C + modulo Python) dal manifest e `--check` fallisce
  sul drift: il firmware include l'header e non porta più copie.

## Deliverable (per PR)

| PR | Contenuto |
|---|---|
| PR1 | Firmware: ordine di registrazione GATT, advertising entro 31 B, contratto HR, contatore di perdita nel frame, `band-core` come subset reale, SQI firmware (rumore + init) |
| PR2 | Formati: writer FIT canonico nell'SDK, writer XDF 1.0 canonico nell'SDK, `synapse24.export`, conformità `fitdecode`/`pyxdf` |
| PR3 | Host: timestamp del frame, validator bounded (spool), transizioni di tier reali, motion gate alimentato |
| PR4 | Analisi: HRV su tempi assoluti, stream per tipo, SQI/MAP corretti, gating LF/HF |
| PR5 | Backend: token, allowlist CORS, confinement dei path, cache, IO non bloccante |
| PR6 | SDK: codec allineato al firmware, CLI reale (discover/info/record/inspect), suite nel gate |
| PR7 | Gate: test SDK, `check_interfaces`, conformità dei formati, drift degli artefatti del contratto, coverage estesa |
| PR8 | Documenti: verità delle claim, ADR-001, questo summary, `AGENTS.md` |

## Review indipendente e correzioni

L'incremento è stato sottoposto a due review indipendenti (un agente con
accesso al repository). I rilievi bloccanti e maggiori sono stati corretti
prima del merge:

| Rilievo | Correzione |
|---|---|
| **Blocker**: la macro generata `SYNAPSE_CONTRACT_UUID_X_BYTES` non era utilizzabile con `BLE_UUID128_INIT` (macro a 16 argomenti): il firmware non compilava | il generatore emette anche `_INIT` (la chiamata completa) e `_BYTES` (la lista, per gli inizializzatori a graffe); i call site usano `_INIT` |
| Il codec dell'SDK ignorava il flag `flags` e ammetteva frame da 256 byte (budget senza header) | il flag del caller arriva sul filo; il budget è `(244 - 12)` come nel firmware e nell'host; test di parità con i flag |
| Il motion gate era alimentato con il PPG **interleaved** (red/IR alternati): la SQI di un segnale pulito crollava e la promozione restava irraggiungibile | la finestra è de-interleaved (canale rosso) e l'accelerometro è allineato per campione; test di regressione |
| `band-core` linkava comunque il provisioning (e `esp_wifi`) | il provisioning è un no-op nel band-core (le chiamate sono compilate fuori, la characteristic rifiuta le scritture) e **non linka lo stack Wi-Fi**; i componenti opzionali restano nel grafo (la loro API è uno stub quando il flag è off) perché un grafo che decide diversamente dal compilatore rompe la build |
| La lunghezza del frame del research service contava i canelli due volte (byte letti fuori dal buffer, notifiche 2-4× troppo lunghe) | la lunghezza è header + valori × dimensione, come nel codec |
| Il contatore `dropped` mescolava valori e campioni | l'unità è il campione, in firmware, host e SOP |
| La coverage misurava `sdk/python` e `backend` senza eseguirne i test (79,55% < 80%) | la suite SDK è raccolta nel run di coverage: 84,58% |
| Il writer FIT del firmware non scriveva il messaggio `file_id` | il messaggio è scritto, con i numeri di campo del profilo |
| Il writer FIT del firmware non chiudeva il file in caso di errore (il pod abortiva) | il fallimento chiude il file, chiude la sessione e restituisce l'errore; l'acquisition non fa più `ESP_ERROR_CHECK` |
| Due encoder XDF (host e SDK) | l'host delega all'SDK, anche nel percorso di correzione dei timestamp |
| L'header XDF perdeva device/acquisition/tier | il writer dell'SDK li scrive quando il caller li fornisce |
| Il token del WebSocket era accettato nella query string | solo l'header `Authorization` |

## Cosa NON è in questo incremento (per design)

- L'acquisto del kit G1 e le misure di banco (il piano del G1 resta
  `docs/hardware/PROCUREMENT.md`).
- OTA firmato, provisioning con credenziali, bonding/LE Secure Connections:
  il provisioning resta disabilitato in `band-core` e la sua sicurezza è un
  incremento dedicato (post-G1).
- 24/7: il soak da 6 ore resta il gate; il 24 h è una claim di sistema (G3).
- Calibrazione scientifica delle soglie: le metriche corrette sono
  dichiarate, non tarate per far passare un dataset.

## Definition of Done

- [x] `ci-local` verde sul tree finale.
- [x] Un solo writer per ogni formato, e i formati sono letti da parser
      esterni (`fitdecode`, `pyxdf`) nel gate.
- [x] La suite dell'SDK e `tools/check_interfaces.py` sono nel gate.
- [x] Nessuna claim nei documenti che non sia coperta da un test o da un
      artefatto.
- [x] Ogni difetto P0 ha un test che fallisce se il difetto torna.

## Test aggiunti

`tests/test_format_conformance.py`, `tests/test_firmware_ble_contract.py`,
`tests/test_firmware_ppg_sqi_host.py`, `tests/test_check_interfaces.py`
(esteso), `tests/test_raw_frame_parity.py` (contatore di perdita),
`tests/test_sessions_catalog.py` (FIT reale vs finto),
`sdk/python/tests/{test_codec,test_cli,test_fit}.py` (riscritti),
`tests/test_signal_quality.py` (regressione rumore), `tests/test_worker_jobs.py`
(fixture sul formato canonico).

## File nuovi/modificati (sintesi)

Nuovi: `sdk/python/synapse_device/xdf_writer.py`,
`src/synapse24/acquisition/stream_spool.py`, `src/synapse24/export/`,
`src/synapse24/contracts_generated.py`,
`firmware/esp32_band/ble/synapse_contract_generated.h`,
`tools/generate_contracts.py`,
`docs/decisions/ADR-001-canonical-paths.md`,
`docs/increments/MVP1_VERIFIABLE_LOOP.md`, `tests/test_format_conformance.py`,
`tests/test_firmware_ble_contract.py`, `tests/test_firmware_ppg_sqi_host.py`,
`tests/test_contract_codegen.py`, `scripts/run_gate_locally.sh`.

Modificati: firmware (`ble_gatt_server.c`, `ble_research_service.c`,
`synapse_raw_frame.[ch]`, `app_main.c`, `main/CMakeLists.txt`, `ppg_sqi.cpp`),
host (`research_client.py`, `raw_codec.py`, `raw_ingest.py`, `utils/xdf.py`,
`sessions/catalog.py`), analisi (`signal_quality/ppg.py`, `signal_quality/ecg.py`),
worker (`hrv_worker.py`), backend (`main.py`, `service.py`), SDK
(`fit.py`, `xdf_writer.py`, `codec.py`, `record.py`, `cli.py`),
`scripts/{export_session_fit.py,validate_phase1_entry.py}`,
`tools/check_interfaces.py`, `.woodpecker/ci.yml`, `pyproject.toml`,
`uv.lock`, documenti.
