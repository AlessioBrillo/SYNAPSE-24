## Summary

Complete rewrite of the frontend dashboard with real SYNAPSE-24 acquisition stack integration.

### Changes

**Backend** (`backend/`) - FastAPI server:
- REST API exposing Tier1Coordinator, LSLGateway, signal_quality data
- WebSocket for real-time status updates
- Tier control (T0/T1/T2) and XDF recording endpoints
- Mock mode for development without hardware

**Frontend** (`frontend/`) - React + TypeScript + Vite + Tailwind:
- Overview: System metrics, pod groups, transitions, power budget
- LiveSignals: Real-time plotting (EEG, ECG, PPG, ACC, GYRO, MAG)
- SignalQuality: Per-modality metrics (SNR, SQI, MAP, impedance, HRV)
- SyncMonitor: Clock drift with T0 (10ms/60s) / T1 (1ms/10s) budgets
- TierControl: Manual override + auto transition rules
- PodManager: Device config from hardware.yaml + .env
- RecordingPanel: XDF session recording with validation tools

**Architecture**:
- TypeScript types mirror Python dataclasses exactly
- Custom hooks for data fetching (polling + WebSocket)
- Docker Compose for local development
- Full integration guide (FRONTEND_INTEGRATION.md)

### Archive

Original mock frontend preserved at tag `archive/feature-frontend-v1`.

### Testing

- Backend: Run with `uvicorn main:app --reload` (mock mode)
- Frontend: Run with `npm run dev` (proxies to :8000)
- Docker: `docker-compose up`