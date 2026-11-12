# Increment: MVP-1.8 — segnale reale registrato nella catena della band

**Periodo:** 2026-11-12 · **Stato:** chiuso come **"ECG e PPG reali registrati provati nella catena C del firmware sull'host; nessun sensore, nessuna scheda, nessuna radio"**. Il PPG ha dato un risultato negativo utile (RMSSD) che tiene spenta la funzione RR.

## Perché non il piano originale

Il piano prevedeva "ECG quality in C su MIT-BIH". Leggendo il firmware non esiste: `synapse_signal_quality.c` ignora i campioni ECG e restituisce una qualità fissa a 0,8 (segnaposto). L'ECG esce senza DSP (500 Hz, s16) e lo analizza il telefono; il valore nel frame è già in **µV riferiti all'elettrodo**, calcolati dal firmware con il gain configurato (`ecg_ad8232.cpp`, `ble_research_service.c`). Una prima versione della replay assumeva count ADC sul filo: era sbagliato ed è stato corretto dopo aver letto quel percorso. La prova sensata su un ECG reale è quindi: *dopo l'ADC e il framing, i battiti si trovano ancora?* Il PPG invece ha DSP vero in C (`synapse_rr.c`, che nell'header dice "non ha mai visto un dito vero").

Il piano prevedeva anche WESAD per il PPG. **Non è ottenibile in automatico**: il mirror UCI risponde 404 e il download è manuale (licenza da accettare dal proprietario). Al suo posto è stato usato **BIDMC** (PhysioNet, ODC-BY): PPG da dito e ECG sincroni a 125 Hz, 53 record da 8 minuti. È il compromesso: vedi "NON provato".

## Fatto

- `scripts/real_signal_replay.py` (ECG): mV a 360 Hz → 500 Hz → front end (gain, mid-rail, ADC a 12 bit con i suoi limiti) → il calcolo del firmware `(mV ADC − mid-rail) / SYNAPSE_ECG_GAIN` e s16 in µV (come `ecg_ad8232.cpp` e `ble_research_service.c`) → frame dall'encoder **C** (`synapse_raw_frame.c`) → decoder SDK → picchi R (NeuroKit, lo stesso del baseline) → Se/PPV contro le annotazioni, prima e dopo la catena. Distingue il gain *vero* del front end da quello *configurato* nel firmware.
- `scripts/real_ppg_replay.py` (PPG): PLETH 125 → 50 Hz → count IR riflettivi (il count scende sul polso) → `synapse_rr_push` in C (stesso glue di `test_firmware_s5_rr.py`) → HR e RMSSD per finestre da 30 s contro gli intervalli R-R dell'ECG nelle stesse finestre. RMSSD calcolato con la stessa regola per band ed ECG: solo intervalli adiacenti e puliti (±20 % della mediana). Con la regola ingenua l'RMSSD risultava gonfiato anche dove i due concordano.
- `scripts/g1_measure.py`: i due numeri della BRINGUP_SOP §4a, dal flusso ECG che l'host riceve (µV; `.npy` o l'XDF del validatore, stream `SYNAPSE_ECG_T0`): rumore con ingresso in corto e gain da una sinusoide nota, già nel formato da scrivere in `CONFIG_SYNAPSE_ECG_GAIN`. Rifiuta flussi saturati o senza la sinusoide. Nessuna soglia pass/fail: né la SOP né READINESS_GATE ne definiscono, una soglia inventata sembrerebbe un requisito.
- `docs/hardware/FIRST_HARDWARE_DAY.md`: cosa manca prima di una prova con hardware reale per chi parte da zero (strumenti che PROCUREMENT dà per posseduti, la trappola del `sdkconfig` locale con `CONFIG_SYNAPSE_ECG_GAIN=600`, sequenza del primo giorno).
- Test: `tests/test_real_signal_chain.py` (9), `tests/test_real_ppg_chain.py` (11), `tests/test_g1_measure.py` (8). Fixture versionate (PhysioNet, ODC-BY, ritagli senza elaborazione): `tests/fixtures/mitbih_ecg_30s.npz` (23 KB), `tests/fixtures/bidmc_ppg_150s.npz` (115 KB), così il gate prova qualcosa senza scaricare nulla.
- `READINESS_GATE.md`: il "fail closed" di Known failures era già fatto (`validate_baseline.py` esce con 2, test esistente); mancava solo il dato WESAD. Il baseline WESAD resta aperto e non entra qui.

## Numeri misurati

**ECG** (60 s, gain vero e configurato 1100×): il framing è bit-esatto e senza buchi; i timestamp dei frame sono il clock dei campioni.

| Record MIT-BIH | Battiti | Se/PPV originale | Se/PPV dopo la catena |
|---|---|---|---|
| 100 | 74 | 0,987 / 1,000 | 0,987 / 1,000 |
| 105 | 83 | 1,000 / 1,000 | 0,988 / 1,000 |
| 108 | 58 | 0,345 / 0,351 | 0,379 / 0,407 |

Nel 105 la catena perde 1 battito su 83 e satura lo 0,2 % dei campioni (il QRS supera i circa ±1,3 mV che lascia un gain di 1100×); con un clone a 2200× (1,3 % saturato nel 100) i battiti si trovano ancora. Un `SYNAPSE_ECG_GAIN` sbagliato scala l'ampiezza sul filo di 1100/valore e non cambia i battiti (test). Il 108 è già difficile per il rilevatore sull'originale e non dice nulla sulla band.

**PPG** (BIDMC, 480 s, 16 finestre da 30 s per record, `synapse_rr.c` com'è):

| Record | HR: errore mediano | HR entro 5 bpm | Intervalli band / battiti ECG | RMSSD band / ECG (mediana) |
|---|---|---|---|---|
| 01 | 0,17 bpm | 100 % | 697 / 730 | 10,5 / 7,9 ms |
| 02 | 0,50 bpm | 100 % | 675 / 728 | **39,3 / 6,8 ms** |
| 03 | 0,09 bpm | 100 % | 583 / 612 | 6,6 / 5,9 ms |
| 04 | 0,50 bpm | 100 % | 669 / 738 | 9,2 / 8,1 ms |

Il rilevatore, scritto e tarato su impulsi sintetici, trova l'HR giusto su polso reale in tutte le 64 finestre confrontate (4 record × 16). L'RMSSD concorda su 3 record su 4; **sul 02 è circa cinque volte quello dell'ECG**.

Il 02 **non è un difetto dei filtri del firmware**, come avevo scritto in una prima versione: un rilevatore offline a fase zero (picco del segnale filtrato, picco sulla banda 0,5-8 Hz, massima pendenza in salita), a 50 e a 125 Hz, dà ~40 ms sul 02 (la band 39; l'ECG 6,8). Il punto di massima pendenza lo porta a ~19 ms ma non all'ECG. Quindi la variabilità intervallo-intervallo di *quel* polso è davvero diversa da quella dell'ECG con ogni metodo provato; la causa (fisiologia del tempo di transito, morfologia, altro) **non è stabilita**: una sonda sul ritardo ECG→polso non ha dato abbastanza battiti accoppiati per concluderlo. Esperimento esplorativo, non versionato. Per questo `CONFIG_SYNAPSE_RR_ENABLE` resta spento fino a un riferimento su un polso vero; il test `test_known_limit_ppg_rmssd_differs_from_ecg_on_record_02` fissa il comportamento.

## NON provato

- Un MAX30102 o un AD8232: BIDMC è un ossimetro **trasmissivo** su pazienti a letto, non un sensore riflessivo a polso o dito con la sua corrente LED, luce ambientale e movimento. `BASE` e `PULSE_COUNTS` (PPG), il range dell'ADC (3,1 V) e il mid-rail (1,65 V) sono valori nominali. Gain, offset, rumore e lead-off dell'ECG li misura `g1_measure.py` sulla scheda.
- WESAD (PPG da polso in movimento): resta il banco di prova più vicino alla band e non è stato eseguito. Si scarica a mano da UCI #465 (`data/wesad/WESAD/S2/S2.pkl`).
- La causa dell'RMSSD del 02 (vedi sopra): non stabilita; non è nei filtri del firmware.
- La radio (MVP-1.7 PR-4, dongle), iOS (Mac), il classificatore di moto, il config write BLE.
- Su Windows Application Control blocca a intermittenza la DLL compilata: i test C host si saltano e vanno ripetuti (qui al 1°–4° tentativo). Uno skip non è un pass.

## Riprodurre

Dati BIDMC (ODC-BY, non versionati; `data/bidmc/` è in `.gitignore`):
`curl -O https://physionet.org/files/bidmc/1.0.0/bidmc_csv/bidmc_01_Signals.csv` (e `_Numerics.csv`, per i record 01–53). Poi `uv run python scripts/real_ppg_replay.py --record 01` e `scripts/real_signal_replay.py --record 100` (MIT-BIH in `data/mitbih/`).

## Prossimi passi

1. Il riferimento per l'RMSSD sul banco (riga 6 di G1) e, su più record BIDMC o su WESAD, se la discrepanza del 02 è fisiologia o morfologia.
2. WESAD a mano per il PPG da polso in movimento.
3. Con un dongle BLE: MVP-1.7 PR-4. Con una scheda: `docs/hardware/FIRST_HARDWARE_DAY.md`, poi `g1_measure.py noise` e `gain` e `ecg_config.gain_measured`.
