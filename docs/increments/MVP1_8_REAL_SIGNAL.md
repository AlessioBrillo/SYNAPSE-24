# Increment: MVP-1.8 — segnale reale registrato nella catena della band

**Periodo:** 2026-11-12 · **Stato:** chiuso come **"ECG e PPG reali registrati provati nella catena C del firmware sull'host; nessun sensore, nessuna scheda, nessuna radio"**. Il PPG ha dato un risultato negativo utile (RMSSD) che tiene spenta la funzione RR.

## Perché non il piano originale

Il piano prevedeva "ECG quality in C su MIT-BIH". Leggendo il firmware non esiste: `synapse_signal_quality.c` ignora i campioni ECG e restituisce una qualità fissa a 0,8 (segnaposto). L'ECG esce **grezzo** (500 Hz, s16) e lo analizza il telefono. La prova sensata su un ECG reale è quindi: *dopo l'ADC e il framing, i battiti si trovano ancora?* Il PPG invece ha DSP vero in C (`synapse_rr.c`, che nell'header dice "non ha mai visto un dito vero").

Il piano prevedeva anche WESAD per il PPG. **Non è ottenibile in automatico**: il mirror UCI risponde 404 e il download è manuale (licenza da accettare dal proprietario). Al suo posto è stato usato **BIDMC** (PhysioNet, ODC-BY): PPG da dito e ECG sincroni a 125 Hz, 53 record da 8 minuti. È il compromesso: vedi "NON provato".

## Fatto

- `scripts/real_signal_replay.py` (ECG): mV a 360 Hz → 500 Hz → count ADC 12 bit con clipping → frame dall'encoder **C** del firmware (`synapse_raw_frame.c`) → decoder SDK → picchi R (NeuroKit, lo stesso del baseline) → Se/PPV contro le annotazioni, prima e dopo la catena.
- `scripts/real_ppg_replay.py` (PPG): PLETH 125 → 50 Hz → count IR riflettivi (il count scende sul polso) → `synapse_rr_push` in C (stesso glue di `test_firmware_s5_rr.py`) → HR e RMSSD per finestre da 30 s contro gli intervalli R-R dell'ECG nelle stesse finestre. RMSSD calcolato con la stessa regola per band ed ECG: solo intervalli adiacenti e puliti (±20 % della mediana). Con la regola ingenua l'RMSSD risultava gonfiato anche dove i due concordano.
- `scripts/g1_measure.py`: i due numeri della BRINGUP_SOP §4a (rumore in count con ingresso in corto; count per mV da una sinusoide nota). Nessuna soglia pass/fail: né la SOP né READINESS_GATE ne definiscono.
- Test: `tests/test_real_signal_chain.py` (7), `tests/test_real_ppg_chain.py` (11), `tests/test_g1_measure.py` (6). Fixture versionate (PhysioNet, ODC-BY, ritagli senza elaborazione): `tests/fixtures/mitbih_ecg_30s.npz` (23 KB), `tests/fixtures/bidmc_ppg_150s.npz` (115 KB), così il gate prova qualcosa senza scaricare nulla.
- `READINESS_GATE.md`: il "fail closed" di Known failures era già fatto (`validate_baseline.py` esce con 2, test esistente); mancava solo il dato WESAD. Il baseline WESAD resta aperto e non entra qui.

## Numeri misurati

**ECG** (60 s, 132 count/mV): il framing è bit-esatto e senza buchi; i timestamp dei frame sono il clock dei campioni.

| Record MIT-BIH | Battiti | Se/PPV originale | Se/PPV dopo la catena |
|---|---|---|---|
| 100 | 74 | 0,987 / 1,000 | 0,987 / 1,000 |
| 105 | 83 | 1,000 / 1,000 | 0,988 / 1,000 |
| 108 | 58 | 0,345 / 0,351 | 0,379 / 0,407 |

Nel 105 la catena perde 1 battito su 83 (identico a 40, 132 e 400 count/mV: non è la quantizzazione). Il 108 è già difficile per il rilevatore sull'originale e non dice nulla sulla band.

**PPG** (BIDMC, 480 s, 16 finestre da 30 s per record, `synapse_rr.c` com'è):

| Record | HR: errore mediano | HR entro 5 bpm | Intervalli band / battiti ECG | RMSSD band / ECG (mediana) |
|---|---|---|---|---|
| 01 | 0,17 bpm | 100 % | 697 / 730 | 10,5 / 7,9 ms |
| 02 | 0,50 bpm | 100 % | 675 / 728 | **39,3 / 6,8 ms** |
| 03 | 0,09 bpm | 100 % | 583 / 612 | 6,6 / 5,9 ms |
| 04 | 0,50 bpm | 100 % | 669 / 738 | 9,2 / 8,1 ms |

Il rilevatore, scritto e tarato su impulsi sintetici, trova l'HR giusto su polso reale in tutti i 64 intervalli confrontati. L'RMSSD concorde su 3 record su 4; **sul 02 è sopra di circa cinque volte**. Per questo `CONFIG_SYNAPSE_RR_ENABLE` deve restare spento: il test `test_known_limit_rmssd_is_wrong_on_record_02` fissa il limite e va invertito quando il rilevatore migliora.

## NON provato

- Un MAX30102 o un AD8232: BIDMC è un ossimetro **trasmissivo** su pazienti a letto, non un sensore riflessivo a polso o dito con la sua corrente LED, luce ambientale e movimento. `BASE`, `PULSE_COUNTS` e `COUNTS_PER_MV = 132` sono valori di ripiego. Gain, offset, rumore e lead-off dell'ECG li misura `g1_measure.py` sulla scheda.
- WESAD (PPG da polso in movimento): resta il banco di prova più vicino alla band e non è stato eseguito. Si scarica a mano da UCI #465 (`data/wesad/WESAD/S2/S2.pkl`).
- Il perché del 02: non indagato (battiti ectopici, onda dicrotica o soglia adattiva). Va guardato con la traccia davanti prima di toccare le soglie.
- La radio (MVP-1.7 PR-4, dongle), iOS (Mac), il classificatore di moto, il config write BLE.
- Su Windows Application Control blocca a intermittenza la DLL compilata: i test C host si saltano e vanno ripetuti (qui al 1°–4° tentativo). Uno skip non è un pass.

## Riprodurre

Dati BIDMC (ODC-BY, non versionati; `data/bidmc/` è in `.gitignore`):
`curl -O https://physionet.org/files/bidmc/1.0.0/bidmc_csv/bidmc_01_Signals.csv` (e `_Numerics.csv`, per i record 01–53). Poi `uv run python scripts/real_ppg_replay.py --record 01` e `scripts/real_signal_replay.py --record 100` (MIT-BIH in `data/mitbih/`).

## Prossimi passi

1. Capire il 02 sulla traccia e, se è il rilevatore, correggere `synapse_rr.c`; poi invertire il test di caratterizzazione.
2. WESAD a mano per il PPG da polso in movimento.
3. Con un dongle BLE: MVP-1.7 PR-4. Con una scheda: `g1_measure.py noise` e `gain`, poi `ecg_config.gain_measured`.
