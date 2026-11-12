# Increment: MVP-1.8 — segnale reale registrato nella catena della band (lato ECG)

**Periodo:** 2026-11-12 · **Stato:** **parziale**. Provato: ECG reale MIT-BIH → ADC a 12 bit → encoder C del firmware → decoder SDK → picchi R. **Non fatto: la catena PPG/RR su segnale reale** (manca WESAD sul disco). Nessun sensore, nessuna scheda, nessuna radio.

## Perché non il piano originale

Il piano prevedeva "ECG quality in C su MIT-BIH". Leggendo il firmware non esiste: `synapse_signal_quality.c` ignora i campioni ECG e restituisce una qualità fissa a 0,8 (segnaposto). L'ECG esce **grezzo** (500 Hz, s16) e il telefono lo analizza. Quindi la prova sensata su un ECG reale è un'altra: *i battiti sono ancora trovabili dopo l'ADC e il framing?* Il PPG invece ha DSP vero in C (`ppg_dsp.c`, `synapse_rr.c`): la sua prova su BVP reale richiede WESAD.

## Fatto

- `scripts/real_signal_replay.py`: mV a 360 Hz → 500 Hz (`resample_poly` 25/18) → count ADC 12 bit con clipping → frame dall'encoder **C** del firmware (`synapse_raw_frame.c`, compilato sull'host) → decoder SDK → rilevatore di picchi (NeuroKit, lo stesso del baseline) → Se/PPV contro le annotazioni, sulla registrazione originale e dopo la catena.
- `scripts/g1_measure.py`: i due numeri della BRINGUP_SOP §4a (rumore in count con ingresso in corto; count per mV da una sinusoide nota) calcolati dai count. Non definisce soglie pass/fail: né la SOP né READINESS_GATE ne danno, una soglia inventata sembrerebbe un requisito.
- `tests/test_real_signal_chain.py` (7 test) con `tests/fixtures/mitbih_ecg_30s.npz` (30 s dei record 100 e 105; PhysioNet MIT-BIH, licenza Open Data Commons Attribution; ritaglio senza elaborazione). `tests/test_g1_measure.py` (6 test: il gain e il rumore che iniettiamo sono quelli che misuriamo).
- `READINESS_GATE.md`: la parte "fail closed" di Known failures era già fatta (`validate_baseline.py` esce con 2, test esistente); il dato mancante è WESAD, con le istruzioni per scaricarlo a mano.

## Numeri misurati (60 s, 132 count/mV)

| Record | Battiti | Se/PPV originale | Se/PPV dopo la catena | Framing lossless |
|---|---|---|---|---|
| 100 | 74 | 0,987 / 1,000 | 0,987 / 1,000 | sì |
| 105 | 83 | 1,000 / 1,000 | 0,988 / 1,000 | sì |
| 108 | 58 | 0,345 / 0,351 | 0,379 / 0,407 | sì |

- Il framing è **bit-esatto** e senza buchi di sequenza; i timestamp dei frame sono il clock dei campioni.
- Nel 105 la catena perde 1 battito su 83. Con 40, 132 e 400 count/mV il risultato è lo stesso: non è la quantizzazione, è il ricampionamento/rilevatore. Il test accetta una perdita relativa all'originale di 0,03.
- Il 108 è già un record difficile per il rilevatore sulla registrazione originale: non dice nulla sulla band.

## NON provato

- L'AD8232: gain, offset, rumore, lead-off. `COUNTS_PER_MV = 132` è un valore nominale di ripiego; il numero vero lo produce `g1_measure.py gain` sulla scheda.
- La catena PPG/RR su BVP reale (`ppg_dsp.c`, `synapse_rr.c`): **aperta**, serve WESAD (`data/wesad/WESAD/S2/S2.pkl`) più un loader del BVP a 50 Hz.
- La radio (MVP-1.7 PR-4, dongle), iOS (Mac), il classificatore di moto, il config write BLE.
- Su questa macchina Windows Application Control blocca a intermittenza la DLL appena compilata: i test C host si saltano e vanno ripetuti finché non girano (qui al 1°–4° tentativo). Uno skip non è un pass.

## Prossimi passi

1. Scaricare WESAD a mano (licenza UCI #465) e chiudere la metà PPG: loader BVP 64→50 Hz, `synapse_rr`/`ppg_dsp` via ctypes, HR e RR contro la verità del torace.
2. Con un dongle BLE: MVP-1.7 PR-4.
3. Con una scheda: `g1_measure.py noise` e `gain`, poi scrivere `ecg_config.gain_measured`.
