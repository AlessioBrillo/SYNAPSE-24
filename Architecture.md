# Architettura Dati-Massimi: Sistema Indossabile 24/7 Multimodale
## Revisione strategica — Fase Sperimentale (no vincoli regolatori/commerciali)

## Premessa: cambio di obiettivo

Il piano precedente ottimizzava per un prodotto vendibile con un chiaro claim (cardiaco medical-grade + neuro wellness). Questa revisione ottimizza per un obiettivo diverso e più aggressivo:

> **Massimizzare qualità, quantità e diversità del dato fisiologico/neurologico raccolto in 24h continue, in un form factor indossabile, per alimentare un motore di pattern recognition che è il vero prodotto.**

Design industriale, regolamentazione e commercializzazione sono esplicitamente fuori scope in questa fase. Questo sblocca scelte che nel piano precedente erano vietate (es. hub esterno, pacchi batteria più ingombranti, form factor "cyborg" temporaneo da laboratorio).

## Il vincolo reale: non è la sensoristica, è l'energia nel tempo

I chip per EEG/ECG/PPG/fNIRS ad alta qualità esistono già e sono comprabili oggi (ADS1299, MAX86141/86150, ecc. — vedi piano precedente). Il collo di bottiglia non è "che sensore uso" ma **quanta potenza posso permettermi in ogni istante, in ogni punto del corpo, per 24h consecutive**.

Due riferimenti concreti cambiano la strategia rispetto al piano "fascia per dormire":

1. **EEG in-ear ultra-low-power**: un sistema di acquisizione ed elaborazione EEG integrato in un auricolare ha raggiunto **600 ore di autonomia** (Guermandi, Cossettini, Benatti, Benini, "A Wireless System for EEG Acquisition and Processing in an Earbud Form Factor with 600 Hours Battery Lifetime," IEEE EMBC 2022). Il consumo crolla quando si riduce il numero di canali, si processa on-device invece di streammare raw data via radio, e si accetta un montaggio a pochi elettrodi.
2. **EEG single-channel da fronte**: un sistema con front-end ADS1298 + STM32 raggiunge **~150,85 mW medi e oltre 24,6h su una batteria da 1000 mAh**, con lo staging del sonno delegato a un modello deep learning (SleePyCo) lato cloud/hub, non on-device. Utile come benchmark di potenza per canale, ma 1000 mAh non è indossabile in testa in modo confortevole.

**Conclusione tecnica:** un'unica fascia testa a streaming continuo multi-canale EEG+fNIRS ad alta densità non regge 24h in un form factor leggero — è un limite fisico di oggi, non un errore di progetto. La soluzione non è rinunciare alla continuità, ma **ridistribuire dove sta la batteria/calcolo e quando i canali costosi sono accesi**.

## Principio architetturale 1 — Disaccoppiamento sensore/energia/calcolo

Se il vincolo "deve sembrare un accessorio" è temporaneamente sospeso, la mossa a massima resa tecnica è **separare fisicamente i tre sottosistemi**:

- **Sensor pod (in testa):** solo elettrodi + AFE analogico (ADS1299-class) + ottica fNIRS + amplificazione minima. Nessuna batteria pesante, nessun radio ad alta potenza. Peso minimizzato = comfort massimo = indossabilità 24h reale.
- **Hub energia/calcolo:** batteria di capacità reale (2.000–5.000+ mAh) + SoC per edge AI (inference multimodale) + radio. Puoi posizionarlo dove il peso è meglio tollerato: fascia cardiaca sull'avambraccio (che già deve portare batteria per il cuore, quindi "riusi" la capacità), oppure un piccolo hub a collo/tasca/cintura collegato via BLE o — per banda molto più ampia e meno latenza — un cavo sottile schermato dietro il collo (accettabile in fase sperimentale, impensabile in fase commerciale).
- **Collegamento:** BLE 5.x per il caso wireless-puro (banda sufficiente per EEG multi-canale a 250–500 Hz compresso); LE Audio/cavo se serve throughput maggiore per fNIRS ad alta risoluzione temporale o EEG ad alto numero di canali senza compressione lossy.

Questo singolo cambio architetturale è quello che sposta di più l'ago: rimuove il vincolo di potenza dalla testa e lo sposta dove è più facile portare capacità (avambraccio, tasca), permettendo di **spingere il numero di canali EEG e la presenza di fNIRS molto oltre quello che un Muse o un Dreem possono fare**, restando comunque indossabile tutto il giorno.

## Principio architetturale 2 — Acquisizione a livelli (tiered), non "tutto sempre acceso"

Per massimizzare dati su 24h reali, la scelta non è "quanti canali tengo sempre accesi" ma **come alterno regimi di acquisizione in base al contesto**, usando l'IMU e semplici euristiche (poi un modello) come trigger.

| Livello | Cosa acquisisce | Quando | Potenza indicativa | Canali |
|---|---|---|---|---|
| **Tier 0 — Continuo H24** | PPG multi-lunghezza d'onda, IMU 9 assi, temperatura cutanea, bioimpedenza base, 1–2 canali EEG in-ear | Sempre | Ordine di grandezza dei µW–basso mW per canale (regime da 600h-earbud) | Ampiezza ridotta, ma copertura temporale totale |
| **Tier 1 — Sessioni a riposo/sonno** | EEG multi-canale (6–16 ch) frontale + dietro-orecchio, fNIRS prefrontale multi-canale, ECG a riposo | Attivato da IMU (immobilità >X min) o da finestra oraria (notte) | Decine di mW, alimentato dall'hub | Massima densità neuro, quando l'artefatto da movimento è minimo — cioè quando il dato è comunque più pulito |
| **Tier 2 — Sessioni volontarie/attive** | ECG a singola derivazione on-demand, test cognitivi con fNIRS+EEG, calibrazione | Iniziate dall'utente o da protocollo sperimentale | Burst, non continuo | Dati "etichettati" di alta qualità per training del modello |

Questo schema massimizza **sia** la copertura temporale (Tier 0 non si interrompe mai) **sia** la qualità del dato neuro denso (Tier 1 arriva proprio nei momenti — sonno, riposo — in cui il segnale EEG/fNIRS è fisiologicamente più pulito, perché l'artefatto da movimento è il principale limite tecnico di questi segnali). Non stai sacrificando densità per continuità: le stai allineando ai momenti in cui ciascuna è più ottenibile.

## Principio architetturale 3 — L'AI come layer che decide, non solo come layer che interpreta

Se il prodotto reale è il riconoscimento di pattern su una mole di dati grande e diversificata, l'architettura AI deve fare due cose distinte:

**A. Edge triage (bassa potenza, sempre attivo).** Un modello piccolo (SNN — spiking neural network, o un TinyML transformer quantizzato) gira sul Tier 0 e decide quando promuovere l'acquisizione a Tier 1: rileva l'inizio del sonno, un pattern anomalo in HRV, immobilità prolungata, o semplicemente segue un programma. Le architetture neuromorfiche (es. ASIC ispirati a Intel Loihi) sono studiate proprio per classificazione continua nel range sub-milliwatt sfruttando la sparsità temporale dei segnali fisiologici — è la strada più coerente con "sempre acceso senza svuotare la batteria".

**B. Fusion e personalizzazione (potenza maggiore, batch).** Sull'hub (o anche offloaded quando disponibile connettività, in fase sperimentale non devi preoccuparti di edge-only), un modello multimodale fa fusion di ECG+PPG+EEG+fNIRS+IMU+temperatura per costruire feature ad alto livello (stadi del sonno, carico cognitivo, stress autonomico, pattern cardio-neuro accoppiati come coerenza cardio-cerebrale). La letteratura recente converge su: fusion ECG-PPG per stima robusta di HR/PTT (MAE ~0,88 bpm in setup di ricerca con dataset a 40 soggetti), federated/personalization layers per adattare le baseline al singolo utente (fondamentale perché la variabilità inter-individuale in segnali neuro è alta), e piattaforme edge-AI modulari (es. BioGAP-Ultra, Benini et al. 2025) pensate esplicitamente per sincronizzare EEG+EMG+ECG+PPG con processing embedded.

**Perché questo conta per te:** il valore del prodotto non è "quanti sensori ho" ma "quanto è ricco e ben sincronizzato il dataset multimodale che l'AI riceve". Un'architettura a livelli con trigger intelligenti produce un dataset *più* utile di uno streaming continuo indiscriminato, perché massimizza il segnale-per-watt e il segnale-per-byte-di-storage.

## Bilancio energetico realistico per l'architettura a livelli

Stima indicativa (da affinare in fase di prototipazione), assumendo hub separato con batteria 2.000–3.000 mAh:

- **Tier 0 continuo (H24):** PPG + IMU + temp + 1-2ch EEG in-ear ≈ qualche mW medio → con ottimizzazione aggressiva (processing locale, niente streaming raw) è nel regime dimostrato dal paper dei 600h earbud, quindi sostenibile per giorni anche su una batteria piccola dedicata al pod testa, se lo isoli dal resto.
- **Tier 1 (6–16h/giorno tra sonno + riposi):** EEG multicanale + fNIRS ≈ decine di mW → su un hub da 2.000+ mAh questo è ampiamente sostenibile per un ciclo di 24h, perché non è mai "sempre" ma a finestre.
- **Radio:** il costo dominante nascosto è spesso il BLE streaming continuo, non l'analogico. Comprimere/quantizzare a bordo pod prima di trasmettere (o processare interamente sul pod per Tier 0) è la leva di risparmio più grande, più della scelta dei sensori stessi.

## Cosa significa "spingere al massimo tecnico" concretamente

Dato che design e regolamentazione sono sospesi, la configurazione a densità/qualità massima realisticamente costruibile oggi con componenti esistenti è:

- **Pod testa:** 8–16 canali EEG dry (ADS1299 x2 se serve >8ch) su montaggio frontale + dietro-orecchio + fNIRS multi-canale (multipli MAX86141) a due lunghezze d'onda con canali corti per rimuovere il contributo superficiale.
- **In-ear satellite:** 1-2 canali EEG a bassissima potenza per continuità H24 anche quando il pod "pesante" non è indossato (es. sport, doccia).
- **Fascia avambraccio:** PPG multi-lunghezza d'onda + ECG a derivazione singola (o due punti di contatto per Lead-I-equivalente) + bioimpedenza + IMU + temperatura, alimentata da batteria propria più capiente essendo su un arto (meno vincolo di peso percepito che in testa).
- **Hub (fase sperimentale, non vincolato dal design):** raccoglie via BLE entrambi i nodi, esegue fusion e edge triage, porta la batteria "vera".

Questa è la configurazione che massimizza **contemporaneamente** copertura 24h, numero di canali, e diversità di modalità — accettando che in fase sperimentale il fattore forma sia da laboratorio, non da prodotto.

## Limiti fisici che restano, anche senza vincoli di design/regolamentazione

- **Artefatto da movimento** resta il limite dominante per EEG e fNIRS in stato attivo, indipendentemente da quanta potenza/canali metti — è un problema di fisica del contatto elettrodo-pelle e di dispersione ottica nel tessuto, non risolvibile solo con più hardware. Il Tier 1 (finestre di quiete) esiste proprio per aggirare questo limite, non eliminarlo.
- **Impedenza pelle-elettrodo a secco** resta 10–100x più instabile della gel, specialmente su cuoio capelluto con capelli — motivo per cui anche in un progetto "senza compromessi di design" i canali scalp-coperti da capelli restano la parte più fragile del sistema.
- **fNIRS non supera ~1,5 cm di profondità** — nessuna quantità di potenza o canali aggiuntivi ti dà accesso a strutture cerebrali profonde con ottica non invasiva; è un limite fisico della luce nel tessuto, non ingegneristico.
- **Batteria come vincolo assoluto rimane**, anche disaccoppiata: più canali e più densità = più energia richiesta nei Tier 1/2, quindi il numero di sessioni ad alta densità per giorno è comunque finito anche con un hub da 3.000+ mAh.

## Prossimi passi consigliati

1. **Prototipo Tier 0 (continuità) per primo**: valida che il pod in-ear + fascia cardiaca reggano davvero 24h+ con processing locale, prima di aggiungere complessità.
2. **Prototipo Tier 1 (densità) in parallelo, disaccoppiato**: banco di lavoro EEG multicanale + fNIRS via ADS1299/MAX86141 su hub da laboratorio, senza preoccuparti ancora di indossabilità — l'obiettivo qui è validare qualità del segnale e algoritmi di fusion.
3. **Unione dei due solo dopo** aver validato entrambi separatamente — è il momento in cui inizi a lavorare su trigger IMU-based per il passaggio Tier 0→Tier 1 automatico.
4. **Dataset labeling**: pianifica fin da subito come etichetterai le sessioni Tier 1/2 (sonno da PSG di riferimento per validazione, task cognitivi noti per fNIRS/EEG) — è la parte che rende il dataset utile per il pattern recognition, non solo grande.

## Caveat

- Le cifre di potenza/durata citate provengono da paper di ricerca con setup specifici (numero canali, sample rate, tipo di processing); vanno validate sul tuo montaggio esatto, non assunte come garantite.
- Il paper delle 600h è su un numero di canali molto ridotto rispetto a un montaggio 8-16 canali multi-sito — non è una prova diretta che quella efficienza scali linearmente con più canali, ma indica la direzione (processing locale + pochi canali continui è la leva giusta).
- L'architettura a hub separato risolve il problema energetico ma introduce un problema di sincronizzazione multi-nodo (clock drift tra pod testa, in-ear, fascia braccio) che andrà affrontato a livello firmware/protocollo.