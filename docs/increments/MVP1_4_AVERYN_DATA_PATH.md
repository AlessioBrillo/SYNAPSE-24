# Increment: MVP-1.4 — percorso dati verso Averyn

**PR:** #66–#76 in questo repository (S1–S9, S5, S10 parziale), #151 in Averyn · **Periodo:** 2026-11-02 → 2026-11-06 · **Stato:** chiuso come **"pronto per l'hardware, non verificato"**; la parte Averyn non è fatta.

## Decisioni del proprietario

- Nessuna scheda disponibile: tutto si prova con fixture generate dall'encoder dell'SDK.
- **Tutta la visualizzazione e l'analisi visiva dei dati della band vive solo in Averyn.** Qui restano firmware, contratti, vettori e test; il viewer `/live` è stato rimosso.
- Contratto consumer v2 corretto prima che qualcuno lo fissasse (**v2.1**); pin di Averyn una volta sola.
- RR/HRV dal PPG dietro flag, spenti finché il banco non li conferma. Stress nascosto finché `null`; Edge AI resta spenta.

## Fatto

| PR | Contenuto |
|---|---|
| #66 | Contratto 2.1: `motion_artifact` (il `ppg_map` della v1) in coda, un'unica definizione dei bit di `flags`, sentinelle per `stress_level`/`sport_type`, unità e scala per campo, `display_unit`/`display_per_wire` per stream, `set_time`, `ppg_skipped`/`power_dropped` nello Status; `raw_frame_v1.json` con vettori; tabella SPO2_SR corretta. |
| #67 | Un percorso con lettera di unità è rifiutato fuori da Windows (due test fallivano su Linux anche su master). |
| #68 | Guadagno ECG 1100 (con 6 ogni QRS saturava), HR standard indipendente dal feature stream (L0), NaN/negativi = non disponibile, HR arrotondato, dato PPG stantio non riportato, batteria mai inventata. |
| #69 | Il build "band-core" di Woodpecker ora compila davvero il profilo core (variabile e `sdkconfig`). |
| #70 | Viewer `/live` e `/ws/live` rimossi. |
| #71 | `set_time` fino a `settimeofday`; finestra notturna solo con orologio impostato; FIFO PPG drenato per intero con timestamp ricostruiti; perdite dello scheduler e contatori nello Status; richieste di reset atomiche al posto dei reset dal task NimBLE; budget di banda rispettati. |
| #72 | `contracts/fixtures/band_scenarios_v1.json`: sette scenari raw e una sessione L1 con risposta attesa. |
| #73 | Provisioning: lunghezza verificata, esito propagato, scrittura cifrata; la scrittura di configurazione non è più accettata e ignorata. |
| #74 | Unità GYRO/MAG in `synapse record`, segno del clock SDK, cache del fit, id di sessione dal contenuto, export senza data inventata, HRV dai timestamp con rigetto degli artefatti, `contracts/` nell'immagine del worker. |
| #75 | Rilevatore di battiti PPG in C puro (`synapse_rr.c`) dietro `CONFIG_SYNAPSE_RR_ENABLE`; terzo build di CI col flag acceso. |
| #76 | Cablaggio ECG corretto (RA = IN−, LA = IN+, RL è un'uscita; LO± sono uscite), log attesi reali, note datate sulle affermazioni false di MVP-1.3, banner "superato" sulla cartella strategia. |
| Averyn #151 | ADR-0030 (proposto): contratto 2.1 e livello L1R con scope `raw_signals`; roadmap, matrix S49–S50. |

## Verifica: che cosa è stato eseguito e che cosa no

**Eseguito:** per ogni PR il gate locale a pezzi (ruff, mypy, `check_contract`, `generate_contracts --check`, `check_interfaces`, suite a blocchi da 5 file con coverage 83,3–84,5 %, SDK, probe gcc) e i build ESP-IDF reali dei check GitHub (band-full, band-core, e con RR acceso). Il job `Baseline Validation` fallisce per il download di WESAD ed è fuori dal gate.

**NON eseguito:** nessuna scheda; il check Woodpecker non è mai stato letto; il rilevatore RR ha visto solo impulsi sintetici; nessun telefono ha usato le characteristic v2; i budget di banda e i flush sono calcolati, non misurati.

## Debito dichiarato

- **Averyn (A2–A7):** fix del parser (bit di contatto di `0x2A37`, scala `/254`, byte RR dispari, `requestMtu`), parser del contratto 2.1 e dei frame raw con re-pin e sha256, scope `signal_quality`/`stress_monitoring`/`raw_signals`, tabella `devices`, endpoint del sidecar, schermate Android, iOS e web.
- **Firmware:** FIT on-device (base dei tempi e record di sessione), Edge AI, provisioning CSR, IMU senza FIFO, rotazione assi del magnetometro, qualità ECG reale, modello di potenza, SQI del PPG senza mutex, `firmware/triage_features.h` divergente dalla copia compilata, notify rifiutato che consuma la sequenza (scelta di progetto da decidere), restart rilevabile solo se il contatore precedente supera 64.
- **Software:** `esp32_tier0.py`, F841 negli script di bring-up, `AGENTS.md` da riallineare alla sequenza del gate.

## Prossimo incremento

1. Averyn A2–A7 (PR accoppiate, contratto già pronto).
2. HIL con il kit: guadagno ECG, frequenze, RR contro ECG di riferimento (SOP §4a punto 5), soak di 1 h.
3. Leggere Woodpecker e aggiungere il boot test in QEMU.
