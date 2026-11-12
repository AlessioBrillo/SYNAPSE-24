# Dal nulla alla prima misura reale (guida per chi parte senza hardware)

Complementa, non sostituisce, `PROCUREMENT.md` (cosa comprare, ~140 €) e `BRINGUP_SOP.md` (cablaggio, flash, misure). Qui c'è solo ciò che quei due documenti **presuppongono** e che a oggi manca, nell'ordine in cui serve. Stato delle prove senza hardware: `docs/increments/MVP1_8_REAL_SIGNAL.md`.

## 1. Cosa manca rispetto a PROCUREMENT.md

PROCUREMENT dà per posseduti "alimentatore da banco, multimetro, oscilloscopio". Senza strumenti:

| Serve per | Cosa | Costo indicativo | Bloccante? |
|---|---|---|---|
| Righe 1-2 (corrente a riposo e in streaming) | **USB power meter** in linea (al posto dell'alimentatore da banco). L'INA219 del kit misura già la corrente durante la registrazione | ~10 € | No: c'è già l'INA219 |
| Cablaggio e INA219 (carico noto, §0 e §4a della SOP) | **Multimetro** con scala mA | 15-25 € | Sì, per verificare l'INA219 e i collegamenti |
| §4a.2 gain AD8232 (sinusoide nota 1 mVpp, 10 Hz) | Un **generatore di segnali** (modulo DDS da banco) più partitore 1000:1 con resistenze all'1 % (es. 10 kΩ + 10 Ω) | ~30-40 € | **No** per G1: la catena funziona con il gain nominale. Senza questa misura gli µV dell'ECG restano "non calibrati" (vedi §5) |
| Rumore AD8232 (§4a.1) | Nulla: ingresso in corto con un filo | 0 € | No |
| Oscilloscopio | Nessuna riga della tabella G1 lo richiede | n.d. | No |
| Telefono | App **nRF Connect** (Android o iOS) per vedere i servizi BLE | gratis | Sì |
| PC | Radio BLE in ruolo *central*. Il blocco incontrato in MVP-1.7 riguarda solo il ruolo *periferico*; il PC come *central* con `bleak` non è stato provato con questo client | già ce l'hai | Da verificare (§2) |

Note sulla lista di PROCUREMENT:
- **ICM-20948 è fuori produzione**: PROCUREMENT riporta l'ultima spedizione al 30 ottobre 2026, già passata. Ne servono 2 comunque (uno di scorta): ordinali subito e accetta cloni/stock di distributori. Se non si trovano, il resto del kit funziona (ECG e PPG non dipendono dall'IMU), ma il test pod↔pod (riga 8) usa l'ACC.
- Kit resistenze (riga 11): prendi anche resistenze **all'1 %** per il partitore.
- ESP32-S3 **N8R8** (WROOM-1), **non** N16R8V: lo dice PROCUREMENT.
- Un Mac non serve per G1: serve solo per compilare iOS.

## 2. Da fare adesso, senza hardware

1. **Toolchain.** Installa ESP-IDF v5.5.5 (`docs/hardware/ESP-IDF_SETUP.md`), poi `uv run python tools/check_toolchain.py`.
2. **Elimina l'impostazione vecchia del gain (trappola).** Nella tua copia, `firmware/esp32_band/sdkconfig` (ignorato da git) contiene `CONFIG_SYNAPSE_ECG_GAIN=600`, mentre il default attuale del Kconfig è `110000` (gain 1100×). ESP-IDF tiene i valori già presenti in `sdkconfig` e non applica il nuovo default: flashando così l'ECG esce ~1,8× troppo grande. Prima del primo build:
   `rm firmware/esp32_band/sdkconfig firmware/esp32_band/sdkconfig.old` e `rm -r firmware/esp32_band/build`; poi compila `band-core` come in `ESP-IDF_SETUP.md` e controlla che in `sdkconfig` compaia `CONFIG_SYNAPSE_ECG_GAIN=110000`. La CI già cancella `sdkconfig` prima di compilare. Se il build si ferma per memoria è un limite noto di questa macchina (AGENTS.md): chiudi gli altri programmi.
3. **Radio del PC.** Una scansione di sola lettura per vedere se il PC fa da *central*: `uv run python -c "import asyncio, bleak; print(asyncio.run(bleak.BleakScanner.discover(5)))"`. Con Bluetooth acceso e un telefono con BLE in vista deve elencare qualcosa.
4. **Il gate senza hardware** (già verde, da rieseguire dopo ogni modifica): `uv run python scripts/validate_phase1_entry.py --config config/hardware_bringup.yaml --dry-run` e `uv run pytest tests/test_sop_end_to_end.py`. Il SOP completo viene eseguito qui su dati sintetici: se non gira qui, non girerà sul banco.

## 3. Il giorno in cui arriva il pacco

Segui `BRINGUP_SOP.md` nell'ordine; i punti dove serve qualcosa in più:

1. **§0 Cablaggio.** Prima di dare corrente controlla col multimetro che nessun pin 3V3 sia in corto con GND e che i GPIO siano quelli della tabella (SDA 8, SCL 9, AD8232 su 1, INT PPG 5, INT IMU 6). Non usare GPIO 26-37.
2. **§1 Flash e §2 nRF Connect.** Devono comparire il servizio di ricerca `A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C70` e le sette caratteristiche. Se manca, il firmware non è quello che credi (`CONFIG_SYNAPSE_FEATURE_RESEARCH_SERVICE`).
3. **§3 Primo giro.** `uv run python scripts/validate_phase1_entry.py --live-hardware --config config/hardware_bringup.yaml --duration 60`. L'XDF finisce in `data/processed/live_validation/synapse_live_<timestamp>.xdf` e il report in `data/live_validation/gate_report.json`.
4. **§4a.1 Rumore.** Cortocircuita RA, LA e RL con un filo (nessun elettrodo sul corpo), registra 60 s con il validatore e poi:
   `uv run python scripts/g1_measure.py noise data/processed/live_validation/synapse_live_<timestamp>.xdf`
   Il validatore potrà segnare FAIL sui gate di HR e qualità perché non c'è un battito: è atteso, interessa solo l'XDF. Se il file non viene scritto, registra con LabRecorder (SOP §5).
5. **§4a.2 Gain (opzionale per G1).** Con il generatore (1 mVpp a 10 Hz applicati tra RA e LA tramite il partitore, RL a massa del partitore) registra 60 s e poi:
   `uv run python scripts/g1_measure.py gain <xdf> --freq 10 --applied-mvpp 1`
   Il risultato è `gain_measured` e `sdkconfig_ecg_gain`: scrivi quest'ultimo in `CONFIG_SYNAPSE_ECG_GAIN` (menuconfig), ricompila, riflasha e scrivi `gain_measured` in `config/hardware.yaml` (`ecg_config.gain_measured`). Lo strumento rifiuta un flusso saturato o senza la sinusoide, invece di dare un numero. Un errore del 5 % sull'ampiezza applicata è un errore del 5 % sul gain: basta per distinguere 600 da 1100, non per un'incertezza strumentale.
6. **Tabella G1 di PROCUREMENT.** Le righe 3, 4, 8, 9, 11 le scrive il validatore; le righe 1, 2, 5, 6, 7, 10 si scrivono a mano in `gate_report.json`. HR contro il conteggio manuale (riga 5): 30 s di polso contati con un timer mentre il PPG registra.
7. **Soak (6 ore, a batteria):** `--soak` come in PROCUREMENT.

## 4. Cosa aspettarsi, in base a ciò che è già misurato

- **HR dal PPG:** su polso reale (BIDMC, trasmissivo, a letto) l'errore mediano è ≤ 0,5 bpm. Su un MAX30102 riflessivo al polso o al dito in movimento **non è noto**: è la riga 5.
- **RMSSD dal PPG (riga 6, soglia 5 ms):** concorda con l'ECG su 3 record BIDMC su 4 e sul quarto è ~5× più alto con ogni rilevatore provato, anche offline: la variabilità dell'intervallo del polso può differire da quella dell'ECG. **Non aspettarti che la riga 6 passi al primo tentativo**, e non è un difetto da correggere nel firmware senza un riferimento sul banco. `CONFIG_SYNAPSE_RR_ENABLE` resta spento finché la riga 6 non passa.
- **Margine dell'ECG:** con gain 1100× e ADC a 12 bit l'uscita satura per ingressi oltre circa +1,3 / −1,5 mV (valori nominali: range dell'ADC e mid-rail non sono misurati). Un QRS grande da torace può superarli: nel test, 0,2 % dei campioni del record 105. I battiti si trovano comunque; l'ampiezza dei picchi saturati no. Da avambraccio le ampiezze sono tipicamente minori, ma non l'ho misurato.
- **Ampiezza sbagliata, battiti giusti:** un `SYNAPSE_ECG_GAIN` sbagliato scala l'intero ECG ma non altera HR e RR. Se gli µV sembrano 2× troppo grandi o piccoli, è il gain, non il segnale.

## 5. Cosa NON è provato (non farlo credere a nessuno)

Nessun sensore, nessuna scheda, nessuna radio: tutto ciò che sta in `MVP1_8_REAL_SIGNAL.md` riguarda dati registrati altrove fatti passare nel codice C del firmware sul PC. Finché la riga 5-7 della tabella G1 non ha un numero misurato dal banco, HR, RMSSD e qualità PPG della band sono **non verificati**. Anche MVP-1.7 PR-4 (radio con la band virtuale) e la compilazione iOS restano da fare, con un dongle BLE e un Mac.
