# ADR-001 — Un solo percorso canonico per il device layer e per i formati

**Status:** accepted · **Data:** 2026-10-20 · **Incremento:** MVP-1 (loop verificabile)

## Contesto

Il repository aveva tre implementazioni dello stesso wire format e due
implementazioni dello stesso loop:

| duplicato | implementazione A | implementazione B | conseguenza |
|---|---|---|---|
| codec del frame raw | `src/synapse24/hardware/raw_codec.py` | `sdk/python/synapse_device/codec.py` | layout dei byte divergente: l'SDK non era mai stato confrontato con il firmware |
| writer FIT | `scripts/export_session_fit.py` | `sdk/python/synapse_device/fit.py` | nessuno dei due produceva un file FIT leggibile da un parser standard |
| "XDF" | `sdk/python/synapse_device/record.py` (h5py) | `src/synapse24/utils/xdf.py` (chunk XDF 1.0) | il primo scriveva un file che `pyxdf` non legge |
| export XDF→FIT | `scripts/export_session_fit.py` | `worker/hrv_worker.py` (parser FIT privato) | la perdita veniva letta in due modi diversi, e quello del worker leggeva finestre vuote |
| CLI della sessione | `synapse` (SDK) | `synapse-sessions` / `synapse-report` (host) | i comandi dell'SDK erano stub `return 2`, dichiarati come "implementati" |

Il sintomo era il gate verde: i test passavano perché testavano l'una o
l'altra implementazione, mai entrambe contro la stessa specifica, e mai
contro un parser esterno.

## Decisione

1. **Il device layer (Apache-2.0) vive nell'SDK.** Codec, transport, clock e
   i lettori/scrittori XDF e FIT sono implementati una sola volta in
   `sdk/python/synapse_device/`, e `synapse24`, `worker` e `scripts` ne
   dipendono (`synapse-device` è una path dependency del progetto). La
   direzione dei dipendimenti non si inverte: l'SDK non importa mai
   `synapse24` (licenza AGPL), quindi non può duplicare l'analisi.
2. **Il percorso canonico dell'host è `src/synapse24`.** Analisi (HRV,
   qualità), catalogo, report e worker stanno lì. I comandi sono
   `synapse-sessions`, `synapse-report`, `synapse-export`.
3. **L'SDK espone solo il device layer.** La sua CLI fa `discover`, `info`,
   `record` e `inspect`. I comandi `export`, `index`, `report` e `analyze`
   sono stati spostati nel pipeline dell'host: la CLI dell'SDK esce con
   codice 2 e dice dove sono andati, invece di fingere di funzionare.
4. **Un formato è un formato solo se un parser esterno lo legge.** Il FIT è
   validato da `fitdecode`, l'XDF da `pyxdf`, in
   `tests/test_format_conformance.py` (nel gate).
5. **Il contratto dei campi FIT ha una sola fonte.** La tabella dei campi
   sviluppatore vive nell'SDK; `tools/check_interfaces.py` verifica che
   corrisponda a `contracts/fit_developer_fields.json` e che export e worker
   non ne conservino copie locali.

## Alternative scartate

- **Tenere l'SDK come "SDK v0 completo" (con export/index/report/analyze).**
  Scartato: implicherebbe o una seconda implementazione dell'analisi
  (nella direzione di licenza sbagliata) o una dipendenza dell'SDK da
  `synapse24` (AGPL dentro un pacchetto Apache-2.0). Le due opzioni sono
  incompatibili con la scelta di licenza già documentata in
  `DEVICE_LOOP_V1.md`.
- **Generare il codice del contratto invece di condividere il modulo.**
  Scartato per ora: il generatore introdurrebbe un artefatto generato da
  tenere in sincronia senza eliminare la classe di bug (che era nel
  *contenuto* del contratto, non nella sua duplicazione). Il codegen
  completo resta un'opzione per l'incremento post-G1.

## Conseguenze

- Il gate esegue anche i test dell'SDK (`sdk/python/tests`): la suite
  dell'SDK non era mai stata eseguita dal CI, ed è il motivo per cui un
  secondo writer FIT e un codec divergente sono sopravvissuti.
- `tools/check_interfaces.py` è entrato nel gate: prima esisteva ma non
  veniva eseguito.
- L'API del Data Plane ora ha un token opzionale e un confine esplicito
  per i percorsi: i claim sulla sicurezza del piano dati sono verificabili.
