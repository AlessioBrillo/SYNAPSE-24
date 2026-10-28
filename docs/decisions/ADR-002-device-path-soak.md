# ADR-002 — Percorso dispositivo soak-ready: clock, MTU, rate e claim

**Status:** accepted · **Data:** 2026-10-27 · **Incremento:** MVP-1.2 (percorso dispositivo canonico e soak-ready)

## Contesto

Alla chiusura di MVP-1 il loop era verificabile nel research client, ma la
CLI dell'SDK — la via normale per un utente del device — era ancora in
parte uno stub (`info` stampava una batteria finta, `record --source
<addr>` restituiva `return 2`), e la semantica del wrap dell'orologio a
32 bit del pod era troppo ampia (N5). Preparando il soak G1 (6 h) queste
scelte diventano rischi fisici: un file che dichiara una rate che non ha
misurato, un'ancora temporale ambigua o un "wrap" che non è un wrap
rovinano una sessione di sei ore prima che chi la esegue se ne accorga.

## Decisioni

1. **Codec e superficie di trasporto: una sola implementazione (Q7).**
   Il codec del frame raw vive in `sdk/python/synapse_device/codec.py`
   (specchio del firmware `synapse_raw_frame.c`, provato da
   `tests/test_raw_frame_parity.py`); `src/synapse24/hardware/raw_codec.py`
   è un re-export puro per i percorsi di import legacy. I consumer di
   contratto (`tools/check_contract.py`) leggono il canonico, mai lo shim.
2. **Estensione dell'orologio: flag + regressione ristretta, mai
   qualunque passo indietro (N5).** Un frame marcato con
   `FLAG_TS_EPOCH_WRAPPED` ha attraversato il roll: una regione in più.
   Un passo indietro **senza** flag è un roll solo quando copre
   ≥ `(1<<32) - 900_000_000` µs (il riordino di consegna che un link BLE
   può produrre è dell'ordine dei minuti; il contatore a 32 bit in µs
   riempie in ~71,6 min). I passi dentro il contatore sono ritardi di
   consegna: il clamp monotono li gestisce nella timeline, non come
   regioni. `_pod_now_us` fornisce un controllo di plausibilità e
   `set_wrap_base` non sovrascrive `_last_us`. `seed_from_sync` crea un
   estensore per ogni stream wire tranne SYNC: ogni stream ha la sua
   regione, e SYNC non ne ha bisogno (non porta stamp).
3. **Timestamp della CLI record: "pod-relative, wall-anchored", senza
   sync exchange (Q2).** La prima delivery dà l'ancora wall; ogni sample
   è `ancora + (device_us − pod_origin)·1e⁻⁶`, spaziato intra-frame di
   `arange(n)/rate_misurata`. La calibrazione NTP host↔pod resta a
   MVP-1.4 (`ClockExchange` nell'SDK). Motivo: il G1 misura zero drop e
   timeline monotone, non la corrispondenza bidirezionale degli orologi;
   riscrivere lo stamp del pod con un offset **non** calibrato sarebbe
   più disonesto che dichiarare l'ancora.
4. **Rate dichiarate = rate misurate.** La rate per stream è la mediana
   `campioni_per_frame·1e⁶/Δdevice_us` sui boundary che portano campioni.
   Con un solo frame non c'è Δ: la rate dichiarata è 0.0 — LSL
   "irregolare" — e mai la nominale configurata travestita da misura.
5. **MTU riportato, mai assunto (Q5).** `request_mtu(247)` ritorna ciò
   che il link concede oppure `None`; il summary del record e l'output
   di `info` distinguono "concesso: N" da "unknown". Con
   `mtu_target=None` (scelta esplicita) la negoziazione viene proprio
   saltata. Il floor del report del gate resta "richiesto", non
   "ricevuto".
6. **Un solo writer per il soak: LabRecorder (Q3).** Le run di prova
   (`--soak-rehearsal`) eseguono lo stesso percorso di codice ma su
   spool sintetico, e si marcano nell'artefatto (`rehearsal: true`,
   `source: "spool (synthetic rehearsal)"`): non possono mai essere
   presentate come evidenza G1. `--soak` e `--soak-rehearsal` sono
   mutuamente esclusivi dalla CLI.
7. **Il claim dei baseline è a tre stati (Q6/PR4).** `verified` (numeri
   rimisurati su questo host e passanti), `partial` (qualcosa misurato
   fuori da questo host, niente su di esso), `not_run` (nessun
   artefatto). La coerenza con i documenti è forzata da
   `tools/check_baseline_claim.py` nel gate: con `partial` i documenti
   possono parlare solo di target, mai di risultati.
8. **Restart del firmware segnalato con il tipo, non per deduzione
   magica (Q4).** I frame del pod portano il flag di wrap; un riavvio
   tra due regioni è visibile dalla regressione ristretta del punto 2.
   L'host non inventa "salto = riavvio" senza una prova sul filo.
9. **Bonding/crittografia: sospeso (Q9).** Il research service gira
   senza bonding per il MVP: il G1 misura perdita e timeline, e il
   bonding aggiungerebbe una variabile radio non richiesta dal gate.
10. **FIT on-device differito (Q8).** L'output di `record` è XDF;
    l'export FIT resta lato host finché l'app Averyn non ne ha bisogno
    (Averyn legge FIT — vedi ADR-001).

## Alternative scartate

- **Sync exchange nella CLI record.** Scartato per il MVP: sarebbe stato
  un protocollo in più non ancora validoato sul banco (MVP-1.4), e il G1
  non lo richiede. La scelta del punto 3 mantiene onesto il MVP.
- **Rate nominale nel file XDF.** Scartato: un file che annuncia 500 Hz
  e contiene 50 Hz sposta la bugia sul consumatore; la rate misurata può
  essere 0.0 nel caso peggiore, il che è vero e non inganna.
- **Un estensore di clock globale per tutti gli stream.** Scartato: lo
  stream SYNC non porta stamp e i data stream possono arrivare
  disancorati fra loro; un estensore per stream (punto 2) rende il wrap
  un fatto locale, gestito correttamente stream per stream.
- **Pattern del claim necessariamente benigni** (lasciare "✅ PASS" nelle
  tabelle di target). Scartato: il checker regredirebbe a un marcatore di
  frasi; le frasi di risultato sono esattamente quelle che la struttura
  dell'artefatto deve autorizzare.

## Conseguenze

- `sdk/python/tests/test_record_ble.py`, `tests/test_pod_time_wrap.py` e
  `tests/test_research_ble_client.py` pinano i punti 2, 3, 4 e 5.
- `tools/check_baseline_claim.py` nel gate impedisce ai documenti di
  promettere ciò che l'artefatto non dichiara (punto 7).
- MVP-1.4 eredita `ClockExchange` per rendere calibrato il punto 3; il
  passo OTA segue G1; il bonding resta esplicitamente rimandato dal
  piano.
