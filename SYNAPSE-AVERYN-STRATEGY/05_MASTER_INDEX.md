# SYNAPSE × AVERYN — Master Index
## Tutta la Documentazione Strategica e Tecnica in Un Solo Posto

---

## 📚 Indice Documenti

| # | File | Descrizione | Pubblico |
|---|------|-------------|----------|
| **00** | [VISIONE_STRATEGICA_COMPLETA.md](00_VISIONE_STRATEGICA_COMPLETA.md) | **Il "Perché": business model, flywheel, risk, roadmap 24 mesi** | Founder, Investitori, Team |
| **01** | [SYNAPSE-24_RESTRUCTURE_PLAN.md](01_SYNAPSE-24_RESTRUCTURE_PLAN.md) | **Il "Cosa" per SYNAPSE-24: pulizia repo, firmware Band v1, SDK, split repo** | Embedded Engineers, ML Engineers |
| **02** | [AVERYN_SYNAPSE_INTEGRATION.md](02_AVERYN_SYNAPSE_INTEGRATION.md) | **Il "Come" per Averyn: BleAdapter, FitImporter, DeviceManager, Metriche, Web Section** | Mobile Engineers, Backend Engineers, Frontend Engineers |
| **03** | [SYNAPSE_CLOUD_ADR.md](03_SYNAPSE_CLOUD_ADR.md) | **Architettura Cloud: stack, data model, inference pipeline, docker-compose, API** | Cloud Engineers, DevOps, ML Engineers |
| **04** | [SYNAPSE_DEVICE_SDK_SPEC.md](04_SYNAPSE_DEVICE_SDK_SPEC.md) | **SDK Specification: Python/C/Rust CLI + Library, BLE GATT, FIT, MQTT, LSL, Provisioning, OTA** | Tutti i Developer (interni + esterni) |

---

## 🎯 Quick Start per Ruolo

### 🧠 **Founder / Product Owner**
1. Leggi **[00_VISIONE_STRATEGICA_COMPLETA.md](00_VISIONE_STRATEGICA_COMPLETA.md)** — Tutto il business logic
2. Controlla **Definition of Done** in ogni doc per milestone chiare
3. Usa **Roadmap 24 mesi** (Sezione 00) per planning investitori/team

### ⚡ **Embedded Engineer (Firmware SYNAPSE Band v1)**
1. **[01_SYNAPSE-24_RESTRUCTURE_PLAN.md](01_SYNAPSE-24_RESTRUCTURE_PLAN.md)** → Sezioni "Nuova Struttura", "Step 2-3", "Definition of Done"
2. **[04_SYNAPSE_DEVICE_SDK_SPEC.md](04_SYNAPSE_DEVICE_SDK_SPEC.md)** → Sezione C/C++ SDK + Esempio `main.c`
3. **Priorità assolute (Ordine)**:
   - `ble_gatt_server.c` (Standard + Custom Service)
   - `fit_writer.c` (LittleFS, validato Garmin/Strava)
   - `provisioning.c` (BLE → WiFi + Cloud + Cert)
   - `ota_update.c` (Ed25519, rollback)
   - `acquisition_fsm.c` (T0/T1/T2 + Motion Gate)

### 🤖 **ML Engineer (synapse-ml + synapse-cloud inference)**
1. **[01_SYNAPSE-24_RESTRUCTURE_PLAN.md](01_SYNAPSE-24_RESTRUCTURE_PLAN.md)** → Sezione "Repo Separate: synapse-ml"
2. **[03_SYNAPSE_CLOUD_ADR.md](03_SYNAPSE_CLOUD_ADR.md)** → Sezioni "Inference Pipeline", "Data Model", "Models"
3. **Task Celery da implementare**: `sleep_staging` (YASA→ONNX), `hrv_trends`, `stress_daily`, `personalization`

### ☁️ **Cloud/DevOps Engineer (synapse-cloud)**
1. **[03_SYNAPSE_CLOUD_ADR.md](03_SYNAPSE_CLOUD_ADR.md)** → **Tutto il documento** (stack, docker-compose, Caddyfile, Authentik, EMQX, MinIO, TimescaleDB)
2. **One-click self-host** = `docker-compose up -d` funziona out-of-box
3. **Managed service** = Helm charts in `synapse-cloud/helm/`

### 📱 **Mobile Engineer (Averyn Android/iOS)**
1. **[02_AVERYN_SYNAPSE_INTEGRATION.md](02_AVERYN_SYNAPSE_INTEGRATION.md)** → Sezioni 1-3 (`SynapseBleAdapter`, `SynapseFitImporter`, `SynapseDeviceManager`)
2. **[04_SYNAPSE_DEVICE_SDK_SPEC.md](04_SYNAPSE_DEVICE_SDK_SPEC.md)** → BLE GATT Reference + FIT Developer Data
3. **Definition of Done MVP**: BLE connect, FIT import, Provisioning <3min, OTA, Consent

### 🌐 **Backend Engineer (Averyn Ktor)**
1. **[02_AVERYN_SYNAPSE_INTEGRATION.md](02_AVERYN_SYNAPSE_INTEGRATION.md)** → Sezioni 4-5 (`SynapseCloudClient`, Nuove Metriche versionate)
2. **[03_SYNAPSE_CLOUD_ADR.md](03_SYNAPSE_CLOUD_ADR.md)** → API Contracts REST (Readiness, Sleep, Stress, HRV)
3. **Auth**: Service account JWT → OAuth2 Proxy → Synapse Cloud

### 🎨 **Frontend Engineer (Averyn Web React)**
1. **[02_AVERYN_SYNAPSE_INTEGRATION.md](02_AVERYN_SYNAPSE_INTEGRATION.md)** → Sezione 5 (Web Synapse Section components)
2. **Pagine**: `/synapse/readiness`, `/synapse/sleep`, `/synapse/stress`, `/synapse/hrv`, `/synapse/devices`
3. **Components**: `ReadinessCard`, `SleepHypnogram`, `DeviceManager`, `StressTimeline`

### 🔬 **Ricercatore / Developer Esterno**
1. **[04_SYNAPSE_DEVICE_SDK_SPEC.md](04_SYNAPSE_DEVICE_SDK_SPEC.md)** → **Tutto** (Python CLI, Library, C SDK, Rust SDK)
2. **Quick Start**: `pip install synapse-device` → `synapse discover` → `synapse record` → `synapse export`
3. **BLE GATT Standard**: Funziona con **qualsiasi** app BLE (nRF Connect, LightBlue, custom)
4. **FIT/XDF Export**: Importa in MATLAB, Python (MNE, pyxdf), LabChart, EEGLAB

---

## 🔄 Flussi di Lavoro Chiave (End-to-End)

### Flusso 1: Utente Compra Band → Usa Averyn
```
1. Utente riceve Synapse Band (box con QR code → Averyn App)
2. Averyn App: "Aggiungi Dispositivo" → Scan BLE → Trova SYN-BAND-XXXX
3. Provisioning Wizard: WiFi SSID/PASS → Cloud URL auto (synapse.mydomain.com)
4. Device genera CSR → Cloud firma cert → Device installa cert → mTLS OK
5. Band registrato su Synapse Cloud + Linkato a utente Averyn
6. Utente fa corsa → Band registra .fit su LittleFS
7. Fine corsa → Sync automatico (BLE → Phone → Averyn Backend → Synapse Cloud)
8. Synapse Cloud: Inference batch (Sleep, HRV, Stress, Readiness)
9. Averyn Backend: Polling Synapse Cloud REST API → Metriche pronte
10. Averyn App/Web: Mostra Readiness 78, Sleep 85%, Stress basso, HRV trend ↑
```

### Flusso 2: Ricercatore Usa Band per Studio
```
1. Ricercatore: `pip install synapse-device`
2. `synapse discover` → Trova Band + Headband
3. `synapse configure` → Provisioning lab WiFi + Cloud locale (synapse-cloud self-host)
4. `synapse record --duration 7200 --format xdf --output study_session.xdf`
5. `synapse stream --protocol lsl` → LSL stream per BioSemi/EEG concurrent recording
6. Analisi offline: `pyxdf.load_xdf()` → MNE-Python / EEGLAB / BrainVision
7. Nessun account Averyn, nessun cloud obbligatorio, dati 100% locali
```

### Flusso 3: Developer Terzo Costruisce App su Synapse Band
```
1. Developer legge `docs/device-integration-guide.md`
2. Usa BLE GATT Standard (HR, RSC, CSC, Battery) → Funziona subito
3. Aggiunge Synapse Custom Service per ECG/PPG/IMU/SQ/Stress raw
4. App pubblica su App Store / Play Store / F-Droid
5. Utenti Band possono usare l'app → Ecosistema cresce
6. Nessuna approvazione Synapse/Averyn necessaria (Open Hardware + Open SDK)
```

---

## ✅ Checklist Master (Tutto il Programma)

### Phase 1: Fondamenta (Mesi 1-3) — **IN CORSO**
- [ ] **SYNAPSE-24 Restructure** completata (3 repo: synapse-24, synapse-ml, synapse-cloud)
- [ ] **Synapse Band Firmware v1** su ESP32-S3: BLE GATT, FIT, Provisioning, OTA, FSM
- [ ] **Python SDK `synapse-device`** su PyPI: discover, configure, record, stream, export, ota
- [ ] **Synapse Cloud Skeleton**: docker-compose, Authentik, TimescaleDB, MinIO, EMQX, Caddy
- [ ] **Averyn MVP-0**: GPS tracking affidabile su device reali (requisito base)

### Phase 2: Integrazione Minima (Mesi 3-5)
- [ ] **Averyn + Synapse Band**: BleSensorAdapter, FitImporter, DeviceManager (Android + iOS)
- [ ] **Synapse Cloud Ingestion + Inference Worker**: sleep_staging, hrv_trends, stress_daily
- [ ] **Averyn SynapseCloudClient**: Service account auth, caching, error handling
- [ ] **Averyn Web Synapse Section**: Readiness, Sleep, Stress, HRV, Devices, Export
- [ ] **Metriche versionate**: Readiness, SleepQuality, StressTrend, HrvTrend in `shared/metrics`

### Phase 3: Prodotto Vendibile (Mesi 5-9)
- [ ] **PCB Synapse Band**: Design, Fab, Assembly, Test (100-500 unità)
- [ ] **Certificazioni**: CE, FCC, RoHS (wellness, non medicale)
- [ ] **Averyn MVP-1/2/3**: Core platform, Analysis, Maps — parità Strava 80%+
- [ ] **Self-Host Documentation**: Guide complete, troubleshooting, backup/restore
- [ ] **Community**: Contributor guide, CLA bot, issue templates, Discord/Forum

### Phase 4: Espansione (Anno 2+)
- [ ] **Synapse Headband** (EEG 8ch + fNIRS) — repo `synapse-headband-firmware`
- [ ] **Advanced ML**: Pattern recognition neuro, cognitive load, biomarker discovery
- [ ] **Ecosystem**: Plugin marketplace, Research partnerships, Developer program
- [ ] **Team/Club Plans**: Recurring revenue per sostenere crescita

---

## 🚨 Decisioni Bloccanti (Da Risolvere Questa Settimana)

| Decisione | Opzioni | Raccomandazione | Status |
|-----------|---------|-----------------|--------|
| **Authentik vs Keycloak** | Authentik (500MB RAM) vs Keycloak (2GB+) | **Authentik** per self-host | ⬜ Da confermare |
| **TimescaleDB vs InfluxDB** | Timescale (SQL, JOIN) vs Influx (FLUX, compression) | **TimescaleDB** (Postgres unico) | ⬜ Da confermare |
| **CLA Tool** | `cla-assistant` (bot) vs `DCO` + manual | **cla-assistant** (automatizzato) | ⬜ Da configurare |
| **TLS Self-Host** | Let's Encrypt (dominio pubblico) vs Tailscale (fallback) | **Both**: Caddy auto-detect | ⬜ Da testare |
| **ONNX Runtime GPU** | CPU-only default vs GPU optional | **CPU default**, GPU via `deploy.resources` | ⬜ Da confermare |

---

## 📞 Contatti & Prossimi Step

| Ruolo | Referente | Prossima Azione |
|-------|-----------|-----------------|
| **Architecture Owner** | Te | Approva/Modifica ADR-0010, ADR-0011 |
| **Embedded Lead** | Tu / Assunto | Inizia `firmware/esp32_band/main/ble_gatt_server.c` |
| **ML Lead** | Tu / Assunto | Crea repo `synapse-ml`, porta training pipelines |
| **Cloud Lead** | Tu / Assunto | Crea repo `synapse-cloud`, docker-compose up |
| **Mobile Lead** | Tu / Assunto | Inizia `SynapseBleAdapter` in Averyn Android |
| **Backend Lead** | Tu / Assunto | Inizia `SynapseCloudClient` in Averyn Ktor |
| **Frontend Lead** | Tu / Assunto | Inizia `SynapseSection` in Averyn Web |

---

## 💡 Ricorda: Il Contratto Fondamentale

> **SYNAPSE-24 = Device Software Platform** (firmware, SDK, tooling)  
> **AVERYN = Strava Free + Synapse Showcase** (app, backend, web, social, maps)  
> **SYNAPSE CLOUD = Ingestion + Batch AI + Sync** (FastAPI, Celery, ONNX, TimescaleDB, AGPL)  
>  
> **Nessun codice cloud in SYNAPSE-24. Nessun firmware in Averyn. Nessuna UI in SYNAPSE CLOUD.**  
>  
> **Interfacce standard (BLE GATT, FIT, MQTT, LSL, REST) = Contratto inamovibile tra i tre.**

---

*Master Index vivo. Aggiornato ad ogni milestone. Versione 1.0 — 27 Settembre 2026*