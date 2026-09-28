# SYNAPSE-24 — Piano di Ristrutturazione Completo
## Da "Monorepo Research" a "Device Software Platform" Vendibile

---

## 🎯 Obiettivo della Ristrutturazione

> **Trasformare SYNAPSE-24 da repository di ricerca (Phase 0 completato) in una Device Software Platform pulita: solo codice che gira sui device (firmware, embedded SDKs, tooling), con interfacce standard (BLE GATT, FIT, MQTT, LSL) per qualsiasi consumer (Averyn, ricercatori, developer terzi).**

---

## 📊 Stato Attuale: Cosa C'è Oggi in `src/synapse24/`

```
src/synapse24/
├── acquisition/          # ✅ CORE DEVICE-SIDE (state machine, clock sync, coordinator)
├── config/               # ✅ CORE (schema hardware.yaml)
├── edge_ai/              # ⚠️ MISTO (deployment ✅, training/quantization ❌)
├── hardware/             # ✅ CORE DEVICE-SIDE (driver, registry)
├── ingestion/            # ❌ DATASET PIPELINES (public data, non device)
├── signal_quality/       # ⚠️ MISTO (base ✅, full Python ❌, embedded subset ✅)
└── utils/                # ⚠️ MISTO (XDF writer ✅, reader/validator ❌)
```

---

## 🗂️ Nuova Struttura Target (Post-Ristrutturazione)

```
SYNAPSE-24/                          # REPO PRINCIPALE: Device Software Platform
├── firmware/                        # NUOVA: Solo embedded C/Rust
│   ├── esp32_band/                  # Band v1 (ECG+PPG+IMU+GPS+Temp)
│   ├── esp32_headband/              # Futuro: EEG+fNIRS (repo separata poi)
│   └── common/                      # Shared: drivers, RTOS abstraction, crypto
│
├── sdk/                             # NUOVA: Host SDKs per "qualsiasi dispositivo"
│   ├── python/                      # `pip install synapse-device`
│   ├── cpp/                         # Embedded SDK per altri MCU/RTOS
│   └── rust/                        # Opzionale: high-performance host tools
│
├── config/                          # Schema hardware Band/Headband
├── tools/                           # Tooling produzione/calibrazione
├── docs/                            # Documentazione device-side
├── scripts/                         # Solo device-relevant (validate_phase1_entry, quantize_and_deploy)
├── tests/                           # Solo unit/integration device
├── pyproject.toml                   # Solo deps device-side
├── README.md                        # Focus device platform
├── LICENSE                          # MIT (invariato)
├── CONTRIBUTING.md                  # Aggiornato
└── ARCHITECTURE.md                  # Device-side only
```

---

## 📦 Repo Separate da Creare (Split Fuori da SYNAPSE-24)

### 1. `synapse-ml` — Machine Learning & Training
- Training pipelines (WESAD, MIT-BIH, Sleep-EDF, DEAP)
- Quantization scripts (INT8, Float16)
- Public dataset ingestion
- Evaluation, baseline validation, closure gates
- Model registry, versioning, artifacts
- **Licenza**: MIT

### 2. `synapse-cloud` — Cloud Ingestion & Batch Inference
- Docker Compose one-click stack
- Auth Gateway (OAuth2 Proxy), Ingestion API (FastAPI), Inference Worker (Celery+ONNX)
- REST API, Notification Service, Web Dashboard (React)
- TimescaleDB + MinIO + Redis + EMQX + Authentik
- **Licenza**: AGPL-3.0 + CLA

### 3. `synapse-datasets` (Opzionale) — Public Dataset Utilities
- Download/preprocessing scripts per dataset pubblici
- **Licenza**: MIT

---

## ✂️ Piano di Migrazione Dettagliato (4 Settimane)

### Settimana 1: Struttura + Porting Core Firmware
- Crea nuova struttura cartelle
- Porta `acquisition/`, `hardware/`, `config/` da Python a C (firmware/esp32_band/)
- Definisci BLE GATT Custom Service UUIDs e caratteristiche

### Settimana 2: Firmware Features Critiche
- `ble_gatt_server.c`: Standard services + Custom Synapse Service
- `fit_writer.c`: FIT file writer su LittleFS (valido per Garmin/Strava/Averyn)
- `provisioning.c`: BLE provisioning flow (WiFi + Cloud URL + Device Cert)
- `ota_update.c`: Signed OTA con Ed25519, rollback automatico
- `acquisition_fsm.c`: T0/T1/T2 state machine + Motion Gate

### Settimana 3: Python SDK + Split Repo
- `sdk/python/synapse_device/`: CLI `synapse discover/configure/record/stream/export/ota/calibrate`
- Crea repo `synapse-ml` e `synapse-cloud` da split
- Pulizia SYNAPSE-24 originale (rimuovi tutto non-device)

### Settimana 4: Documentazione + CI/CD + Validation
- `docs/device-integration-guide.md`: BLE, FIT, MQTT, LSL per developer terzi
- `docs/ble-gatt-spec.md`: Spec completa per Averyn integration
- CI/CD: firmware build test, SDK test, lint su 3 repo
- Dogfooding: registra sessione reale Band → FIT → importa in Averyn

---

## 🔴 Cosa NON Va Nella Nuova SYNAPSE-24 (Checklist di Pulizia)

| Categoria | Rimuovi/Spsta | Destinazione |
|-----------|---------------|--------------|
| ML Training | `edge_ai/training.py`, `quantization.py`, `wesad_*.py`, `scripts/train_*` | `synapse-ml/` |
| Dataset Ingestion | `ingestion/` (tutto), `scripts/ingest_datasets.py`, `download_datasets.py` | `synapse-ml/` |
| Signal Quality Full | `signal_quality/fnirs.py`, `eeg.py`, `ecg.py` (full), `ppg.py` (full) | Embedded in firmware; full in `synapse-ml/` |
| XDF/Utils Cloud | `utils/xdf.py`, `xdf_correction.py`, `scripts/validate_live_*.py` | `synapse-cloud/` |
| Test Non-Device | `tests/test_edge_ai.py`, `test_ingestion.py`, `test_*_closure*.py` | `synapse-ml/` |
| Hardware EEG/fNIRS | `hardware/cerelog.py`, `emotibit.py`, `inear_eeg.py` | `firmware/esp32_headband/` (futuro) |

---

## ✅ Definition of Done per Ristrutturazione

- [ ] `firmware/esp32_band/` compila con ESP-IDF 5.2+, gira su ESP32-S3 DevKit
- [ ] BLE GATT Server espone tutti i servizi standard + custom Synapse
- [ ] `synapse record` genera `.fit` valido (leggibile da Garmin Connect, Strava, Averyn)
- [ ] `synapse provision` configura WiFi + Cloud URL + Cert su device vergine
- [ ] `synapse ota` aggiorna firmware signed con rollback
- [ ] Python SDK installabile via `pip install -e sdk/python`
- [ ] `synapse-ml` repo indipendente, training pipelines girano
- [ ] `synapse-cloud` repo indipendente, docker-compose up funziona
- [ ] Documentazione `docs/device-integration-guide.md` completa
- [ ] CI/CD attivo su tutte e 3 le repo

---

## 💡 Note Critiche per l'Esecuzione

1. **Porting Python → C non è banale**: Usa i test Python come reference oracle. State machine, clock sync, signal quality vanno riscritti in C embedded.
2. **FIT Writer è critico**: Deve produrre file validi per `fitparse` / Garmin Connect. Usa libreria C esistente o scrivi minimal writer.
3. **Provisioning UX**: Deve funzionare "out of box" — utente apre Averyn App → "Aggiungi Band" → magia. Testa con non-tecnici.
4. **OTA Security**: Firma Ed25519, public key in firmware, private key offline (HSM o air-gapped). Non saltare.
5. **Certificati Device**: Ogni Band ha cert X.509 unico (mTLS con cloud). Generazione in factory test.

---

*Questo piano è eseguibile da un solo developer in 4 settimane se focalizzato. Parallelizza: firmware (tu) + SDK Python (contributor) + Cloud skeleton (contributor).*