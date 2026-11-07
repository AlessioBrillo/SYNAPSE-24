# Increment: MVP-1.5 — i segnali della band in Averyn

**PR:** #78, #79, #81, #82 in questo repository (#80 chiusa, superata da #81); #161–#165 in Averyn · **Periodo:** 2026-11-06 → 2026-11-07 · **Stato:** chiuso come **"pronto per l'hardware, non verificato"**; la parte server e web è fatta, **la parte telefono non è fatta**.

## Decisioni del proprietario (2026-11-06)

- Nessuna scheda: fixture generate dall'encoder dell'SDK, niente HIL.
- Prima vista: lo storico web. Live e schermate mobili vengono dopo.
- Il contratto v1 si ritira (nessuna band v1 è mai esistita); ADR-0030 diventa accepted con RR sotto `rr_recording`.
- Eccezione dichiarata all'ordine di lavoro di Averyn: il contratto si fissa finché è gratis.
- Sequenza avanzata solo su notify accettato; tutte le correzioni firmware high e medium; rilevatore RR con refrattario adattivo.

## Cosa l'audit ha trovato (e che adesso è corretto)

| Dove | Difetto | Esito |
|---|---|---|
| Firmware, `ble_research_service.c` | Con l'accelerometro iscritto e gyro/mag no, dopo ~2,6 s si scriveva oltre i loro buffer (corruzione di memoria); `ring_overflow` contava ~650 «perdite» al secondo senza iscritti | F1 (#78) |
| Firmware | Un notify rifiutato bruciava il numero di sequenza: per l'host una perdita che non c'era | F2 (#79) |
| Firmware, RR | Gli intervalli RR sparivano se il frame v2 non partiva; il refrattario fisso a 300 ms prendeva la dicrota per un battito; una mediana avvelenata all'avvio rifiutava ogni battito vero per ~30 battiti; nessuna scadenza | F3 (#81) |
| Firmware | `0x2A37` leggibile a 65535 bpm o con il valore del dito già staccato; CHARGING senza il bit POWER; TEMP/POWER mai in `sensor_health`; GPS/TMP117 assenti abortivano il boot; `tier` fisso a 0 | F4 (#81) |
| Firmware | I tier contavano tick da 10 ms: «5 minuti di immobilità» passavano in 100 ms; SQI del PPG senza sincronizzazione | F5 (#81) |
| CI GitHub | `band-full` ricompilava band-core; uno step «unit tests» che non poteva fallire | F6 (#81) |
| Host | Un frame ripetuto o tardivo era letto come riavvio e i suoi campioni accodati due volte; un salto in avanti enorme passava per riavvio | H1 (#82) |
| Worker | Battiti a campione intero: ~14 ms di errore su RMSSD a 50 Hz | H2 (#82) |
| Contratto | `sport_type` solo nell'header v1; hash dei contratti diverso su Windows (CRLF); nessun file di sessione del telefono | C1 (#82) |
| Averyn | Bit di contatto di `0x2A37` letti al contrario; un byte RR dispari buttava via l'intera notifica; frazioni divise per 255 invece di 254; nessuno scope, nessun sidecar, nessuna vista | A1–A8 |

## Fatto

**Questo repository.** F1–F6 (firmware), contratto 2.2 (`gatt_v2.json`: v1 *retired*, `sport_types`, regola di sequenza; `.gitattributes` LF), le fixture del telefono `contracts/fixtures/session_60s.*` con la risposta attesa e `scripts/make_band_sidecars.py`, il decoder host con la regola del contratto, i battiti del worker interpolati, `AGENTS.md` allineato alla pipeline, il README del backend vero.

**Averyn.** `shared/sensors`: parser del feature frame v2.2, dello status, dei frame raw, tabella degli stream con le unità dal contratto, control point, formato dei sidecar, lettore di sessione (53 test); `backend`: scope `signal_quality` / `stress_monitoring` / `raw_signals`, sidecar `features`/`raw`, revoca che cancella (intera, o solo i campi coperti, con tombstone), lettura per finestre min/max (22 test nuovi, 159 totali, su PostgreSQL e S3 reali); `apps/web`: la sezione «Segnali della band» con zoom e pan, buchi come interruzioni e come tabella, i cinque consensi nell'account (49 test); ADR-0030 accepted, `synapse.md`, `metrics/synapse-series.md`, TDD-0017.

## Verifica: che cosa è stato eseguito e che cosa no

**Eseguito (sull'albero finale di ciascuna PR):** lint, tipi, `check_contract`, `generate_contracts --check`, `check_interfaces`; la suite completa a blocchi da 5 file (coverage sul perimetro del gate `src`+`worker`+`backend`: 83,7 %); la suite SDK (88 %); il probe gcc; i test C sull'host **nel container Linux, senza skip** (150 passati); in Averyn `:shared:sensors:check`, `:backend:check` (Testcontainers), `npm run check` e `npm run build`. Quasi ogni correzione ha un test che fallisce sul codice vecchio. Le PR di questo repository sono passate per `/code-review`; **quelle di Averyn (#161–#165) no**.

**NON eseguito:** nessuna scheda, nessuna band; nessun telefono; nessun browser (la pagina non è mai stata aperta: layout, rendering della banda uPlot e interazione sono provati solo dai tipi e dalle funzioni pure); nessun build ESP-IDF (il firmware è stato compilato solo dal probe gcc dei sorgenti dell'host e dai test C); nessun check Woodpecker letto; i check GitHub falliscono in 1–2 secondi senza eseguire nulla (fatturazione) e non sono un segnale; l'iOS di Averyn non è stato compilato.

**Nota sulla coverage:** `coverage report` senza filtro dà ~68 %, perché con `--cov-append` il totale include `sdk/python` a 0 %. Una corsa singola con `--cov=src --cov=worker --cov=backend` (il comando del gate) non lo include.

## Scostamenti dal piano

- Nessuna tabella `devices` in Averyn: nulla la scriverebbe ancora (niente scaffolding in anticipo).
- La piramide min/max è costruita alla prima lettura e tenuta in una cache in memoria, non all'ingest né persistita.
- `research_client.py` è stato corretto sul posto, non riscritto sull'SDK (LSL e coordinator lo usano); il `PodClockModel` inutilizzato dell'SDK è stato cancellato.
- La caratteristica feature v1 resta nel firmware: toglierla senza poter compilare è un rischio senza prova. Il contratto la dichiara *retired*.
- F4 è parziale: mancano LEAD_OFF dai pin LO±, CLOCK_WRAPPED, l'RR in `0x2A37`, una sentinella per la batteria dello stream POWER fuori batteria. F5: la FSM non ha un harness host, sono provate le regole di finestra, non il comportamento.
- La sessione di fixture dura 60 s, non 5 minuti.
- **Il lato telefono (A5/A6) non è stato fatto**: né lo store dei sidecar sul telefono, né l'upload, né gli adapter Android e iOS.

## Debito dichiarato

- **Averyn:** adapter Android (`requestMtu(247)`, `set_time`, consensi riletti durante la registrazione) e iOS; store e sync dei due sidecar (raw solo su Wi-Fi); schermata dei segnali live; viste mobili su asse temporale reale; i sidecar non sono ancora nell'export dell'utente né nella DPIA; i consensi `rr_recording` e `hrv_analysis` del feature frame dipendono dal mascheramento sul telefono prima di scrivere (da fare con l'adapter); persistenza della piramide; ricomputo dello stato di `ConsentScope` su iOS/Android.
- **Firmware:** rimozione della caratteristica v1 e di `g_feat_seq`; LEAD_OFF, CLOCK_WRAPPED, RR in `0x2A37`; batteria POWER fuori batteria; i gruppi di `sdkconfig` senza effetto e `MSYS_1_BLOCK_COUNT`; cppcheck su `firmware/common`; FIT on-device, Edge AI, provisioning CSR, IMU senza FIFO, rotazione del magnetometro, qualità ECG reale, modello di potenza.
- **Host:** 36 F841 (l'ignore globale resta); `esp32_tier0.py` (è vivo: lo importa il registry); la copia radice di `firmware/triage_features.h`; gli stub 501 del backend (bloccati da test); `make_consumer_vectors.py` scrive ancora `contract_version: "2.1"` nei vettori (da portare a 2.2 insieme al re-pin di Averyn); le UUID della research service vivono ancora solo in `gatt_v1.json` (da spostare in `gatt_v2.json`).
- **Verifica:** HIL col kit (guadagno ECG, frequenze, RR contro un ECG di riferimento, soak di 1 h, budget di banda misurati), le righe S29–S31 e S49–S50 della matrice di Averyn, boot test in QEMU, lettura di Woodpecker, una prova della pagina in un browser con una sessione di fixture caricata.

## Prossimo incremento

1. Il lato telefono di Averyn: store e sync dei sidecar, adapter Android (con il nucleo testabile in JVM), poi iOS.
2. Aprire la pagina in un browser con la sessione di fixture e correggere ciò che si vede.
3. HIL col kit; leggere Woodpecker; poi il debito del firmware, a partire da ciò che richiede la scheda.
