# SYNAPSE × AVERYN — Visione Strategica Completa
## Il "Perché", il "Cosa" e il "Come" di Due Progetti che Diventano Uno Solo Business

---

## 🎯 L'Obiettivo Finale (In Una Frase)

> **Creare l'unico ecosistema al mondo dove un hardware bio-sensing open-source di livello ricerca (SYNAPSE) viene venduto a consumatori sportivi mainstream attraverso una piattaforma sportiva gratuita "alla Strava" (Averyn) che ne mostra il valore reale — senza paywall, senza lock-in, con self-hosting reale — finanziando il cloud con il margine sull'hardware.**

---

## 🧠 Il Contesto: Perché Esistono Questi Due Progetti

### SYNAPSE-24: La Nascita da un'Insoddisfazione Tecnica
SYNAPSE-24 nasce perché **non esiste oggi nessun wearable che faccia davvero "24/7 multimodale"**:
- Apple Watch / Garmin / Whoop: ottimi per sport, **zero neuro** (niente EEG, fNIRS, EDA research-grade)
- Muse / EmotiBit / OpenBCI: ottimi per neuro, **zero sport** (niente GPS, mappe, segmenti, social)
- Tutti: **closed source, cloud proprietario, dati bloccati, subscription obbligatorie**

SYNAPSE-24 ha già risolto la parte difficile: **il software**. Phase 0 completato significa:
- Algoritmi validati su dataset pubblici (WESAD stress ≥80%, MIT-BIH R-peak ≥99.6%, Sleep-EDF staging)
- Pipeline completa: acquisizione → signal quality → edge AI (TFLM) → LSL/XDF sync
- Architettura tiered (T0 continuo, T1 sonno/riposo, T2 task) che risolve il problema energetico
- Hardware abstraction per ESP32-S3, Cerelog, EmotiBit, PiEEG, in-ear EEG

**Ma manca il prodotto vendibile** e manca il canale per arrivare agli utenti.

### Averyn: La Nascita da un'Insoddisfazione Utente
Averyn nasce perché **Strava ha tradito i suoi utenti**:
- Paywall su funzioni base (segmenti, analisi, mappe offline)
- Dati non esportabili facilmente, API restrittive
- Nessun self-hosting reale, dipendenza totale dal loro cloud
- Nessuna integrazione hardware aperta (solo partner selezionati)

Averyn è **pre-alpha**: ha solo l'architettura (KMP, Ktor, PostGIS, Docker Compose) e un piano dettagliato. Manca tutto il prodotto.

---

## 💡 L'Intuizione Chiave: I Due Problemi Si Risolvono a Vicenda

| Problema SYNAPSE | Soluzione Averyn |
|------------------|------------------|
| Hardware research-grade non vendibile a massa | Averyn = canale distribution + showcase valore |
| Nessun cloud per AI batch / multi-device / history | Averyn backend + Synapse Cloud = infrastruttura condivisa |
| Utenti non capiscono "perché serve EEG/HRV/stress" | Averyn traduce in "Readiness, Sleep Quality, Stress Trends" — linguaggio sportivo |

| Problema Averyn | Soluzione SYNAPSE |
|-----------------|-------------------|
| "Solo GPS" = commodity, stesso di Strava | Synapse Band = **differenziazione unica**: HRV reale, stress, sleep staging, readiness |
| Nessun hardware proprio = dipende da Garmin/Apple | Synapse Band = hardware proprietario, margine, controllo totale |
| Self-hosting costoso da mantenere | Synapse hardware sales finanziano cloud gestito gratuito |

---

## 🏗️ L'Architettura di Business (Non Tecnica)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        ECOSYSTEM FLYWHEEL                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   ┌──────────────┐      ┌──────────────┐      ┌──────────────┐             │
│   │  UTENTE      │      │   AVERYN     │      │  SYNAPSE     │             │
│   │  SPORTIVO    │◄────▶│   APP (Free) │◄────▶│   BAND       │             │
│   │              │      │              │      │  (€249)      │             │
│   └──────┬───────┘      └──────┬───────┘      └──────┬───────┘             │
│          │                     │                     │                      │
│          │  Compra Band        │  Mostra Readiness   │  Invia HRV/Stress    │
│          │  (margine ~50%)     │  Sleep/Stress       │  Sleep/Recovery      │
│          ▼                     ▼                     ▼                      │
│   ┌──────────────────────────────────────────────────────────────────┐     │
│   │                    SYNAPSE CLOUD (AGPL, Self-hostable)           │     │
│   │  • Batch AI: Sleep Staging (YASA), HRV Trends, Personalizzazione │     │
│   │  • Multi-device Sync, History, Backup                            │     │
│   │  • Finanziato da: margine hardware (€129/Band ≈ 3-4 anni cloud)  │     │
│   └──────────────────────────────────────────────────────────────────┘     │
│          │                     │                     │                      │
│          │  Self-host option   │  Open Source        │  Open Hardware       │
│          │  (Docker Compose)   │  (AGPL/MIT)         │  (Schemi, BOM, FW)   │
│          ▼                     ▼                     ▼                      │
│   ┌──────────────────────────────────────────────────────────────────┐     │
│   │                    COMMUNITY & ECOSYSTEM                         │     │
│   │  • Contributors condivisi (firmware, app, cloud, ML)             │     │
│   │  • Ricercatori usano Band + Averyn per studi                     │     │
│   │  • Developer costruiscono su SDK aperti                          │     │
│   │  • Nessun lock-in: esporti tutto, hosti tutto, modifichi tutto   │     │
│   └──────────────────────────────────────────────────────────────────┘     │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Il Modello Economico Sostenibile

| Voce | Valore | Note |
|------|--------|------|
| **Synapse Band BOM** | ~€120 | ESP32-S3, AD8232, MAX30102, ICM-20948, MAX-M10S, LiPo, PCB, case |
| **Prezzo Vendita** | €249 | Margine lordo ~€129 (52%) |
| **Costo Cloud/Utente/Anno** | ~€30-40 | CPU, storage, bandwidth, domain, monitoring |
| **Break-even Cloud** | 3-4 anni | Poi margine puro o reinvestimento in R&D |
| **Averyn Cloud Cost** | Condiviso | Stessa infrastruttura, costi marginali aggiuntivi |
| **Recurring Revenue** | Opzionale | Team/Club plans (€12-20/mese) — non paywall core |

**Nessuna subscription obbligatoria.** L'utente compra la Band, usa Averyn gratis per sempre. Se vuole cloud SYNAPSE dopo anno 1: €2-3/mese opzionale. Può sempre self-hostare.

---

## 🎭 Perché Questa Cosa Non È Mai Stata Fatta Prima

1. **Hardware companies** (Garmin, Apple, Whoop) → **chiudono tutto** per vendere subscription
2. **Open hardware projects** (OpenBCI, Muse) → **non fanno consumer apps**, restano nicchia ricerca
3. **Open source apps** (Gadgetbridge, OpenTracks) → **niente hardware**, niente cloud, niente AI
4. **Research projects** → **non pensano a business model**, finiscono in paper accademici

**Noi uniamo tutto**: Hardware open + App open + Cloud open + Business model sostenibile.

---

## 🎯 I Tre Pilastri Non Negoziali

### 1. **Open Source Reale, Non "Open Core"**
- SYNAPSE firmware: MIT (fai quello che vuoi, anche chiudi derivati)
- Averyn app/backend: AGPL-3.0 (se offri come servizio, condividi modifiche)
- Synapse Cloud: AGPL-3.0 + CLA (stesso principio)
- **Nessuna funzionalità core dietro paywall** — mai

### 2. **Self-Hosting Come Prima Classe, Non Afterthought**
- Docker Compose one-click per tutto (Averyn + Synapse Cloud)
- Tailscale fallback per utenti senza dominio/IP pubblico
- Documentazione completa: backup, restore, upgrade, troubleshooting
- **L'utente deve poter dire "non mi fido del vostro cloud" e andarsene in 30 min**

### 3. **Hardware Come Prodotto, Non Come Prototipo Perpetuo**
- Synapse Band v1: **solo sensori sport** (ECG, PPG, IMU, GPS, Temp) — vendibile in 6-9 mesi
- EEG/fNIRS → Synapse Headband v2 (repo separata, timeline 12-18 mesi)
- **Niente vaporware**: se non si può produrre a €249 con margine, non si promette

---

## 📍 Dove Siamo Oggi (Reality Check)

| Progetto | Stato Reale | Gap Principale |
|----------|-------------|----------------|
| **SYNAPSE-24** | Phase 0 ✅ (software validated). Phase 1 ready (€90 breadboard rig). Firmware ESP32-S3 parziale. | Manca: Band firmware completo, BLE GATT standard, FIT writer, OTA, provisioning, Python SDK |
| **Averyn** | Scaffold only. MVP-0 (GPS tracking) in corso. Architettura solida. | Manca: tutto il prodotto (tracking, analysis, social, maps, segments, Synapse section) |
| **Synapse Cloud** | **Non esiste** | Da creare da zero (FastAPI, TimescaleDB, Celery, ONNX Runtime, Authentik) |
| **Integrazione** | Zero | Da definire protocolli, data models, consent, UI |

---

## 🗺️ La Roadmap Realistica (Non Ottimistica)

### Fase 1: Fondamenta (Mesi 1-3) — **Quello Che Facciamo ORA**
- [ ] **SYNAPSE-24 Restructure**: separa device-side da ML/cloud/datasets
- [ ] **Synapse Band Firmware v1**: ECG+PPG+IMU+GPS+Temp + BLE GATT + FIT + OTA
- [ ] **Python SDK `synapse-device`**: discover, configure, record, stream, export
- [ ] **Synapse Cloud Skeleton**: docker-compose, Authentik, TimescaleDB, MinIO, Caddy
- [ ] **Averyn MVP-0**: GPS tracking affidabile su device reali (requisito base)

### Fase 2: Integrazione Minima (Mesi 3-5)
- [ ] **Averyn + Synapse Band**: BleSensorAdapter, FitImporter, DeviceManager
- [ ] **Synapse Cloud Ingestion + Inference Worker**: sleep staging, HRV trends, stress daily
- [ ] **Averyn SynapseCloudClient**: chiama cloud per metriche neuro
- [ ] **Averyn Web Synapse Section**: Readiness, Sleep, Stress, HRV Trends, Device Mgmt

### Fase 3: Prodotto Vendibile (Mesi 5-9)
- [ ] **PCB Synapse Band** (non breadboard): design, fab, assembly, test
- [ ] **Certificazioni base** (CE, FCC, RoHS) — non medicale, wellness
- [ ] **Produzione batch 100-500 unità**: supply chain, logistica, supporto
- [ ] **Averyn MVP-1/2/3**: Core platform, Analysis, Maps — parità Strava

### Fase 4: Espansione (Anno 2+)
- [ ] **Synapse Headband** (EEG 8ch + fNIRS) — per ricercatori/quantified-self avanzato
- [ ] **Synapse Cloud ML avanzato**: pattern recognition neuro, cognitive load, biomarker discovery
- [ ] **Ecosystem**: SDK per developer, marketplace plugin, research partnerships
- [ ] **Team/Club plans**: revenue recurring per sostenere crescita

---

## ⚠️ I Rischi Reali (E Come Li Mitighiamo)

| Rischio | Probabilità | Impatto | Mitigazione |
|---------|-------------|---------|-------------|
| **Hardware production delays** | Alta | Critico | Inizia PCB design ORA in parallelo; usa CM flessibili; buffer 3 mesi |
| **BLE/GPS/HRV reliability su massa** | Alta | Critico | Test su 20+ device reali prima di produzione; dogfooding esteso |
| **Cloud costs > hardware margin** | Media | Alto | Monitoring costi da giorno 1; modelli edge-first; batch not real-time |
| **AGPL managed cloud complexity** | Media | Medio | CLA bot da giorno 1; legal review prima di launch; documenta tutto |
| **Averyn scope creep (Strava parity)** | Alta | Alto | MVP verticali rigorosi; no social/mappe finché tracking non è rock-solid |
| **Single developer burnout** | Alta | Critico | Automazione CI/CD; issue templates; community contributors prima possibile |
| **Competitor copia (Garmin aggiunge HRV/stress)** | Certa | Medio | Differenziazione: open source, self-host, neuro futuro, community, prezzo |

---

## 🧭 Le Decisioni Chiave Già Presi (E Perché)

| Decisione | Scelta | Motivazione |
|-----------|--------|-------------|
| **Licenza Synapse Cloud** | AGPL-3.0 + CLA | Coerenza con Averyn; protegge da cloud provider che rivendono; managed service = convenience non monopoly |
| **Auth Unificato** | Authentik (non Keycloak) | RAM 500MB vs 2GB+; Python-based; sufficiente per OIDC/SAML; self-host friendly |
| **AI Deployment** | Hybrid (Edge TFLM + Cloud ONNX) | Real-time su device (stress triage, motion); batch pesante su cloud (YASA, personalizzazione) |
| **Synapse Band v1 Sensori** | Solo sport (no EEG/fNIRS) | Vendibile in 6-9 mesi; BOM €120; margine chiaro; EEG/fNIRS = repo/prodotto separato |
| **Self-Host TLS** | Caddy + Tailscale fallback | Utenti non tecnici: niente dominio/porte → Tailscale; tecnici: dominio + Let's Encrypt auto |
| **Business Model** | Hardware margin funds cloud | Nessuna subscription obbligatoria; allineato incentivi; sostenibile se volumi >500/anno |

---

## 📝 Conclusione: Cosa Significa Successo

**Tra 12 mesi, successo significa:**

1. **Synapse Band v1** prodotta (100+ unità vendute), firmware stabile, OTA funzionante
2. **Averyn** usabile quotidianamente da 100+ runner/ciclisti (tracking + analysis + Synapse section)
3. **Synapse Cloud** self-hostable con docker-compose, gestisce sleep/HRV/stress per utenti Band
4. **Community** di 10+ contributor attivi tra firmware, app, cloud, ML
5. **Cash flow positivo** su hardware (margine copre cloud + R&D base)

**Tra 24 mesi, successo significa:**

- Synapse Headband in pre-order per ricercatori
- Averyn = alternativa credibile a Strava (parità funzionale 80%+)
- Ecosistema developer attivo (plugin, integrazi, research studies)
- Modello replicabile: nuovo hardware → nuova sezione Averyn → stesso flywheel

---

## 🤝 Il Patto Implicito Tra Noi (Te e Me)

> **Tu fai le decisioni strategiche. Io traduco in specifiche tecniche eseguibili.**

Questa documentazione non è "quello che devi fare" — è **"quello che abbiamo deciso insieme di fare, scritto chiaro così non ci perdiamo"**.

Ogni file che segue è un **contratto tecnico** derivato da questa visione. Se qualcosa non torna, lo cambiamo *qui*, non nel codice.

---

*Documento vivo. Aggiornato ad ogni decision point. Versione 1.0 — 26 Settembre 2026*