# Increment: MVP-1.9 — tutto ciò che si poteva fare prima di avere l'hardware

**Periodo:** 2026-11-14 · **Stato:** chiuso come **"la catena acquisizione → BLE → host / Averyn non perde né sposta campioni senza dirlo, il contratto è uno e il resto della documentazione dice il vero; nulla è stato provato su una scheda, un sensore o un telefono"**.

Un audit completo (firmware, host / SDK / contratti, documentazione) su `master` `d332cab` ha trovato ~55 problemi correggibili senza hardware. Otto PR (#91–#98), ognuna con review, gate e merge. Averyn non è stato toccato: riceve l'elenco di ciò che deve fare al re-pin (sezione "Per Averyn").

## Fatto

| PR | Cosa | Perché contava |
|---|---|---|
| **W1** #91 | **Integrità dei campioni.** Un buco nella sequenza di un sensore (anello o coda piena dello scheduler, overflow della FIFO del PPG, campioni scartati) arriva nel byte `dropped` del frame successivo, su un confine di frame; i timestamp del PPG non tornano mai indietro; l'IMU campiona al doppio della lettura (prima 48,9 Hz letti a 50: ~2 % di duplicati) con una sola lettura da 12 byte; via il calo del PPG a 16 Hz col moto; il livello di riposo dell'ECG è misurato una volta e congelato (prima era un passa-alto a 0,016 Hz applicato a bordo a uno stream "raw") | Prima l'host ricostruiva i tempi come se non si fosse perso nulla e ogni campione dopo un buco era spostato |
| **W2** #92 | **Link e flag.** MTU 247 (il default NimBLE era 256), 32 blocchi mbuf, richiesta di intervallo 15–30 ms, data length 251 e PHY 2M; la characteristic di feature v1 non esiste più (il percorso dell'HR standard resta); flag v2 `LEAD_OFF` e `CLOCK_WRAPPED` vivi; dopo un lead-off lungo il livello di riposo dell'ECG si rimisura; POWER: batteria −1 fuori da 3,0–4,4 V (l'USB leggeva 100 %); `reserve_mah` dalla configurazione | |
| **W3** #93 | **Igiene firmware.** PSRAM ottale per la N8R8 (la configurazione `quad` non l'avrebbe inizializzata al boot); lettura della configurazione da NVS (la versione salvata non veniva mai letta, e un blob rifiutato restava nella configurazione viva); task dei sensori sul core 1; coda campioni 256; slot EEG inutilizzato tolto (−16 KB di RAM interna); nome BLE dal Kconfig | |
| **W4** #94 | **Dati lato host.** `pyxdf` con il dejitter di default appiattiva ogni buco sotto 1 s: un frame perso a 500 Hz (0,13 s) spariva prima dell'export FIT e del worker HRV, e RMSSD e frequenza cardiaca nei file per Averyn erano sbagliati. Ora si carica senza dejitter e i picchi R prendono il tempo dai timestamp. Le unità sono **dichiarate** nell'XDF e lette da chi le usa (il report indovinava dal tipo: ECG "mV" per file scritti in µV); il writer XDF dell'SDK scrive i canali sotto `<desc>` come richiede lo standard; una risposta di sync a un'altra richiesta non entra più nel modello di clock; un frame ripetuto non viene rispinto; un manifest dei rate illeggibile solleva (prima un ECG a 500 Hz diventava un segnale a 1 MHz); `synapse record` riporta i frame persi nel collegamento | |
| **W5** #95 | **Contratto 2.3.** Una sola fonte della versione (`hardware.yaml`); le UUID del servizio research in `gatt_v2.json`; `check_interfaces` confronta le **coppie** stream→UUID in ogni copia scritta a mano (due characteristic scambiate passavano: ogni UUID era ancora da qualche parte nel file); vettori nuovi (metadati degli stream, control point accettato e rifiutato con il codice d'errore, Heart Rate Measurement, sequenza attraverso il giro dei 16 bit), tutti riprodotti byte per byte dal firmware C; `docs/contracts/AVERYN_INTERFACE.md` con le regole del frame raw, la tabella consenso→campo e l'elenco del re-pin | |
| **W6** #96 | **CI, dipendenze, codice morto.** `tensorflow-cpu` esce dalle dipendenze principali (extra `train`: ~10k file e 2+ GB, usato solo dall'addestramento); via gli ignore globali di F841 e PLW1510 (50 punti corretti); otto script senza riferimenti, il modulo `session.py` dell'SDK, `mypy.ini` e i due header duplicati eliminati; il dry-run di fase 1 giudica ciò che fa il setup (era una costante `True` che stampava PASS comunque); i sei test INA219 saltavano su Linux (simbolo non definito) e ora girano; i workflow GitHub sono un solo avviso, non due copie sfasate della pipeline | |
| **W7** #97 | **Catalogo e backend.** Il lettore FIT supera definizioni con dati sviluppatore, big-endian e messaggi con timestamp compresso; gli XDF su orologio reale hanno `started_at`/`ended_at`; i file non letti e i duplicati sono elencati in `skipped` (sparivano); l'id dei job segue il contenuto (era il percorso) | |
| **W8** #98 | Documentazione e questa chiusura (sotto) | |

### Documentazione (W8)

`HARDWARE_BRINGUP_GUIDE.md` eliminata (descriveva un firmware, una build e un esito PASS inventati); `HARDWARE_BRINGUP.md`, `INCREMENT_SUMMARY.md` e `SYNAPSE-AVERYN-STRATEGY/` in `docs/archive/`; la SOP è l'unico documento di procedura. Corretti: il gain di `FIRST_HARDWARE_DAY.md` (600 = gain 6, ~183×, non 1,8×), la SOP (INA219, pin di lead-off, "LO± a massa" che metteva in corto), PROCUREMENT (ICM-20948: la data di fine spedizione è passata), l'elenco delle licenze, i commenti di `synapse_config.h` (pin e gain vecchi), il README (non si dice più "kit orderable" né "ogni proprietà è provabile in CI"), la tabella dei progressi in `Roadmap.md`, e questo documento più `MVP1_7_VIRTUAL_BAND.md`.

## Decisioni del proprietario

Perimetro solo SYNAPSE-24; perdite nel byte `dropped` esistente; livello di riposo ECG congelato; unità dichiarate; IMU a frequenza doppia; link MTU 247 / DLE / 2M; v1 eliminata; `ecg_quality` non disponibile; TensorFlow come extra; codice morto eliminato; contratto 2.3 con nota di re-pin; ADC continuo con DMA **rinviato** a dopo il primo banco; **l'RR non va nella 0x2A37** (è sotto il consenso `rr_recording`: un'app generica di cardiofrequenza lo riceverebbe in chiaro; resta nel frame v2, dove il telefono lo maschera).

## Scostamenti dal piano approvato (e perché)

- **Periodo dello scheduler in µs:** non fatto; ogni frequenza in uso è un numero intero di millisecondi.
- **Flag di wrap del frame azzerato a ogni connessione:** lasciato, è voluto (un frame inviato prima della riconnessione non deve far sembrare un giro il primo dopo).
- **Magnetometro "solo se nuovo":** non fatto, condivide il campione dell'IMU.
- **IMU con controllo del data-ready:** sostituito da una frequenza al doppio, perché un `NOT_FOUND` sposterebbe la timeline (la sequenza numera solo i campioni consegnati).
- **Unità numeriche del percorso LSL dal vivo:** restano mV, g, rad/s, µT, perché l'immobilità thresholda l'accelerometro in g; sono **dichiarate** nell'XDF e lette da chi le usa. I file di `synapse record` usano le unità del contratto (µV, mg, dps, µT).
- **Lettura a burst del MAX30102** (~1–2 % del bus I²C, non provabile qui), **ruoli central/observer di NimBLE** (`ble_gattc_exchange_mtu` potrebbe richiedere il ruolo central), **file di sincronizzazione C++ non compilati** (li usa un test ESP-IDF), **opzioni Kconfig "inutilizzate"** (`check_contract` le lega a `hardware.yaml`): non fatti.
- **Un solo modello di clock e il ritiro di `esp32_tier0`:** non fatti; `clock_sync.py` e `esp32_tier0.py` servono ai gate della fase 0 e a una dozzina di test, e non toccano i dati dalla band all'app.
- **`ecg_quality` fisso a 0,8:** sul filo era già non disponibile (0xFF); il segnaposto vive solo nel modulo di qualità, che non si pubblica.

## NON provato

- **Niente scheda, niente sensore, niente radio, niente telefono, niente iOS.** Tutto ciò che sta sopra è provato sull'host (C compilato con gcc nel container Linux, 0 skip) e con un `band-core` ESP-IDF compilato da zero; non è provato che la band faccia ciò che il codice dice.
- **`band-full` e la variante con RR acceso non sono stati compilati** (memoria della macchina); li compila la CI Woodpecker, che non è mai stata letta.
- Le modifiche al link BLE (parametri di connessione, data length, 2M) sono **richieste** che il telefono può rifiutare: il comportamento reale va visto al banco.
- Il livello di riposo dell'ECG congelato può saturare l'ADC se la linea di base deriva molto (il lead-off lungo → rimisura lo mitiga solo se il pin LO+ è cablato).
- Tutto ciò che il piano dichiarava fuori perimetro: ADC continuo con DMA, PPG oltre 50 Hz, filtri IMU, rotazione del magnetometro, pull-up I²C esterni, gestione dell'energia, throughput BLE reale, avvio con PSRAM ottale, gain ECG, RMSSD del PPG (riga 6), MVP-1.7 PR-4 (dongle), iOS (Mac), tutte le modifiche ad Averyn.

## Per Averyn (al re-pin)

L'elenco completo, con gli sha256, è nella sezione "Dalla 2.2 alla 2.3" di `docs/contracts/AVERYN_INTERFACE.md`: nuovi hash, UUID del servizio research in `gatt_v2.json`, characteristic v1 sparita, flag `lead_off` e `clock_wrapped`, canale batteria di POWER = −1 come "non disponibile", `ecg_quality` sempre 255, MTU 247. Le regole del frame raw e la tabella consenso→campo sono nello stesso file.

## Debito residuo

- **Firmware:** ADC ECG continuo con DMA; PPG a più di 50 Hz (il DSP è tarato a 50); filtri IMU e rotazione del magnetometro; letture a burst del MAX30102; ruoli central/observer; sincronizzazione C++ non compilata; opzioni Kconfig inutilizzate; il modulo Edge AI (spento) ha ancora riferimenti rotti; FIT on-device, provisioning CSR, modello di potenza, qualità ECG reale.
- **Host:** `esp32_tier0.py` e il doppio modello di clock; i test che richiedono TensorFlow ora saltano nel gate (un passo dedicato con `--extra train` li farebbe girare).
- **Averyn** (invariato, `MVP1_6_PHONE_SIDE.md`): build iOS, consenso raw tardivo su Android, I/O dei sidecar fuori dal thread dei callback, schermata dei segnali dal vivo, export e DPIA dei sidecar.
- **Verifica:** HIL con la band; lettura di Woodpecker; compilare `band-full` e RR acceso; una prova su un telefono vero; WESAD a mano per il PPG da polso in movimento.

## Prossimi passi

1. Il primo giorno con la scheda (`docs/hardware/FIRST_HARDWARE_DAY.md`): `sdkconfig` pulito, rumore e gain con `g1_measure.py`, e **guardare** se i campioni persi arrivano davvero in `dropped`.
2. Un dongle BLE per MVP-1.7 PR-4; un Mac per iOS.
3. Il re-pin di Averyn.
4. Se la deriva della linea di base satura l'ADC al banco: tornare sulla decisione del livello di riposo congelato.

## Verifica di questo incremento

Ogni PR: gate statico (ruff, mypy ×3, `check_contract`, `generate_contracts --check`, `check_interfaces`), suite completa a nove blocchi nel container Linux (0 test falliti; copertura cumulativa 83,5–83,9 % su `src`, `worker` e `backend`), test host C con gcc senza saltati dove la modifica li riguardava, e per le PR firmware un `band-core` ESP-IDF 5.5.5 compilato da zero. La review automatica (`code-review`) ha trovato difetti veri in ogni PR (documentati nei commenti delle PR) e sono stati corretti prima del merge.
