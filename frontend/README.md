# SYNAPSE-24 Dashboard Frontend

React + TypeScript + Vite + Tailwind CSS dashboard for real-time multimodal biosignal monitoring.

## Features

- **Live Signals**: Real-time plotting of EEG, ECG, PPG, ACC, GYRO, MAG streams
- **Signal Quality**: Per-modality quality metrics (SNR, SQI, MAP, impedance, HRV)
- **Clock Sync Monitor**: Multi-pod drift visualization with per-tier budgets (T0: 10ms/60s, T1: 1ms/10s)
- **Tier Control**: Manual override + automatic transition rules (T0 ↔ T1 ↔ T2)
- **Pod Manager**: Device configuration from hardware.yaml + .env
- **Recording Panel**: XDF session recording with drift correction

## Quick Start

### Prerequisites

- Node.js 20+
- Backend API running on port 8000 (see `../backend`)

### Development

```bash
# Install dependencies
npm install

# Start dev server (proxies /api to localhost:8000)
npm run dev

# Build for production
npm run build

# Preview production build
npm run preview

# Lint
npm run lint
```

### With Docker

```bash
# From project root
docker-compose up frontend
```

## Architecture

```
src/
├── api/           # Axios client + WebSocket
├── components/    # React components (one per page)
├── hooks/         # Custom React hooks for data fetching
├── types/         # TypeScript types (mirror Python dataclasses)
├── App.tsx        # Main app with routing
└── main.tsx       # Entry point
```

## API Integration

The frontend consumes these backend endpoints:

| Endpoint | Purpose |
|----------|---------|
| `GET /api/status` | Full system status (Tier1Coordinator) |
| `GET /api/sync` | Clock sync status (LSLGateway) |
| `GET /api/quality/{pod_id}` | Signal quality metrics |
| `GET /api/live/{pod_id}` | Recent time-series data |
| `POST /api/control/tier` | Force tier change |
| `POST /api/control/record` | Start/stop XDF recording |
| `GET /api/config` | Hardware configuration |
| `WS /ws/live` | Real-time updates |

## TypeScript Types

Types in `src/types/index.ts` mirror Python dataclasses from:
- `synapse24.acquisition.tier1_coordinator.Tier1Coordinator.get_status()`
- `synapse24.acquisition.lsl_gateway.LSLGateway.get_sync_status()`
- `synapse24.signal_quality` base types

## Styling

- Tailwind CSS with custom `synapse` color palette
- Dark theme optimized for long monitoring sessions
- Responsive grid layouts

## Adding New Pages

1. Create component in `src/components/`
2. Add to `PAGES` array in `App.tsx`
3. Add navigation item in `Sidebar.tsx`
4. Implement data fetching in `hooks/useData.ts`