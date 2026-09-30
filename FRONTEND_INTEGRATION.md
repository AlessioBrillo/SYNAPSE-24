# SYNAPSE-24 Frontend Integration Guide

This document describes the integration of the new React-based dashboard (`feature/frontend-v2`) with the existing SYNAPSE-24 Python acquisition stack.

## Overview

The dashboard consists of two parts:
1. **Backend** (`backend/`) - FastAPI server exposing Python acquisition state via REST + WebSocket
2. **Frontend** (`frontend/`) - React + TypeScript + Vite + Tailwind dashboard consuming the API

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Browser (React)                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐              │
│  │  Overview    │  │ LiveSignals  │  │SignalQuality │  ...        │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘              │
│         │                 │                 │                      │
│         └─────────────────┼─────────────────┘                      │
│                           ▼                                        │
│              ┌────────────────────────┐                            │
│              │   useData Hooks        │                            │
│              │  (polling + WS)        │                            │
│              └───────────┬────────────┘                            │
│                          │                                          │
│              ┌────────────▼────────────┐                            │
│              │   API Client (axios)    │                            │
│              └───────────┬────────────┘                            │
└──────────────────────────┼────────────────────────────────────────┘
                           │ HTTP / WS
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      Backend (FastAPI)                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐              │
│  │ /api/status  │  │ /api/quality │  │ /api/live    │  ...        │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘              │
│         │                 │                 │                      │
│         └─────────────────┼─────────────────┘                      │
│                           ▼                                        │
│              ┌────────────────────────┐                            │
│              │  Tier1Coordinator      │                            │
│              │  LSLGateway            │                            │
│              │  SignalQuality         │                            │
│              └────────────────────────┘                            │
└─────────────────────────────────────────────────────────────────────┘
```

## Data Flow

### 1. System Status (2s polling)
```
Frontend: useSystemStatus(2000)
    │
    ▼
Backend: GET /api/status
    │
    ▼
Python: Tier1Coordinator.get_status()
    │
    ▼
Returns: {
  current_tier: "T1",
  tier1_pods: { "head_pod": {...} },
  tier0_pods: { "forearm_hub": {...} },
  inear_pods: { "in_ear_satellite": {...} },
  sync: { pods: {...} },
  controller: { motion_gate: {...}, recent_transitions: [...] }
}
```

### 2. Live Signals (500ms polling + WebSocket)
```
Frontend: useLiveData(podId, 10s, 500)
    │
    ▼
Backend: GET /api/live/{pod_id}?duration_s=10
    │
    ▼
Python: LSLGateway.get_recent_data(pod_id, 10.0)
    │
    ▼
Returns: { timestamps: [...], data: {...}, sampling_rate: 500 }
```

### 3. Signal Quality (3s polling)
```
Frontend: useSignalQuality(podId, 3000)
    │
    ▼
Backend: GET /api/quality/{pod_id}
    │
    ▼
Python: signal_quality.compute_ecg_quality(), compute_ppg_quality(), etc.
```

### 4. Real-time Updates (WebSocket)
```
Backend: WS /ws/live
    │
    ▼
Periodic broadcast: { type: "status_update", data: {...} }
    │
    ▼
Frontend: useWebSocket() → updates local state
```

## TypeScript ↔ Python Type Mapping

| Python (dataclass) | TypeScript (interface) | Location |
|---|---|---|
| `Tier1Coordinator.get_status()` | `SystemStatus` | `src/types/index.ts` |
| `LSLGateway.get_sync_status()` | `SyncStatus` | `src/types/index.ts` |
| `signal_quality.SignalQualityMetrics` | `SignalQualityMetrics` | `src/types/index.ts` |
| `hardware.yaml` pods | `PodConfig` | `src/types/index.ts` |

## Development Workflow

### 1. Start Backend
```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install -e ../src  # Install SYNAPSE-24
uvicorn main:app --reload
# Server at http://localhost:8000
```

### 2. Start Frontend
```bash
cd frontend
npm install
npm run dev
# Vite at http://localhost:5173 (proxies /api to :8000)
```

### 3. With Docker
```bash
docker-compose up
# Frontend: http://localhost:5173
# Backend:  http://localhost:8000
```

## Connecting Real Hardware

### 1. Disable Mock Mode
```bash
# backend/.env
SYNAPSE_MOCK_MODE=false
```

### 2. Configure Hardware
Edit `config/hardware.yaml` with your pod configurations.

### 3. Set BLE/Serial in Environment
```bash
# backend/.env
SYNAPSE_FOREARM_BLE=AA:BB:CC:DD:EE:FF
SYNAPSE_FOREARM_SERIAL=/dev/ttyUSB0
SYNAPSE_HEAD_BLE=11:22:33:44:55:66
SYNAPSE_HEAD_SERIAL=/dev/ttyUSB1
SYNAPSE_EAR_BLE=77:88:99:AA:BB:CC
```

### 4. Replace Mock Functions in `backend/main.py`

```python
# Current mock:
MOCK_POD_STATUS = {...}

# Replace with real implementation:
from synapse24.acquisition.tier1_coordinator import Tier1Coordinator
from synapse24.acquisition.lsl_gateway import LSLGateway

coordinator = Tier1Coordinator(...)
gateway = LSLGateway(...)

@app.get("/api/status")
async def get_status():
    return coordinator.get_status()

@app.get("/api/live/{pod_id}")
async def get_live_data(pod_id: str, duration_s: float = 10.0):
    data, timestamps = gateway.get_recent_data(pod_id, duration_s)
    return {"pod_id": pod_id, "timestamps": timestamps.tolist(), ...}
```

## API Contract

### GET /api/status
Response: `SystemStatus`

### GET /api/sync
Response: `SyncStatus`

### GET /api/quality/{pod_id}
Response: `PodSignalQuality`

### GET /api/live/{pod_id}?duration_s=10
Response: `LiveDataPoint`

### POST /api/control/tier
Request: `{ target_tier: "T0"|"T1"|"T2", reason: "user_requested" }`
Response: `{ success: true, target_tier: "T1" }`

### POST /api/control/record
Request: `{ action: "start"|"stop", session_name?: string, duration_s?: number }`
Response: `{ success: true, session_name: "session_123" }`

### GET /api/config
Response: `HardwareConfig`

### WS /ws/live
Messages: `{ type: "status_update", data: SystemStatus }`

## Pages & Components

| Page | Component | Data Sources |
|------|-----------|--------------|
| Overview | `Overview.tsx` | `/api/status` |
| Live Signals | `LiveSignals.tsx` | `/api/live/{pod}`, `/api/status` |
| Signal Quality | `SignalQuality.tsx` | `/api/quality/{pod}` |
| Clock Sync | `SyncMonitor.tsx` | `/api/sync`, `/api/status` |
| Tier Control | `TierControl.tsx` | `/api/status`, `POST /api/control/tier` |
| Pod Manager | `PodManager.tsx` | `/api/config`, `/api/status` |
| Recording | `RecordingPanel.tsx` | `/api/status`, `POST /api/control/record` |

## Adding New Metrics

1. **Add Python computation** in `synapse24/signal_quality/` or acquisition modules
2. **Expose via API** in `backend/main.py`
3. **Add TypeScript type** in `frontend/src/types/index.ts`
4. **Create hook** in `frontend/src/hooks/useData.ts`
5. **Build component** in `frontend/src/components/`

## Testing

### Backend
```bash
cd backend
pytest tests/ -v
```

### Frontend
```bash
cd frontend
npm run lint
npm run build
# E2E: playwright test
```

## Deployment

### Production Build
```bash
cd frontend && npm run build
# Output in frontend/dist/
```

### Docker Production
```bash
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

## Troubleshooting

### "No pods available" in Live Signals
- Check backend `/api/status` returns pod data
- Verify `SYNAPSE_MOCK_MODE=true` for development
- Check browser network tab for API errors

### WebSocket connection fails
- Verify backend allows CORS from `localhost:5173`
- Check `/ws/live` endpoint in browser dev tools

### TypeScript errors after Python changes
- Update types in `frontend/src/types/index.ts`
- Run `npm run build` to verify

## Future Enhancements

- [ ] Server-sent events (SSE) fallback for WebSocket
- [ ] Persistent session storage (IndexedDB)
- [ ] Offline mode with local mock data
- [ ] Plugin system for custom visualizations
- [ ] Multi-user authentication
- [ ] Export to PDF/CSV from dashboard