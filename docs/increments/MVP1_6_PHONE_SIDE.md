# Increment: MVP-1.6 — il lato telefono della band in Averyn (Android)

**PR:** #84 in questo repository; #167, #168, #171 in Averyn · **Periodo:** 2026-11-07 · **Stato:** chiuso come **"Android scritto e provato sulla JVM, non verificato su band né telefono"**; iOS non fatto.

## Decisioni del proprietario (2026-11-07)

- Perimetro Android + iOS; iOS si scrive qui e si compila sul Mac del proprietario (la PR resta aperta fino ad allora).
- I consensi si applicano **sul telefono prima di scrivere** (fail closed), non solo alla revoca sul server.
- I segnali grezzi salgono su rete non a consumo; su dati mobili solo se l'utente lo sceglie (default spento).
- L'adapter della band è una classe separata: `SensorAdapter` non cambia, l'HR passa da lì, i byte vanno al recorder.
- UI nelle impostazioni esistenti, nessun flusso di pairing guidato.

## Fatto

**Questo repository (#84).** `make_consumer_vectors.py` scriveva ancora `contract_version: "2.1"`: ora 2.2, vettori rigenerati. Averyn li ha re-pinnati.

**Averyn.**
- **#167** `shared/sensors`: `SynapseConsents`, `maskSynapseFeatureFrame` (RR, HRV della band e stress a "non disponibile" senza il loro consenso, stessa regola di `maskFeatureSidecar` del server, con un test di parità su 8 combinazioni), `SynapseRecorder` (feature/status solo con `signal_quality`, raw solo con `raw_signals`; `consents` volatile). Rigiocando `session_60s.*` nel recorder i due file escono identici byte per byte.
- **#168** `shared/tracking`, `shared/sync`, `shared/client`: `ActivityStore` con i due sidecar e i tetti del server; `ActivitySync` li carica dopo l'attività (**204 = successo**: l'upload dell'attività risponde 200/201, copiare l'RR avrebbe segnato ogni upload come fallito); 403/410 permanenti con cancellazione locale, 507 permanente `QUOTA_EXCEEDED`, 404/429/5xx/rete da ritentare, 413/422 permanenti; il raw solo con `rawUploads`; `deleteLocal` cancella anche i sidecar.
- **#171** `apps/android`: `SynapseBleAdapter` (scan sull'UUID del servizio consumer che la band annuncia, `requestMtu(247)`, `GattStepper` = un'operazione GATT alla volta, `set_time` a ogni connessione, raw avviato o fermato secondo il consenso), `HrSensor` (fascia e band intercambiabili per `TrackingService`), `BandPrefs`, cinque consensi in cache, `SyncWorker` con regola sulla rete a consumo, impostazioni EN + IT.

Tre review (agente) hanno trovato e fatto correggere: `consents` non volatile (#167); orfani di 52 MB dopo la cancellazione e 507 ritentato all'infinito (#168); una perdita del consenso RR cambiando la band on/off, un hang senza risposta all'MTU, due `start()` in corsa e `t_rx_ns` che torna a zero dopo un riavvio del processo (#171).

## Verifica: che cosa è stato eseguito e che cosa no

**Eseguito.** Gate di questo repository sul PR #84: passi leggeri (ruff, mypy, contratti, interfaces, baseline, phase1), suite completa a 18 blocchi (83,73 % sul perimetro del gate), SDK, probe gcc, test C host nel container Linux (88 passati, nessuno skip). Averyn: `:shared:sensors:check`, `:backend:check` (Testcontainers), `:shared:tracking/sync/client:check`, `:apps:android:compileDebugKotlin`, `testDebugUnitTest`, `ktlintCheck`, `lintDebug`.

**NON eseguito.** Nessuna band e nessun telefono: la sequenza GATT di Android (MTU, CCCD, scritture del control point) segue il contratto, non un'osservazione. Nessun build ESP-IDF né Woodpecker. Nessun browser. iOS non scritto né compilato. I check di GitHub falliscono per fatturazione e non sono un segnale: le PR di Averyn sono state mergiate con `--admin` dopo il gate locale.

## Scostamenti dal piano

- Le UUID della research service non sono state spostate in `gatt_v2.json` (tocca generatore, checker e il pin di Averyn, e non serve al telefono).
- `AverynClient.putSynapse` non esiste: il sync usa il proprio `HttpClient`. `consents()` restituiva già tutti i grant.
- Il recorder non usa `SynapseRawSession` per i contatori: senza schermata live nessuno li leggerebbe.
- Un frame di versione ignota sul canale delle feature viene scritto non mascherato, come fa il server (non se ne conosce il layout).
- Un consenso raw concesso a registrazione in corso vale alla connessione successiva.
- Il sidecar sale in un unico corpo in memoria (fino a 48 MB), come l'attività e l'RR.

## Debito dichiarato

- **Averyn:** adapter iOS e il suo build; schermata dei segnali live; viste mobili; sidecar nell'export dell'utente e nella DPIA; flusso di pairing guidato; revoca del raw a metà `setUp` lascia i flussi accesi sulla radio (non scrive nulla); `try/finally` attorno a un `connectGatt` annullato nell'istante prima di `currentGatt`; upload raw a pezzi; nuovo tentativo del raw quando torna una rete non a consumo senza un nuovo `enqueueSync`.
- **Questo repository:** invariato rispetto a MVP-1.5, più le UUID research in `gatt_v2.json`.
- **Verifica:** HIL con la band, una prova su un telefono reale (annuncio dell'UUID del servizio, MTU negoziato, ordine delle sottoscrizioni), la pagina web aperta con una sessione vera.

## Prossimo incremento

1. iOS: `SynapseBand.swift` con lo stesso flusso, interruttore e upload; si compila sul Mac (`xcodegen`, `xcodebuild`, `iosSimulatorArm64Test`).
2. Prova su telefono reale con la band (o con un secondo telefono che simula il GATT) e correzione di ciò che si vede.
3. Export e DPIA dei sidecar; schermata live.
