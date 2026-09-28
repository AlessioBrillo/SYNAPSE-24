# ADR-0010: Synapse Cloud Architecture
## Cloud Ingestion, Batch Inference & Multi-Device Sync per Synapse Ecosystem

---

## 📋 Status
**Accepted** — 27 Settembre 2026

---

## 🎯 Context

Synapse Band v1 genera dati ad alta frequenza (ECG 500Hz, PPG 64Hz, IMU 100Hz, GPS 1Hz) che richiedono:
1. **Ingestion affidabile** da migliaia di device (MQTT/HTTPS, mTLS, backpressure)
2. **Batch inference** per modelli pesanti: Sleep Staging (YASA), HRV Trends, Personalizzazione modelli
3. **Multi-device sync** per utenti con Band + Headband (future) + Phone sensors
4. **REST API** per Averyn Backend e Web Dashboard ricercatori
5. **Self-hostable one-click** (Docker Compose) con auto-TLS (Caddy + Tailscale fallback)
6. **AGPL-3.0 + CLA** per coerenza con Averyn e protezione da cloud provider

---

## 🏗️ Decision

### Stack Tecnologico

| Layer | Technology | Rationale |
|-------|------------|-----------|
| **API Gateway / Auth** | **Authentik** (OIDC/SAML) + **OAuth2 Proxy** (Go) | RAM ~500MB vs Keycloak 2GB+; Python-based; self-host friendly; sufficient per OIDC |
| **Ingestion API** | **FastAPI** (Python 3.11+) | Async nativo, Pydantic validation, OpenAPI auto, team ML-friendly |
| **Message Queue** | **EMQX** (MQTT Broker) + **Redis Streams** | EMQX: rule engine per pre-processing, cluster-ready, rule engine SQL; Redis: cache + Celery broker |
| **Time-Series DB** | **TimescaleDB** (PostgreSQL extension) | SQL nativo, JOIN con dati utente/device, compressione automatica, retention policies |
| **Object Storage** | **MinIO** (S3-compatible) | XDF raw files, firmware binaries, model artifacts; versioning, lifecycle |
| **Batch Inference** | **Celery** + **ONNX Runtime** (Python) | YASA/sklearn → ONNX; task queue scalable; GPU optional via ONNX Runtime CUDA |
| **REST API** | **FastAPI** (shared con ingestion) | Single codebase, shared auth/models |
| **Notifications** | **ntfy.sh** (self-hosted) + **Firebase/APNs** (mobile push) | ntfy: unified push/email/webhook; mobile nativo per app |
| **Web Dashboard** | **React 18 + TypeScript + Vite** | Ricercatori: hypnogram, signal quality, raw data explorer |
| **Reverse Proxy / TLS** | **Caddy** | Auto-TLS Let's Encrypt + Tailscale internal HTTPS; config 5 righe |
| **Orchestration** | **Docker Compose** (dev/self-host) + **Helm/K8s** (managed) | One-click self-host; K8s per scaling gestito |

### Architettura Servizi

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         SYNAPSE CLOUD (Docker Compose)                       │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────┐  │
│  │   Caddy      │    │  Authentik   │    │   EMQX       │    │  Redis   │  │
│  │  (TLS/Proxy) │◄───│  (OIDC IdP)  │    │  (MQTT Broker)│───▶│ (Cache/  │  │
│  │              │    │              │    │  Rule Engine │    │  Celery) │  │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘    └────┬─────┘  │
│         │                   │                   │                 │        │
│         ▼                   ▼                   ▼                 ▼        │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                    OAUTH2 PROXY (Go)                                  │   │
│  │         Valida JWT RS256 → Headers X-User-ID, X-Roles, X-Consent     │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│         │                   │                   │                 │        │
│         ▼                   ▼                   ▼                 ▼        │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────┐  │
│  │  Ingestion   │    │   Inference  │    │    REST      │    │  Notif-  │  │
│  │  API         │    │   Worker     │    │   API        │    │  ication │  │
│  │  (FastAPI)   │    │  (Celery+    │    │  (FastAPI)   │    │  Service │  │
│  │              │    │   ONNX)      │    │              │    │          │  │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘    └────┬─────┘  │
│         │                   │                   │                 │        │
│         ▼                   ▼                   ▼                 ▼        │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────┐  │
│  │ TimescaleDB  │◄───│   MinIO      │    │  PostgreSQL  │    │  ntfy/   │  │
│  │ (Metrics,    │    │  (XDF raw,   │    │  (Authentik, │    │  Firebase│  │
│  │  Sessions,   │    │   Models,    │    │   Users,     │    │  APNs)   │  │
│  │  Devices)    │    │   Firmware)  │    │   Consent)   │    │          │  │
│  └──────────────┘    └──────────────┘    └──────────────┘    └──────────┘  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼ External Consumers
         ┌────────────────────┬────────────────────┬────────────────────┐
         ▼                    ▼                    ▼                    ▼
   ┌──────────┐         ┌──────────┐         ┌──────────┐         ┌──────────┐
   │  Averyn  │         │  Web     │         │  Third-  │         │  CLI/SDK │
   │  Backend │         │  Dash-   │         │  Party   │         │  (Python)│
   │  (Ktor)  │         │  board   │         │  Apps    │         │          │
   └──────────┘         └──────────┘         └──────────┘         └──────────┘
```

---

## 📊 Data Model (TimescaleDB + PostgreSQL)

### Core Tables (TimescaleDB Hypertables)

```sql
-- Dispositivi registrati
CREATE TABLE devices (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    serial          VARCHAR(32) UNIQUE NOT NULL,      -- Es. "SYN-BAND-A1B2"
    hardware_version VARCHAR(16) NOT NULL,            -- "band-v1.0"
    firmware_version VARCHAR(16) NOT NULL,
    user_id         UUID REFERENCES users(id),
    status          VARCHAR(16) DEFAULT 'active',     -- active, inactive, decommissioned
    last_seen       TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now(),
    metadata        JSONB DEFAULT '{}'
);

SELECT create_hypertable('devices', 'created_at', chunk_time_interval => '1 month');

-- Sessioni di acquisizione (metadati, non raw data)
CREATE TABLE sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id       UUID REFERENCES devices(id),
    user_id         UUID REFERENCES users(id),
    sport_type      VARCHAR(32),                      -- running, cycling, sleep, generic
    started_at      TIMESTAMPTZ NOT NULL,
    ended_at        TIMESTAMPTZ,
    duration_sec    INTEGER,
    timezone        VARCHAR(64),
    gps_enabled     BOOLEAN DEFAULT false,
    eeg_enabled     BOOLEAN DEFAULT false,
    fnirs_enabled   BOOLEAN DEFAULT false,
    xdf_object_key  VARCHAR(256),                     -- MinIO key per XDF raw
    fit_object_key  VARCHAR(256),                     -- MinIO key per FIT export
    status          VARCHAR(16) DEFAULT 'completed',  -- completed, partial, failed
    created_at      TIMESTAMPTZ DEFAULT now()
);

SELECT create_hypertable('sessions', 'started_at', chunk_time_interval => '1 day');

-- Metriche calcolate (batch inference results)
CREATE TABLE metrics (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      UUID REFERENCES sessions(id),
    user_id         UUID REFERENCES users(id),
    metric_type     VARCHAR(64) NOT NULL,             -- readiness, sleep, stress, hrv_trend, signal_quality
    algorithm_version VARCHAR(64) NOT NULL,           -- readiness-synapse-v1, sleep-yasa-v2, etc.
    computed_at     TIMESTAMPTZ DEFAULT now(),
    valid_from      TIMESTAMPTZ NOT NULL,             -- Periodo a cui si riferisce
    valid_to        TIMESTAMPTZ,
    value           JSONB NOT NULL,                   -- Structured metric payload
    confidence      FLOAT,                            -- 0.0-1.0
    created_at      TIMESTAMPTZ DEFAULT now()
);

SELECT create_hypertable('metrics', 'computed_at', chunk_time_interval => '1 day');
CREATE INDEX idx_metrics_user_type ON metrics(user_id, metric_type, valid_from DESC);

-- Inference Jobs (Celery task tracking)
CREATE TABLE inference_jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      UUID REFERENCES sessions(id),
    user_id         UUID REFERENCES users(id),
    task_type       VARCHAR(64) NOT NULL,             -- sleep_staging, hrv_trends, stress_daily, personalize
    status          VARCHAR(16) DEFAULT 'pending',    -- pending, running, completed, failed, retry
    priority        INTEGER DEFAULT 5,
    input_refs      JSONB,                            -- MinIO keys, parameters
    output_refs     JSONB,                            -- MinIO keys, metric IDs
    error_message   TEXT,
    attempts        INTEGER DEFAULT 0,
    max_attempts    INTEGER DEFAULT 3,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now()
);

SELECT create_hypertable('inference_jobs', 'created_at', chunk_time_interval => '1 day');

-- Consensi utente per scopi dati
CREATE TABLE user_consents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID REFERENCES users(id),
    device_id       UUID REFERENCES devices(id),
    scope           VARCHAR(64) NOT NULL,             -- hrv_analysis, sleep_staging, stress_monitoring, etc.
    granted         BOOLEAN NOT NULL,
    granted_at      TIMESTAMPTZ DEFAULT now(),
    revoked_at      TIMESTAMPTZ,
    UNIQUE(user_id, device_id, scope)
);
```

### MinIO Bucket Structure
```
synapse-data/
├── raw-xdf/                    # XDF files from devices
│   └── {user_id}/{device_id}/{session_id}.xdf
├── fit-exports/                # FIT files generated by device/cloud
│   └── {user_id}/{device_id}/{session_id}.fit
├── models/                     # ONNX model artifacts (versioned)
│   ├── sleep_staging/
│   │   ├── v1/model.onnx
│   │   └── v1/labels.json
│   ├── stress_triage/
│   ├── hrv_trends/
│   └── personalization/
├── firmware/                   # Signed firmware binaries
│   ├── band-v1/
│   │   ├── v1.0.0/firmware.bin
│   │   ├── v1.0.0/firmware.bin.sig
│   │   └── v1.0.0/manifest.json
│   └── headband-v1/
└── exports/                    # User-initiated exports (CSV, XDF, JSON)
    └── {user_id}/export_{timestamp}.zip
```

---

## 🔄 Inference Pipeline (Celery Tasks)

```python
# services/inference-worker/tasks.py
from celery import Celery
import onnxruntime as ort
import numpy as np
from datetime import datetime, timedelta

app = Celery('synapse_inference', broker='redis://redis:6379/0')

# Load models at startup (singleton per worker)
MODELS = {
    'sleep_staging': ort.InferenceSession('models/sleep_staging/v1/model.onnx'),
    'stress_daily': ort.InferenceSession('models/stress_triage/v1/model.onnx'),
    'hrv_trends': ort.InferenceSession('models/hrv_trends/v1/model.onnx'),
    # Personalization: per-user fine-tuned models loaded on-demand
}

@app.task(bind=True, max_retries=3, default_retry_delay=300)
def run_sleep_staging(self, session_id: str):
    """YASA sleep staging on night session (requires EEG or HRV+ACC proxy)."""
    session = db.get_session(session_id)
    xdf_data = minio.download(session.xdf_object_key)
    
    # Extract features: HRV time/freq, ACC, EDA if available
    features = extract_sleep_features(xdf_data)
    
    # Run YASA ONNX model
    probs = MODELS['sleep_staging'].run(None, {'input': features})[0]
    stages = probs.argmax(axis=1)  # 0=W, 1=N1, 2=N2, 3=N3, 4=REM
    
    # Compute sleep metrics
    metrics = compute_sleep_metrics(stages, xdf_data.timestamps)
    
    # Store metrics + hypnogram
    metric_id = db.insert_metric(
        session_id=session_id,
        metric_type='sleep',
        algorithm_version='sleep-yasa-v1',
        valid_from=session.started_at,
        valid_to=session.ended_at,
        value={
            'hypnogram': stages.tolist(),
            'sleep_metrics': metrics,
            'confidence': float(probs.max(axis=1).mean())
        }
    )
    
    minio.upload(f'exports/hypnogram/{session_id}.json', stages)
    return metric_id

@app.task(bind=True, max_retries=3)
def run_hrv_weekly_trend(self, user_id: str, week_start: str):
    """Weekly HRV trend analysis with personalization."""
    sessions = db.get_sessions(user_id, week_start, week_start + 7 days)
    hrv_data = aggregate_hrv(sessions)
    
    # Personalized baseline (per-user model if exists)
    user_model_key = f'models/personalization/{user_id}/hrv_baseline.onnx'
    if minio.exists(user_model_key):
        model = ort.InferenceSession(minio.download(user_model_key))
    else:
        model = MODELS['hrv_trends']  # Population model
    
    trend = model.run(None, {'hrv_history': hrv_data})[0]
    
    db.insert_metric(
        user_id=user_id,
        metric_type='hrv_trend',
        algorithm_version='hrv_trend-personalized-v1',
        valid_from=week_start,
        valid_to=week_start + 7 days,
        value={'trend': trend.tolist(), 'baseline_hrv': hrv_data.mean()}
    )

@app.task(bind=True, max_retries=3)
def run_stress_daily(self, user_id: str, date: str):
    """Daily stress summary from continuous T0 monitoring."""
    sessions = db.get_sessions(user_id, date, date + 1 day)
    stress_data = aggregate_stress_triage(sessions)  # From edge TFLM results
    
    db.insert_metric(
        user_id=user_id,
        metric_type='stress',
        algorithm_version='stress-daily-v1',
        valid_from=date,
        valid_to=date + 1 day,
        value={
            'daily_average': stress_data.mean(),
            'peak': stress_data.max(),
            'episodes': detect_stress_episodes(stress_data),
            'recovery_score': compute_recovery(stress_data)
        }
    )

@app.task(bind=True, max_retries=3)
def run_personalization(self, user_id: str):
    """Monthly model personalization using user's accumulated data."""
    # Collect 30+ days of labeled data
    training_data = collect_personalization_data(user_id, days=30)
    
    if len(training_data) < MIN_SAMPLES:
        return  # Skip, not enough data
    
    # Fine-tune population model (few-shot / LoRA style)
    personalized_model = fine_tune_population_model(
        base_model='models/hrv_trends/v1/model.onnx',
        user_data=training_data
    )
    
    # Save per-user model
    minio.upload(f'models/personalization/{user_id}/hrv_baseline.onnx', personalized_model)
    
    # Update model registry
    db.update_user_model_version(user_id, 'hrv_trends', new_version)
```

---

## 🔐 Authentication & Authorization

### Authentik Realm Configuration
```yaml
# keycloak/realm-export.json (key fields)
{
  "realm": "synapse",
  "clients": [
    {
      "clientId": "synapse-ingestion-api",
      "serviceAccountsEnabled": true,
      "directAccessGrantsEnabled": false,
      "standardFlowEnabled": false,
      "attributes": {
        "access.token.lifespan": "3600"
      }
    },
    {
      "clientId": "synapse-rest-api",
      "serviceAccountsEnabled": true,
      "standardFlowEnabled": true,
      "redirectUris": ["https://synapse.mydomain.com/callback"],
      "webOrigins": ["https://synapse.mydomain.com"]
    },
    {
      "clientId": "averyn-backend",
      "serviceAccountsEnabled": true,
      "attributes": {
        "access.token.lifespan": "3600"
      }
    },
    {
      "clientId": "synapse-web-dashboard",
      "standardFlowEnabled": true,
      "redirectUris": ["https://synapse.mydomain.com/dashboard/callback"],
      "webOrigins": ["https://synapse.mydomain.com"]
    },
    {
      "clientId": "synapse-mobile-android",
      "standardFlowEnabled": true,
      "redirectUris": ["com.averyn.app://oauth2redirect"],
      "publicClient": true
    },
    {
      "clientId": "synapse-mobile-ios",
      "standardFlowEnabled": true,
      "redirectUris": ["com.averyn.app://oauth2redirect"],
      "publicClient": true
    }
  ],
  "roles": {
    "realm": [
      {"name": "device:read", "description": "Read device telemetry"},
      {"name": "device:write", "description": "Configure device, OTA"},
      {"name": "metrics:read", "description": "Read computed metrics"},
      {"name": "metrics:write", "description": "Trigger inference jobs"},
      {"name": "consent:manage", "description": "Manage data consent scopes"},
      {"name": "admin", "description": "Full administrative access"}
    ]
  },
  "defaultRoles": ["device:read", "metrics:read"]
}
```

### Token Claims (JWT RS256)
```json
{
  "sub": "user_uuid",
  "email": "user@example.com",
  "realm_access": {
    "roles": ["device:read", "metrics:read", "consent:manage"]
  },
  "client_roles": {
    "synapse-ingestion-api": ["device:write"],
    "averyn-backend": ["metrics:read", "device:read"]
  },
  "consent": {
    "synapse:hrv_analysis": true,
    "synapse:sleep_staging": true,
    "synapse:stress_monitoring": true,
    "synapse:personalization": false,
    "synapse:research_sharing": false
  },
  "device_ids": ["SYN-BAND-A1B2", "SYN-HEAD-C3D4"]
}
```

### OAuth2 Proxy Config (Ingress)
```yaml
# services/auth-gateway/oauth2-proxy.cfg
provider = "oidc"
oidc_issuer_url = "https://auth.synapse.mydomain.com/application/o/synapse/"
client_id = "synapse-ingestion-api"
client_secret = "${OAUTH2_PROXY_CLIENT_SECRET}"
cookie_secret = "${OAUTH2_PROXY_COOKIE_SECRET}"
cookie_domains = [".synapse.mydomain.com"]
cookie_expire = "168h"
cookie_secure = true
email_domains = ["*"]
upstreams = [
  "http://ingestion-api:8000",
  "http://rest-api:8001",
  "http://inference-worker:8002"
]
skip_auth_preflight = true
pass_access_token = true
pass_authorization_header = true
set_xauthrequest = true
```

---

## 🐳 Docker Compose One-Click (Self-Host)

```yaml
# docker-compose.yml
version: '3.8'

services:
  # Reverse Proxy + Auto TLS
  caddy:
    image: caddy:2.8
    ports: ["80:80", "443:443", "443:443/udp"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data
      - caddy_config:/config
    environment:
      - ACME_AGREE=true
      - DOMAIN=${SYNAPSE_DOMAIN:-synapse.local}
      - TAILSCALE_AUTHKEY=${TAILSCALE_AUTHKEY:-}
    networks: [synapse-net]
    depends_on: [authentik, ingestion-api, rest-api, web-dashboard]

  # Identity Provider
  authentik:
    image: ghcr.io/goauthentik.io/server:2024.8
    environment:
      - AUTHENTIK_BOOTSTRAP_PASSWORD=${AUTHENTIK_BOOTSTRAP_PASSWORD}
      - AUTHENTIK_BOOTSTRAP_EMAIL=${AUTHENTIK_BOOTSTRAP_EMAIL}
      - AUTHENTIK_SECRET_KEY=${AUTHENTIK_SECRET_KEY}
      - POSTGRES_HOST=postgres
      - POSTGRES_DB=authentik
      - POSTGRES_USER=authentik
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
      - REDIS_HOST=redis
    volumes:
      - authentik_media:/media
    networks: [synapse-net]
    depends_on: [postgres, redis]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/health"]
      interval: 30s
      timeout: 10s
      retries: 3

  # PostgreSQL (shared: Authentik + TimescaleDB + Users)
  postgres:
    image: timescale/timescaledb:latest-pg16
    environment:
      - POSTGRES_DB=synapse
      - POSTGRES_USER=synapse
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
      - POSTGRES_MULTIPLE_DATABASES=synapse,authentik
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./migrations:/docker-entrypoint-initdb.d
    networks: [synapse-net]
    command: >
      postgres -c shared_preload_libraries=timescaledb
               -c max_connections=200
               -c timescaledb.max_background_workers=8

  # Redis (Cache + Celery Broker)
  redis:
    image: redis:7-alpine
    volumes: [redis_data:/data]
    networks: [synapse-net]
    command: redis-server --maxmemory 512mb --maxmemory-policy allkeys-lru

  # MQTT Broker with Rule Engine
  emqx:
    image: emqx/emqx:5.6
    ports: ["1883:1883", "8883:8883", "8083:8083", "18083:18083"]
    environment:
      - EMQX_NAME=emqx
      - EMQX_HOST=127.0.0.1
      - EMQX_DASHBOARD__DEFAULT_USERNAME=admin
      - EMQX_DASHBOARD__DEFAULT_PASSWORD=${EMQX_DASHBOARD_PASSWORD}
      - EMQX_AUTH__HTTP__CONNECT_URL=http://ingestion-api:8000/api/v1/auth/mqtt/connect
    volumes:
      - emqx_data:/opt/emqx/data
      - emqx_etc:/opt/emqx/etc
    networks: [synapse-net]
    depends_on: [ingestion-api]

  # MinIO Object Storage
  minio:
    image: minio/minio:RELEASE.2024-08-17
    ports: ["9000:9000", "9001:9001"]
    environment:
      - MINIO_ROOT_USER=minioadmin
      - MINIO_ROOT_PASSWORD=${MINIO_ROOT_PASSWORD}
      - MINIO_DOMAIN=${SYNAPSE_DOMAIN:-synapse.local}
    volumes: [minio_data:/data]
    networks: [synapse-net]
    command: server /data --console-address ":9001"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 30s

  # Ingestion API (FastAPI)
  ingestion-api:
    build: ./services/ingestion-api
    environment:
      - DATABASE_URL=postgresql://synapse:${POSTGRES_PASSWORD}@postgres:5432/synapse
      - REDIS_URL=redis://redis:6379/0
      - MINIO_ENDPOINT=minio:9000
      - MINIO_ACCESS_KEY=minioadmin
      - MINIO_SECRET_KEY=${MINIO_ROOT_PASSWORD}
      - MINIO_BUCKET=synapse-data
      - EMQX_HOST=emqx
      - EMQX_PORT=1883
      - JWT_PUBLIC_KEY_PATH=/keys/jwt_public.pem
    volumes:
      - ./keys:/keys:ro
    networks: [synapse-net]
    depends_on: [postgres, redis, minio, emqx]
    deploy:
      resources:
        limits: {cpus: '1', memory: '1G'}

  # Inference Worker (Celery + ONNX)
  inference-worker:
    build: ./services/inference-worker
    environment:
      - DATABASE_URL=postgresql://synapse:${POSTGRES_PASSWORD}@postgres:5432/synapse
      - REDIS_URL=redis://redis:6379/0
      - MINIO_ENDPOINT=minio:9000
      - MINIO_ACCESS_KEY=minioadmin
      - MINIO_SECRET_KEY=${MINIO_ROOT_PASSWORD}
      - MINIO_BUCKET=synapse-data
      - CELERY_CONCURRENCY=2
    volumes:
      - ./models:/models:ro
    networks: [synapse-net]
    depends_on: [postgres, redis, minio]
    deploy:
      resources:
        limits: {cpus: '2', memory: '4G'}
        reservations: {devices: [{driver: nvidia, count: 1, capabilities: [gpu]}]}

  # REST API (FastAPI)
  rest-api:
    build: ./services/rest-api
    environment:
      - DATABASE_URL=postgresql://synapse:${POSTGRES_PASSWORD}@postgres:5432/synapse
      - REDIS_URL=redis://redis:6379/0
      - MINIO_ENDPOINT=minio:9000
      - MINIO_ACCESS_KEY=minioadmin
      - MINIO_SECRET_KEY=${MINIO_ROOT_PASSWORD}
      - JWT_PUBLIC_KEY_PATH=/keys/jwt_public.pem
    volumes:
      - ./keys:/keys:ro
    networks: [synapse-net]
    depends_on: [postgres, redis, minio]

  # Notification Service
  notification-service:
    build: ./services/notification-service
    environment:
      - NTFY_URL=http://ntfy:80
      - FIREBASE_CREDENTIALS_PATH=/keys/firebase.json
      - APNS_KEY_PATH=/keys/apns.p8
    volumes:
      - ./keys:/keys:ro
    networks: [synapse-net]
    depends_on: [ntfy]

  # ntfy (Unified Push)
  ntfy:
    image: binwiederhier/ntfy:latest
    environment:
      - NTFY_BASE_URL=https://synapse.mydomain.com/notify
      - NTFY_AUTH_DEFAULT_ACCESS=deny-all
    volumes:
      - ntfy_data:/var/lib/ntfy
      - ./ntfy-config.yml:/etc/ntfy/server.yml:ro
    networks: [synapse-net]

  # Web Dashboard (React)
  web-dashboard:
    build: ./services/web-dashboard
    environment:
      - VITE_API_URL=https://synapse.mydomain.com/api
      - VITE_AUTH_URL=https://auth.synapse.mydomain.com
    networks: [synapse-net]
    depends_on: [rest-api]

volumes:
  postgres_data:
  redis_data:
  minio_data:
  emqx_data:
  emqx_etc:
  ntfy_data:
  caddy_data:
  caddy_config:
  authentik_media:

networks:
  synapse-net:
    driver: bridge
```

### Caddyfile (Auto-TLS + Tailscale)
```caddy
# Caddyfile
{
    admin off
    email {$SYNAPSE_ADMIN_EMAIL}
    # Let's Encrypt per dominio pubblico
    # Tailscale fallback se TAILSCALE_AUTHKEY impostato
}

{$SYNAPSE_DOMAIN} {
    # Authentik
    handle_path /auth/* {
        reverse_proxy authentik:9000
    }
    
    # Ingestion API (device MQTT/HTTP)
    handle_path /ingest/* {
        reverse_proxy ingestion-api:8000
    }
    
    # REST API (Averyn, Web, CLI)
    handle_path /api/* {
        reverse_proxy rest-api:8000
    }
    
    # Web Dashboard
    handle_path /dashboard/* {
        reverse_proxy web-dashboard:3000
    }
    
    # MinIO Console
    handle_path /minio/* {
        reverse_proxy minio:9001
    }
    
    # EMQX Dashboard
    handle_path /emqx/* {
        reverse_proxy emqx:18083
    }
    
    # ntfy
    handle_path /notify/* {
        reverse_proxy ntfy:80
    }
}

# Tailscale HTTPS (se configurato)
{$TAILSCALE_DOMAIN} {
    tls internal
    import {$SYNAPSE_DOMAIN}
}
```

---

## 🔌 API Contracts (OpenAPI Summary)

### Ingestion API (Device → Cloud)
```yaml
# POST /api/v1/ingest/{device_id}/telemetry
# Headers: Authorization: Bearer <device_jwt>, Content-Type: application/msgpack
# Body: MsgPack encoded telemetry batch
{
  "session_id": "uuid",
  "timestamp": "2026-09-27T10:30:00.123Z",
  "ecg": [float32; 500],      # 1 second @ 500Hz
  "ppg": [float32; 64],       # 1 second @ 64Hz
  "imu_acc": [float32; 100],  # 1 second @ 100Hz
  "imu_gyro": [float32; 100],
  "gps": {"lat": 45.123, "lon": 9.456, "alt": 120, "speed": 3.2},
  "temp": 36.5,
  "battery": 85,
  "signal_quality": {"ecg_sqi": 0.92, "ppg_sqi": 0.78, "ppg_map": 0.15},
  "stress_triage": 0          # 0=baseline, 1=stress, 2=artifact
}
```

### REST API (Averyn Backend → Cloud)
```yaml
# GET /api/v1/metrics/readiness?user_id={uuid}&date=2026-09-27
# Headers: Authorization: Bearer <service_account_jwt>
Response:
{
  "score": 78,
  "hrv_component": 82,
  "sleep_component": 75,
  "stress_component": 80,
  "recovery_component": 72,
  "factors": ["HRV 5% below baseline", "Sleep efficiency 82%"],
  "algorithm_version": "readiness-synapse-v1"
}

# GET /api/v1/metrics/sleep?user_id={uuid}&date=2026-09-27
Response:
{
  "score": 85,
  "total_sleep_time": "7h 23m",
  "deep_sleep_percent": 18.2,
  "rem_percent": 22.1,
  "awake_percent": 5.3,
  "sleep_efficiency": 0.89,
  "hrv_during_sleep": {"rmssd": 52, "sdnn": 68, "lf_hf": 1.4},
  "hypnogram": [0,0,0,1,2,3,3,2,1,4,4,1,0...],  # 30s epochs
  "algorithm_version": "sleep-yasa-v1"
}

# GET /api/v1/devices/{device_id}/firmware/latest?hw_version=band-v1.0
Response:
{
  "version": "1.2.0",
  "url": "https://synapse.mydomain.com/firmware/band-v1.0/v1.2.0/firmware.bin",
  "signature": "base64_ed25519_signature",
  "release_notes": "Improved HRV accuracy, fixed GPS cold start",
  "required": false
}
```

---

## 💰 Resource Requirements (Self-Host Minimum)

| Component | CPU | RAM | Storage | Notes |
|-----------|-----|-----|---------|-------|
| **Caddy** | 0.1 | 50 MB | 100 MB | TLS certs, logs |
| **Authentik** | 0.5 | 500 MB | 1 GB | Postgres + Redis external |
| **PostgreSQL + TimescaleDB** | 1 | 1 GB | 10 GB+ | TimescaleDB compression ~90% |
| **Redis** | 0.1 | 256 MB | 100 MB | Cache + Celery broker |
| **EMQX** | 0.5 | 200 MB | 500 MB | Rule engine, clustering |
| **MinIO** | 0.5 | 500 MB | 20 GB+ | XDF raw ~50MB/night/user |
| **Ingestion API** | 1 | 1 GB | 100 MB | FastAPI, async |
| **Inference Worker** | 2 | 4 GB | 5 GB | + GPU optional (ONNX CUDA) |
| **REST API** | 0.5 | 500 MB | 100 MB | FastAPI |
| **Notification** | 0.1 | 100 MB | 50 MB | ntfy + Firebase/APNs |
| **Web Dashboard** | 0.1 | 200 MB | 500 MB | Static files served by Caddy |
| **TOTAL** | **~6.5** | **~7.5 GB** | **~35 GB+** | **RPi 4 8GB borderline; Mini PC N100 16GB consigliato** |

---

## 🚀 Deployment Scenarios

| Scenario | Infrastructure | Costo Mensile Stimato | Utenti Supportati |
|----------|----------------|----------------------|-------------------|
| **Self-Host Home** | Mini PC (N100, 16GB, 512GB NVMe) ~€200 | €0 (elettricità ~€3/mese) | 1-10 (famiglia/team) |
| **Self-Host VPS** | Hetzner CX42 (8 vCPU, 16GB, 160GB) | €16/mese | 50-100 |
| **Managed Cloud (Our SaaS)** | K8s (GKE/EKS) + Cloud SQL + Cloud Storage | €200-500/mese base | 1000+ |
| **Enterprise Self-Host** | K8s on-prem + Harbor + Vault | Variabile | 10000+ |

---

## ✅ Consequences

### Positive
- **Full self-host parity**: Stesso codice gira in managed cloud e self-host
- **AGPL compliance**: Chiunque offra Synapse Cloud come servizio deve condividere modifiche
- **ML-native**: ONNX Runtime + Celery = modelli Python → produzione senza rewrite
- **TimescaleDB**: SQL per JOIN complessi (utenti + dispositivi + metriche + consensi)
- **EMQX Rule Engine**: Pre-processing MQTT → HTTP ingestion senza codice custom
- **Caddy + Tailscale**: Self-host "funziona subito" anche dietro NAT/CGNAT

### Negative / Rischi
- **Complessità operativa**: 12+ container per self-host (mitigato da docker-compose + healthchecks)
- **Authentik learning curve**: Meno documentazione di Keycloak (mitigato: config esportata come code)
- **ONNX Runtime GPU**: Richiede driver NVIDIA host-side per GPU (mitigato: CPU-only default, GPU optional)
- **Model versioning**: Gestione modelli per-utente in MinIO richiede discipline (mitigato: task `run_personalization` automatizzato)

### Neutral
- **Python ecosystem**: Team ML a proprio agio; team backend Kotlin deve imparare FastAPI/Pydantic
- **TimescaleDB licensing**: TimescaleDB Community è Apache-2.0 (compatibile AGPL)

---

## 🔗 Related ADRs
- **ADR-0011**: Unified Auth with Authentik (prossimo)
- **ADR-0002**: Averyn License AGPL-3.0 (coerenza)
- **ADR-0005**: Ktor Modular Monolith (Averyn backend separation)

---

*ADR scritto per essere eseguibile. Ogni sezione "Stack", "Data Model", "Docker Compose" è copiabile direttamente in repo `synapse-cloud`.*