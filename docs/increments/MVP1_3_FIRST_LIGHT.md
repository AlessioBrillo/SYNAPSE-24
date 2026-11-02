# Increment: MVP-1.3 — First light e contratto consumer v2

**PR:** #55–#64 (tutte unite su `master`) · **Periodo:** 2026-10-29 → 2026-11-02 · **Scope:** firmware della band, SDK, worker, backend, contratti e documentazione di cablaggio (nessun acquisto hardware) · **Stato:** chiuso come **"pronto per l'hardware", non "verificato"**.

> **Rinumerazione.** `MVP1_2_DEVICE_PATH.md` assegnava a MVP-1.3 il provisioning GATT. Questo incremento ha preso il numero perché l'audit ha mostrato che il firmware non poteva ancora avviarsi: il provisioning (solo CSR) slitta a **MVP-1.4**, insieme al FIT on-device.

## Obiettivo

Portare la band da "compila e passa un gate che ne stubba i pezzi rotti" a "il codice è corretto per quanto si può dire senza una scheda": che si avvii, trasmetta dati sensati e li descriva in modo che una UI non debba indovinare nulla. In parallelo, chiudere le incoerenze di contratto con Averyn e togliere dal repository ciò che mentiva.

La misura di successo era verificabile solo a metà: ogni difetto trovato ha un test che fallisce se torna (molti verificati per mutazione), ma **nessun test ha girato su una scheda**.

## Che cosa è stato fatto

| PR | Contenuto |
|---|---|
| #55 | Cancellati i target firmware legacy (`esp32_forearm/head/tier0/headband`), `ble_lsl_bridge`, `power_monitor`, il client BLE legacy e i comandi di provisioning Wi-Fi/cloud (percorso diretto device→cloud, rifiutato da ADR-0014). |
| #56 | **First light (P0).** Puntatore nullo alla prima registrazione di un sensore, task dei sensori mai creati, ADC1 aperto due volte, handle GATT che erano codici d'errore (feature stream, HR e batteria muti), IMU con il magnetometro irraggiungibile e abort su ogni errore I²C, FIFO del PPG mai abilitato, pin su flash/PSRAM, Edge AI che avrebbe corrotto la memoria (spenta). |
| #57 | **Correttezza dei dati.** ECG saturo (bias AD8232 non tolto), campioni PPG/IMU scartati per un controllo di tempo globale, testimone di wrap non azzerato, POWER a 100 Hz invece di 1 Hz, `pod_send` del SYNC alla scrittura, RR in 1/1024 s come da standard. |
| #58 | **Contratto consumer v2.** Quattro characteristic nuove (Feature v2, Status, Stream Metadata, Control Point) generate da `config/hardware.yaml`; encoder C puri verificati byte per byte contro `synapse_device.consumer` e `gatt_v2.json`; v1 congelato (sha256 fissato da Averyn); frame v1 misurato in **23 byte**, non 26; checker UUID per intero. |
| #59 | Worker/export: `unknown` non è un'etichetta di stress, `window_start` sempre in secondi Unix, una sola regola per le finestre HRV, rilevatore PPG dedicato, enum del sonno W/N1/N2/N3/REM + 255. |
| #60 | Backend: 404 veri, nessun percorso assoluto in risposta, `/ws/live` autenticabile da browser, bind su loopback, Dockerfile che installa il server. |
| #61 | Viewer locale: feed live (sorgente sintetica dichiarata demo, o BLE), perdite con il motivo, pagina `/live` senza risorse esterne. |
| #62 | mypy anche su `worker/` e `backend/`, import inutilizzati di nuovo segnalati, guardie perché non si spengano. |
| #63 | Documenti di cablaggio allineati alla mappa dei pin del firmware (27/34/36 erano flash/PSRAM); `READINESS_GATE` e `POWER_BUDGET` dicono ciò che non è misurato. |
| #64 | `AVERYN_INTERFACE.md` riscritto secondo il codice e gli ADR di Averyn; README e roadmap corretti; il tripwire della baseline intercetta la riga della roadmap che prima sfuggiva. |

## Decisioni prese

- **v1 congelato, v2 a parte.** `contracts/gatt_v1.json` non si modifica mai (Averyn ne fissa lo sha256); le novità sono characteristic nuove. Un client v1 non le vede.
- **Nessun dato inventato.** Ogni campo "non disponibile" è una sentinella e si legge `null`; la sorgente live è `off` salvo configurazione esplicita, e `synthetic` si dichiara demo.
- **Visualizzazione raw in locale** (viewer in SYNAPSE-24). Averyn usa solo L0/L1 e non chiama il Data Plane (ADR-0026).
- **Una definizione per cosa.** L'enum del sonno (ADR-0014), l'offset FIT→Unix, le finestre HRV e la mappa dei pin hanno ciascuno una sola fonte, con test che li confrontano.
- **Il kit non c'è ancora:** l'incremento si chiude "ready for HIL".

## Verifica: che cosa è stato eseguito e che cosa no

**Eseguito** (gate locale su host, `scripts/run_gate_locally.sh` nei suoi passi, a blocchi; ultimo risultato sull'incremento): ruff, mypy (`synapse24`, SDK, `worker`, `backend`), `check_contract`, `generate_contracts --check`, `check_interfaces`, `check_baseline_claim`, dry-run Phase 1; suite principale con coverage **84,38%** (soglia 80); suite SDK **88,67%**; probe gcc nel container del gate; test C compilati su host (nativamente quando Windows lo consente, altrimenti nel container Linux).

**NON eseguito, e non va dato per fatto:**

- **Nessuna esecuzione su scheda.** Il codice NimBLE (`ble_gatt_server.c`), `app_main.c`, i driver ECG/PPG/IMU e il modulo power non sono compilati da nessun gate host: per loro ci sono solo guardie statiche, che provano che le correzioni ci sono, non che la band misuri.
- **Nessun build ESP-IDF** in questa macchina (il container va in OOM) e il boot test in QEMU non è nel pipeline.
- **Il check Woodpecker (`ci/woodpecker/push/ci`) non è mai comparso** sui commit di questo incremento: ogni "verde" è locale.
- **Nessun telefono** ha usato le characteristic v2; **nessun browser** ha caricato `/live`; la sorgente `ble:` non è stata provata con una band.
- **Nessuna scheda è stata cablata** con i documenti corretti; le cifre di consumo restano un limite superiore stimato.

### Come eseguire il gate su questa macchina

La run unica viene fermata per memoria bassa. Si esegue a pezzi: passi leggeri uno per uno, la suite principale a blocchi di 10 file con `--cov-append` (poi `coverage report --fail-under=80`), la suite SDK, il probe gcc. Attenzione ai **test saltati**: Windows Application Control blocca a intermittenza le DLL non firmate che i test C compilano, e uno skip non è un pass. Quei test vanno eseguiti nel container Linux (`ghcr.io/astral-sh/uv:python3.11-bookworm`, dipendenze del `pyproject.toml`, `MSYS_NO_PATHCONV=1` con Git Bash). Docker Desktop va avviato a mano.

## Cambi di contratto da sapere

- `contracts/fit_developer_fields.json`, campo `synapse_sleep_stage`: cambia il **significato** dei valori 1–4 (ora N1/N2/N3/REM, 255 = non disponibile); gli id restano immutati. Nessun consumatore noto leggeva il campo.
- `window_start` nei payload dei job è sempre in secondi Unix.
- Il frame feature v1 è di 23 byte (documentazione e ADR-0026 di Averyn dicono 26).
- Il campo RR di `0x2A37` è in 1/1024 s: **il parser di Averyn lo legge ancora come millisecondi** (vedi sotto).
- API: gli endpoint di sessione rispondono 404 (non 200 con un corpo d'errore), `current_tier` è `null`, `sessions_dir` non c'è più, `/ws/live` inizia con `hello`, i percorsi sono relativi, `/api/live` e `/api/control/*` rispondono 501.
- `SYNAPSE_PPG_SAMPLE_RATE` è ora il codice SPO2_SR del datasheet (`0x00` = 50 Hz); i pin di default sono cambiati (lead-off 15, wired sync 10).

## Debito dichiarato (non fatto)

- **Cross-repo Averyn (repo non toccato, serve un via libera esplicito):** parser RR in `HeartRateFrameParser.kt` (ms → 1/1024 s), ADR-0026 (26 → 23 B), re-pin a `gatt_v2.json`, scope di consenso `signal_quality` / `stress_monitoring`.
- **Firmware:** SQI del PPG (rimozione DC, finestra sul ring, lock), qualità ECG ancora fissa a 0,8, motion gate per finestre, contatori di perdita per motivo, temperatura reale, ISR del wired sync (conta solo nel ruolo POD), nome di advertising unico per unità, parametri di connessione / PHY / DLE, pairing, tier e hash di build nello Status, flag `lead_off` e `clock_wrapped` riservati.
- **Edge AI** spenta per default: l'interprete è costruito dentro la propria arena e corromperebbe la memoria; i sorgenti restano compilati.
- **Sequenza su notify rifiutato:** l'audit la dava per gap fantasma, ma i test esistenti la dichiarano scelta di progetto; da decidere.
- **Software:** il report non include i risultati dei job; catalogo (id di sessione stabile da sha256, `started_at` degli XDF, walker FIT, ancoraggio di t0 nell'export, unità dai `channel_units`); `esp32_tier0.py` ancora presente (va prima spostata `forearm_hub_normative_rates`); ~40 variabili inutilizzate (F841) negli script di bring-up hardware; il rilevatore PPG non ha rigetto degli artefatti.
- **Doc:** la riga "Phase 1 READY TO START" della roadmap non dice se aspetta la baseline di Phase 0.

## Prossimo incremento (proposta: MVP-1.4)

1. **Hardware in the loop** non appena arriva il kit: boot log, un frame per stream, frequenze effettive, bias ECG, soak di 1 h (`BRINGUP_SOP.md`). Sono la prima prova reale di tutto ciò che qui è solo "pronto".
2. Leggere davvero il check Woodpecker e aggiungere il boot test in QEMU al pipeline.
3. Il lavoro **cross-repo con Averyn** elencato sopra.
4. Provisioning CSR e FIT on-device (rimandati da MVP-1.2).
5. Il debito software di questa pagina, nell'ordine: catalogo, rimozione di `esp32_tier0.py`, SQI/qualità ECG.
