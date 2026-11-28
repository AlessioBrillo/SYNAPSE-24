# Il manuale del collaboratore tester — SYNAPSE-24

**Versione:** bozza approvata a parole il 26/11/2026.
**Destinatario:** una persona con profilo tecnico che si occupa di prove pratiche, non di scrivere firmware.
**Disponibilità prevista:** 4-6 ore a settimana, in laboratorio.
**Permessi:** puoi eseguire test, compilare report e documenti, aprire issue. **Non** modifichi firmware, Kconfig o file di configurazione tracciati da Git.

Questo manuale spiega **cosa fare, in che ordine e come capire se è andata bene**. Non devi conoscere il C o ESP-IDF: trovi i comandi già scritti e, accanto, il significato in parole semplici.

---

## 0. Cinque regole che non si discutono

1. **Elettrodi sul corpo solo con la banda a batteria e USB scollegato.** Prima di indossare gli elettrodi fai sempre flash, calibrazione e lettura dei log. Durante la registrazione guardi solo via Bluetooth, oppure da un portatile a batteria e non in carica.
2. **Il generatore di segnali e gli strumenti alimentati a rete si collegano agli ingressi solo fuori dal corpo.** Mai collegare il generatore a una persona.
3. **Le batterie LiPo si caricano fuori dal corpo, mai incustodite.** Se una cella è gonfia, ammaccata o calda: non usarla.
4. **Nessun nome vero nei dati.** Ogni persona registrata ha un codice tipo `S001`. Il consenso deve essere segnato prima della registrazione.
5. **Se un passo fallisce, ti fermi.** Non "aggiusti al volo" e non cancelli un tentativo andato male: ogni tentativo va segnalato, anche quello rosso.

---

## 1. Preparare il PC (una volta, poi solo ricontrollo)

Scopo: avere un computer che sa compilare il firmware e fare i controlli.

1. Apri PowerShell nella cartella del progetto.
2. Digita, uno alla volta:

```powershell
uv sync --dev
$py="$env:APPDATA\uv\python\cpython-3.12-windows-x86_64-none"
$env:PATH="$py;$py\Scripts;$env:PATH"
. "$env:USERPROFILE\esp\esp-idf\export.ps1"
uv run python tools/check_toolchain.py
```

3. Vuoi vedere scritto **READY**. Se vedi **BLOCKED**, non andare avanti: significa che l'ambiente ESP-IDF non è attivo in quella finestra.
4. Controlla che il contratto sia pulito:

```powershell
uv run python tools/check_contract.py
uv run python tools/generate_contracts.py --check
```

**Criterio di successo:** i due comandi finiscono senza errori. In caso di errore, non correggi i file: apri una issue con il messaggio esatto.

Nota: il gate completo su questa macchina Windows può esaurire la memoria. La prova verde di riferimento è quella eseguita su Linux dal server. Tu la usi come verità, non devi ripeterla sul PC.

---

## 2. Controllare il kit appena arriva (incoming inspection)

Prima di dare corrente, controlla fisicamente ogni pezzo. Segna ogni riga con OK o KO.

| # | Cosa controlli | Come | Criterio |
|---|---|---|---|
| 1 | ESP32-S3 | Leggi la scritta sul chip/modulo | Deve essere **N8R8** (8 MB flash + 8 MB PSRAM). Se è N16R8V, è sbagliato: fermati |
| 2 | Seconda ESP32 | Stessa verifica | Serve per il test di sincronizzazione a due pod |
| 3 | Cavi USB | Prova a passare dati | Non devono essere "solo carica" |
| 4 | AD8232, MAX30102, ICM-20948 | Controlla i pin e le saldature | Nessun pin piegato, nessun corto visibile |
| 5 | Resistenze e pull-up | Prendi quelle da 1 % | Servono per il partitore del gain e le pull-up I²C da 4,7 kΩ |
| 6 | Batterie | Misura la tensione col multimetro | 500 mAh per il banco, 1000+ mAh per il soak |
| 7 | INA219 | Controlla l'indirizzo 0x40 e lo shunt 0,1 Ω | Deve rispondere sull'I²C |
| 8 | Elettrodi | Confezione integra | Monouso, pelle integra |

Prima di alimentare: con il multimetro verifica che **3V3 non sia in corto con GND** e che i pin usati siano quelli giusti: SDA **8**, SCL **9**, ECG **1**, INT PPG **5**, INT IMU **6**, lead-off **15**, sync **10**. Non usare mai i GPIO 26-37: su questa scheda sono flash e PSRAM.

Dove scriverlo: `data/live_validation/incoming_<data>.json`.

---

## 3. Primo avvio della banda

1. Cancella la configurazione vecchia:

```powershell
Remove-Item firmware\esp32_band\sdkconfig -ErrorAction SilentlyContinue
Remove-Item firmware\esp32_band\sdkconfig.old -ErrorAction SilentlyContinue
```

Perché: un `sdkconfig` vecchio conserva valori vecchi (ad esempio il gain). È la trappola più comune.

2. Fai il flash della versione `band-bench` (è `band-core` con la shell di fabbrica già accesa: non devi usare `menuconfig`):

```powershell
uv run python scripts/build_and_flash_firmware.py --variant band-bench --port COM3 --clean monitor
```

3. Nel log devi vedere, nell'ordine:
   - `port 0 up: SDA=8 SCL=9`
   - `Sensors initialized (ECG:500Hz, PPG:50Hz, IMU:50Hz) [band-bench]`
   - `research service registered`
   - `Entering main loop`

   La riga deve dire **`[band-bench]`**: il log stampa la variante davvero compilata. Una riga `[band-core]` significa immagine vecchia o sbagliata: fermati, cancella `sdkconfig` e rifai il flash con `--variant band-bench` (non basta `menuconfig`).

4. Chiedi alla banda il suo record (la modalità factory è già attiva in `band-bench`):

```powershell
uv run python scripts/factory_calibrate.py --port COM3 --dump
```

**Criterio:** la risposta deve contenere un seriale `SYN-` seguito da 12 caratteri esadecimali, e deve essere **lo stesso** che vedi nel log `SYNAPSE_ID` e nella characteristic DIS `2A25`. Se il seriale è vuoto, è un bug: fermati e segnalalo. Non provare a scriverlo a mano.

Il factory test (`FACTORY RESULT: PASS`) deve durare circa 13 secondi e non mostrare righe `[FAIL]`.

---

## 4. Configurare e calibrare i sensori

Ci sono tre livelli. Non confonderli:

- **Contratto**: `config/hardware.yaml` fissa rate, range, ODR, pin e UUID. Questi valori sono compilati nel firmware e **non** si cambiano da USB o BLE durante una prova.
- **Default di fabbrica**: i valori compilati per i campi per-unità (gain 1100, LED `1F,1F`, IMU 8 g / 500 dps).
- **Profilo per unità**: il record dentro la banda con i valori misurati davvero per quella unità: gain, correnti LED, bias IMU e capacità della cella.

La regola è: **si misura, si scrive nel record, si verifica che la banda legga lo stesso valore**. Scrittura e verifica sono due passi distinti:

```powershell
# 1. Scrittura USB: crea il record host con provenance completa (prima di ogni scrittura BLE)
uv run python scripts/factory_calibrate.py --port COM3 --gain-json data/live_validation/gain.json --led 1F,1F --operator "A. Brillo" --instrument "GEN-42" --fw "band-bench@<hash>" --uncertainty 2 --battery 1000 --reboot
# 2. Profilo BLE: ammesso solo su unita gia calibrata via USB
uv run python scripts/device_profile.py example
uv run python scripts/device_profile.py configure SYN-A1B2C3D4E5F6 --address AA:BB:CC:DD:EE:FF --profile profilo.json
# riavvia la banda, poi:
uv run python scripts/device_profile.py verify SYN-A1B2C3D4E5F6 --address AA:BB:CC:DD:EE:FF
```

`configure` scrive il profilo in modo atomico e rifiuta la scrittura se anche un solo campo letto indietro non corrisponde, ma **non commetta più il record host**: dopo la scrittura la banda deve essere **riavviata** (`pending_reboot: 1` nel readback) e poi devi eseguire `verify`. È `verify` — non `configure` — a confrontare il readback con il contratto e a committare `data/calibration/<seriale>.json` (fuori da Git). Senza un `verify` verde dopo il riavvio, il record locale non esiste e la prova non è valida.

Una scrittura BLE di valori per-unità è ammessa solo se esiste già un record calibrato via USB con **provenance completa** (seriale, operatore, strumento, incertezza, versione firmware): la BLE applica quel record, non lo inventa. Il campo `feature_rate_hz` è **solo di sessione**: non viene persistito nel record.

### 4.1 Gain ECG

1. Fuori dal corpo, collega il generatore all'ingresso: **imposta il generatore a 1 Vpp**, sinusoide a 10 Hz; il **partitore 1000:1 porta 1 mVpp agli elettrodi** (resistenze all'1 %). Non impostare il generatore a 1 mVpp: dopo il partitore sarebbero 1 µVpp.
2. Registra 60 secondi con il validatore.
3. Calcola:

```powershell
uv run python scripts/g1_measure.py gain <xdf> --freq 10 --applied-mvpp 1 --configured-gain <valore-attuale>
```

 `--applied-mvpp 1` è il segnale **agli elettrodi** (1 mVpp), non quello letto sul generatore (1 Vpp): passa `1`, non `1000`.

 Il `<valore-attuale>` è il gain che la banda sta usando adesso (quello del record, se già calibrata). Se lo lasci fuori e la banda è già calibrata, il numero sarà sbagliato.

**Criterio:** tre misure consecutive devono concordare entro il **5 %**. Un valore fino a ±15 % dal nominale 1100 è accettato ma va annotato; oltre il doppio significa cablaggio o Kconfig sbagliato.

Nota: l'XDF ECG scritto dal validatore è ora in **µV**, coerente con `g1_measure.py`: non convertire di nuovo i campioni prima di calcolare il gain.

### 4.2 Corrente LED del PPG

1. Con la banda ferma sul tavolo, prova le coppie di corrente una alla volta (per esempio `1A,1A`, `1F,1F`, `24,24`).
2. Per ogni coppia registra 30 secondi e annota SQI e perfusion index.
3. Scegli la coppia con il miglior compromesso tra qualità e consumo.
4. Scrivila nel record:

```powershell
uv run python scripts/factory_calibrate.py --port COM3 --led 1F,1F --operator "Il tuo nome"
```

**Criterio:** la misura con la corrente scelta deve restare stabile per almeno 30 secondi consecutivi.

### 4.3 Bias IMU

1. Metti la banda su un tavolo fermo, senza muoverla, per 10 secondi.
2. Esegui `uv run python scripts/calibrate_imu.py <xdf> --duration 10 > imu.json`.
3. Scrivi i valori nel record con `factory_calibrate.py --imu-json imu.json`.

**Criterio:** la banda ferma deve leggere circa 1 g sull'asse Z e valori prossimi a zero sugli altri assi.

### 4.4 Chiusura della calibrazione

Ogni volta che salvi, ricontrolla:

```powershell
uv run python scripts/factory_calibrate.py --port COM3 --dump
```

**Criterio:** seriale corretto (derivato dal MAC, non scritto a mano), data, operatore, strumento (`--instrument`), versione firmware (`--fw`), incertezza del gain (`--uncertainty 5` = ±5 %), rumore ECG (`--noise-uv`) e capacità reale della cella (`--battery 1000`). Il file finisce in `data/calibration/<seriale>.json`, fuori da Git. La capacità deve corrispondere a `hardware_bringup.yaml`: se la cella è diversa, il gate di configurazione va in FAIL, non va "aggiustato" il contratto a mano.

---

## 5. Matrice dei test da eseguire

Ogni test ha un ID. Quando lo esegui, salva l'esito in `evidence/<ID>.json`. T05, T11 e T12 richiedono sempre `--manifest` (umano o bench): senza manifest non parte la registrazione.

| ID | Test | Comando / azione | Criterio di passaggio |
|---|---|---|---|
| T01 | Controllo kit | `bench_suite.py incoming --verified-by "nome verificatore"` + tabella del paragrafo 2 | Tutte le righe OK, **cinque righe manuali verificate con `--verified-by`** (senza verificatore T01 è FAIL) |
| T02 | Identità e record | `bench_suite.py factory --port COM3 --address AA:BB:CC:DD:EE:FF` | Seriale `SYN-` + 12 hex, uguale al log **e alla characteristic DIS `2A25` letta via BLE** |
| T03 | I²C scan | `bench_suite.py sensors --port COM3 --log boot.log` | **Log fresco** con righe `answers` per 0x57, 0x68, 0x40 |
| T04 | Configurazione flashed | `validate_phase1_entry.py --live-hardware …` | Ogni campo uguale al contratto; un campo mancante è FAIL |
| T05 | Streaming 60 s | `bench_suite.py stream --live --manifest <bench.json>` | Report fresco, zero frame persi, XDF valido; se passi `--bench-values`, serve anche `--verified-by` |
| T06 | Qualità PPG | Stesso run | SQI ≥ 0,3 e MAP ≤ 0,5 per oltre 30 s |
| T07 | Frequenza cardiaca | Run con manifest **umano** + conteggio del polso | Errore medio ≤ 2 bpm |
| T08 | RMSSD | Confronto con reference NeuroKit2 | Errore medio < 5 ms |
| T09 | Potenza INA219 | Carico noto da 100 Ω | Corrente entro ±5 % del valore atteso |
| T10 | Sync a due pod | `bench_suite.py sync --pods a.xdf b.xdf` | **Almeno 30 min di sovrapposizione**, correlazione ≥ 0,7 e residuo ≤ 10 ms (soglia da `sync.tier_budgets.T0`) |
| T11 | Gate report | `bench_suite.py g1-report --live --manifest <manifest.json>` | Report fresco, `rehearsal: false`, PASS/FAIL esplicito; **senza `--manifest` scrive un'evidenza FAIL, non si arresta** |
| T12 | Soak 6 ore | `bench_suite.py soak --live --manifest <manifest.json> --duration 21600` con LabRecorderCLI | Zero drop, XDF monotono, batteria sufficiente |

Le cinque righe manuali di T01 e le righe misurate a mano (T07, T09, `--bench-values`) le compili tu; io le verifico e aggiungo `--verified-by` nel report: il nome del verificatore resta nello storico del run.

---

## 6. Registrare una sessione con una persona

1. Chiedi il consenso e assegna un codice: `S001`, `S002`, ... Non scrivere mai il nome vero.
2. Compila `data/sessions/<data>/manifest.json` con `"kind": "human"`, codice soggetto (`S001`…), `"consent": true`, condizione (seduto / in movimento), seriale banda, operatore, data. Senza `kind` il manifest è comunque umano, ma scriverlo esplicitamente evita errori. Esempio reale di struttura (valori da sostituire con quelli della sessione):

```json
{
  "kind": "human",
  "subject_id": "S001",
  "consent": true,
  "condition": "seated, still",
  "device_serial": "SYN-A1B2C3D4E5F6",
  "operator": "nome operatore",
  "date": "2026-11-27"
}
```

   Il `subject_id` è sempre un codice `S###`: **nessun nome vero nei dati**, né qui, né in `source_id`, né nelle issue.
3. Collega gli elettrodi: RA vicino al gomito, LA vicino al polso, RL su una parte ossea. Pelle pulita e asciutta.
4. **Stacca l'USB.**
5. Avvia:

```powershell
uv run python scripts/validate_phase1_entry.py --live-hardware --manifest data/sessions/<data>/manifest.json --config config/hardware_bringup.yaml --duration 60
```

6. Alla fine controlla:
   - il file XDF esiste e ha `sha256`;
   - il gate report ha `rehearsal: false`;
   - `device_serial` è il seriale della banda;
   - nessun `source_id` contiene il nome della persona.

Se il soggetto si sente a disagio o la pelle si arrossa: fermati, togli gli elettrodi, segnala.

---

## 6b. Prove da banco (senza persona)

Le righe di calibrazione e il controllo INA219 si fanno con elettrodi cortocircuitati o un carico noto: **non c'è nessun soggetto e nessun consenso da dichiarare**. Usa un manifest dedicato:

```json
{
  "kind": "bench",
  "condition": "shorted input, off body",
  "device_serial": "SYN-A1B2C3D4E5F6",
  "operator": "nome operatore",
  "date": "2026-11-27"
}
```

Salvalo in `data/bench/manifest.json` e passalo a `bench_suite.py stream --live --manifest data/bench/manifest.json`. I file XDF finiscono in `data/bench/`, mai nell'albero delle sessioni umane. I gate T07/T08 (che richiedono un riferimento umano) risultano `N/A` dichiarato: non è un pass gratuito, ma non può nemmeno far fallire una prova tecnica.

Se durante la prova misuri a mano corrente a riposo o corrente media, scrivi un file JSON con `{"idle_current_ma": 83.2}` e passalo con `--bench-values <file>` insieme a `--verified-by "nome verificatore"`. Il validatore rifiuta il comando se manca il verificatore, e il valore resta nello storico del run.

---

## 7. Il soak di 6 ore

Serve per provare il clock, la radio e la batteria per davvero.

- Usa la batteria da **1000+ mAh**, non quella da 500.
- Serve LabRecorderCLI installato e avviato: è lui il solo scrittore del file.
- Comando: usa `bench_suite.py`, non il solo validatore: è lui a scrivere `evidence/T12_*.json`. Il validatore da solo non produce l'evidenza T12.

```powershell
uv run python scripts/bench_suite.py soak --live --manifest data/sessions/<data>/manifest.json --duration 21600
```

Con 4-6 ore a settimana non puoi stare presente per sei ore di fila: fai l'avvio insieme a me, poi fissiamo un controllo a metà e la verifica finale. **Non** usare `--soak-rehearsal` come prova: è solo una prova del meccanismo e il suo report è marcato `rehearsal: true`.

**Criterio:** zero frame persi, XDF con timestamp monotone, batteria ancora sufficiente alla fine. Usa `bench_suite.py soak --live --manifest ... --duration 21600`: l'evidenza T12 richiede un report fresco con `rehearsal: false`, **`duration_s == 21600`** e come sorgente dell'artifact **LabRecorderCLI** (non lo spool sintetico, non un XDF ricostruito dal recorder interno).

---

## 8. Come segnalare un difetto

Apri una issue con questo schema:

```
Titolo: [T05] drop di sequenza con banda SYN-XXXXXXXXXXXX

ID test: T05
Seriale banda: SYN-XXXXXXXXXXXX
Firmware/build: <hash o versione>
Config usata: config/hardware_bringup.yaml
Comando esatto: <copia il comando>
Risultato atteso: zero drop
Risultato visto: 17 drop in 60 s
Evidence: evidence/T05_<timestamp>.json
Log: <incolla le righe utili>
Foto: <se serve>
Tentativi: 1/3, 2/3, 3/3
```

Non scrivere dati della persona nella issue.

---

## 9. Cosa significa "task chiuso"

Un compito è finito solo se:

- [ ] ha un ID test;
- [ ] esiste il file di evidenza corrispondente;
- [ ] il gate report dice PASS o FAIL esplicitamente (no N/A su una prova live);
- [ ] è collegata una issue, anche se il risultato è verde;
- [ ] io ho aggiunto `verified_by`;
- [ ] la tabella in `STATUS.md` è aggiornata.

Un tentativo fallito **non** si cancella: si segnala e si ripete.

---

## 10. Routine settimanale

- **Lunedì:** controllo che PC e contratto siano puliti; leggo `STATUS.md`.
- **Martedì:** test T01-T04 (kit, factory, I²C, configurazione).
- **Mercoledì:** test T05-T09 (streaming, qualità, HR, RMSSD, potenza).
- **Giovedì:** calibrazione e ripetizione dei test andati male.
- **Venerdì:** scrivo il report, aggiorno le evidenze, preparo le issue del lunedì dopo.

---

## Appendice A — Parole che incontrerai

- **XDF**: il file che contiene tutti i segnali registrati, con i tempi.
- **LSL**: il sistema che fa arrivare i segnali al PC con orari affidabili.
- **BLE / GATT**: il Bluetooth usato dalla banda per parlare col PC.
- **NVS**: la piccola memoria della banda dove sta la calibrazione.
- **sdkconfig**: il file di configurazione generato quando compili; vecchio = valori vecchi.
- **Gate**: un controllo con un risultato preciso: PASS, FAIL o N/A.
- **Seriale**: l'identificativo unico della banda, `SYN-` + 12 caratteri.
- **Drop**: un pezzo di dati perso. "Zero drop" è il requisito.

## Appendice B — Cose da non fare mai

- Fare flash o calibrazione con gli elettrodi addosso.
- Cancellare o riscrivere un gate report precedente.
- Cambiare `hardware.yaml` per "far passare" una misura.
- Scrivere il nome di una persona in `source_id`, log o issue.
- Modificare Kconfig a mano per provare un valore: si cambia il contratto e si ricompila.
- Usare `--soak-rehearsal` come se fosse un soak reale.
- Andare avanti dopo un `[FAIL]` senza averlo segnalato.
