# Increment: MVP-1.2 — Percorso dispositivo canonico e soak-ready

**Branch:** `feat/device-loop-v1` · **Scope:** software-only + firmware research service (nessun acquisto hardware) · **Gate:** `bash scripts/run_gate_locally.sh` = la stessa sequenza di `.woodpecker/ci.yml`

## Obiettivo dell'incremento

Portare il percorso device dell'SDK da "funziona nel sintetico e nel
research client" a **percorso canonico e pronto al soak G1**: la CLI
normale (`info` / `record`) usa lo stesso codec del firmware, la stessa
superficie Transport, la stessa estensione clock del research client, e
dichiara solo quello che ha misurato. Nessuna feature analytics: la CLI
non fa sync exchange, gli stamp del pod restano "pod-relative,
wall-anchored", e la calibrazione host↔pod resta a MVP-1.4
(`ClockExchange`).

La misura di successo, verificabile:

> `synapse info --address <pod>` e `synapse record --source <pod>`
> esistono davvero: il probe legge la telemetria POWER reale, la
> registrazione scrive un XDF 1.0 letto da `pyxdf`, dichiarando la rate
> **misurata** dagli stamp del pod (0.0 = irregolare, mai assunta) e
> l'MTU **grantito** dal link (unknown, mai il target).

## Difetti trovati e correzioni

| # | Difetto trovato | Correzione | Prova |
|---|---|---|---|
| M-1 | Tre superfici di codec/transport divergenti: `raw_codec.py` duplicava il codec del firmware e l'SDK portava il suo XDF | codec unico in `sdk/python/synapse_device/codec.py` (specchio del firmware provato da parità byte a byte, decisione Q7), `src/synapse24/hardware/raw_codec.py` ridotto a re-export puro | `tests/test_raw_frame_parity.py`, `tests/test_format_conformance.py` |
| M-2 | `tools/check_contract.py` leggeva lo shim `raw_codec.py` per la parità del frame raw: dopo lo spostamento del canonico nell'SDK il check greppava un file che non dichiara più niente, e tutto passava vuoto | il contract check legge il codec canonico (`sdk/python/synapse_device/codec.py`); il messaggio di errore nomina il modulo reale | `tests/test_contract_checker.py`, `CONTRACT OK: 100 checks passed` |
| M-3 | La CLI presentava l'MTU come il target costante (247): una bugia quando il link ne concede un altro | `request_mtu()` ritorna il valore **grantito** o `None` (non sapere ≠ non dichiarare); il target resta la costante di `config/hardware.yaml` | `sdk/python/tests/test_transport.py` |
| M-4 | N5 — il wrap del contatore 32-bit interpretava qualunque passo indietro come roll: un frame consegnato in ritardo poteva sembrare un'epoca nuova | witness numerico ristretto: il flag conta da solo, e un passo indietro è wrap **solo** se copre ≥ `(1<<32) - 900_000_000` µs; i passi dentro il contatore sono riordino per il clamp monotono; `_pod_now_us` di plausibilità; `set_wrap_base` senza override di `_last_us`; `seed_from_sync` crea un clock per ogni stream wire tranne SYNC | `tests/test_pod_time_wrap.py` (6/6), `tests/test_research_ble_client.py`, `sdk/python/tests/test_record_ble.py::test_wrap_straddle_keeps_the_timeline_monotonic` |
| M-5 | La CLI inventava i dati: `info` stampava "Battery: 100% (Charging: No)" senza nessun pod, `record --source <addr>` era uno stub `return 2` | `info` statico onesto (facts SDK, battery MAI stampata senza un pod); `--address` = probe reale su POWER (connect → MTU → subscribe → un frame entro timeout), exit 1 quando il pod è assente o muto; `record --source <addr>` = `record_ble()` reale (MTU → subscribe → capture → XDF → summary) | `sdk/python/tests/test_cli.py`, pin `"battery: NOT probed here"` |
| M-6 | `record_synthetic`/`record_ble` dichiaravano nel file la rate nominale richiesta, non quella prodotta | rate **misurata** dai delta degli stamp device del pod (mediana sui boundary che portano campioni), 0.0 = LSL "irregolare"; timestamps: base per frame (= ancora wall + delta device − pod_origin) + `arange(n)/rate` intra-frame; rate non misurabile → base condivisi per frame e nominal 0 nel file | `sdk/python/tests/test_record_ble.py::test_measured_rate_and_wall_anchored_timestamps`, `::test_mtu_target_none_skips_the_negotiation` |
| M-7 | `record.py` portava una seconda copia dei UUID GATT | una sola fonte: `transport.CHAR_UUID_TO_STREAM` → `_GATT_RAW_CHARS` / `_RECORD_UUIDS` (i 5 stream di `STREAM_META`; POWER è telemetria del probe `info`, SYNC appartiene al modello clock); seam `_build_transport` (alias di `transport.build_transport`) monkeypatchabile, `record_ble(transport=…, mtu_target=…)` iniettabili | `sdk/python/tests/test_record_ble.py::test_every_capture_uuid_is_unsubscribed` |
| M-8 | Un XDF corrotto restava sul disco come "sessione" | round-trip duro: `pyxdf` rilegge il file appena scritto; conteggio campioni ≠ atteso → `XdfRoundTripError`, nessuna sessione finta | `sdk/python/tests/test_record_ble.py`, `tests/test_format_conformance.py` |
| M-9 | Un pod muto produceva il silenzio, non un errore | nessun frame → `BleRecordError` con la causa (pod spento, service off, MTU), e **nessun file** | `sdk/python/tests/test_record_ble.py::test_no_frames_raises_and_leaves_no_file` |

## PR4 — Verità del claim (consistenza artefatto ↔ documenti)

1. `tools/check_baseline_claim.py` riscritto: **tre stati** del claim
   (`verified` / `partial` / `not_run`) letti da `claim_level` nell'artefatto
   `docs/baselines/phase0_results.json` (schema 1.1; artefatti legacy
   inferiti). `verified` richiede `verified_on_host` vero **e** numeri che
   passano i target (WESAD ≥80%, MIT-BIH Se/PPV ≥99.6%, RMSSD MAE <5 ms,
   Sleep-EDF κ ≥ floor); `partial` e `not_run` vietano ogni documento che
   dichiara risultati (tripwire: heading "Exit Gate Results … COMPLETE",
   status "Phase 0 Status … COMPLETE", "All exit criteria met", righe di
   tabella dei risultati con `✅ PASS`). Artefatto corrotto = exit 2.
2. `Roadmap.md` e `README.md` allineati: la tabella Phase 0 è "targets,
   not re-verified here", "Phase 0 Status: **open**", checklist README non
   barrata, riga di stato "code complete; baseline claims partial".
3. `tools/check_contract.py`: scan **tree-wide** del vecchio UUID a
   gruppi dash (malformato: `UUID.fromString`/Java lo rifiuta) con skip
   di archivi/build/strategy-dir e senza catturare la letterale
   `0x53594E41` (versione applicativa "SYNA"); letterali FIT
   `SYNAPSE_DEV_FIELD_*` contro `contracts/fit_developer_fields.json`
   **in entrambe le direzioni** (nessun nome non dichiarato in un
   consumatore; nessun `c_name` mancante da `fit_writer.h`; la macro
   derivata `SYNAPSE_DEV_FIELD_DEFINITION_NUM` è whitelista).
4. `scripts/run_gate_locally.sh`: `tests/test_firmware_fit_writer_host.py`
   aggiunto allo step firmware host; rimossa una doppia esecuzione
   ridondante di `sdk/python/tests`; `tests/test_gate_parity.py` pin
   l'equivalenza comando-per-comando tra pipeline e script locale.

## PR5 — CLI reale (info/record)

- **`info`**: senza `--address` stampa solo facts SDK (stream, formato
  frame, superficie GATT, MTU target) con la riga "battery: NOT probed
  here"; `--address <pod>` esegue il probe POWER: connect →
  `request_mtu(MTU_TARGET)` → subscribe `CHAR_POWER` → primo frame entro
  `_POWER_PROBE_TIMEOUT_S` (default 3 s, costante modulo, monkeypatchabile)
  → decode dei quattro canali `voltage_mv / current_ma / power_mw /
  battery_pct` (scala MILLI = mV/mA/mW/%) → unsubscribe → disconnect.
  `TransportError` o frame indecodificabile → exit 1 con il messaggio del
  transport.
- **`record`**: `--source synthetic` gira `record_synthetic` (ora attraverso
  lo stesso percorso del BLE: decode → estensione clock → `_xdf_payload` →
  round-trip `pyxdf`); ogni altro valore è `record_ble(address, out,
  duration, transport=…, mtu_target=…)` — MTU negoziato e riportato,
  5 stream di sessione, ancore wall alla **prima** delivery, nessun sync
  exchange.
- `tests/test_record_ble.py`: pod finto che consegna frame **reali**
  (`encode_frame`) con schedule wall-clock; pin su rate misurata ≠ assunta,
  ancora wall, straddle del wrap monotono, `dropped` dai frame, MTU
  `None` = nessuna negoziazione, cleanup degli unsubscribe.

## PR6 — Documenti

Questo documento, `docs/decisions/ADR-002-device-path-soak.md`,
l'allineamento di `docs/hardware/BRINGUP_SOP.md` §5 alle due modalità
soak (`--soak` reale, `--soak-rehearsal` marcata) e l'allineamento del
claim (Roadmap, README).

## Cosa NON include (per decisione esplicita)

- **Provisioning GATT** (servizio di pairing) — MVP-1.3.
- **OTA / aggiornamento firmware** — dopo G1.
- **FIT on-device** — MVP-1.4 (l'output di `record` resta l'XDF).
- **UI Averyn** — piano dedicato.
- **Sync exchange nella CLI** — MVP-1.4 (vedi ADR-002).
- **Dataset reali / ri-run dei baseline**: il claim resta `partial` finché
  `scripts/validate_baseline.py` non gira su un host con dataset verificati.
