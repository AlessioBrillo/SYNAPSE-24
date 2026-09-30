# SYNAPSE-24 Dashboard Backend

FastAPI server exposing real-time data from Tier1Coordinator, LSLGateway, and signal quality modules to the React frontend.

## Features

- **REST API**: System status, signal quality, live data, configuration
- **WebSocket**: Real-time updates for live monitoring
- **Tier Control**: Programmatic tier transitions (T0/T1/T2)
- **Recording Control**: XDF session start/stop
- **Mock Mode**: Works without hardware for development

## Quick Start

### Prerequisites

- Python 3.11+
- SYNAPSE-24 source installed (`pip install -e ../src`)
- Optional: Redis for caching

### Development

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows

# Install dependencies
pip install -r requirements.txt

# Install SYNAPSE-24 in development mode
pip install -e ../src

# Run server
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### With Docker

```bash
# From project root
docker-compose up backend
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/health` | Health check |
| GET | `/api/status` | Full system status |
| GET | `/api/sync` | Clock synchronization status |
| GET | `/api/quality/{pod_id}` | Signal quality for pod |
| GET | `/api/live/{pod_id}` | Recent time-series data |
| POST | `/api/control/tier` | Force tier change |
| POST | `/api/control/record` | Start/stop recording |
| GET | `/api/config` | Hardware configuration |
| WS | `/ws/live` | Real-time status updates |

## Environment Variables

Create `.env` from `.env.example`:

```bash
cp .env.example .env
# Edit with your device details
```

| Variable | Description | Default |
|----------|-------------|---------|
| `SYNAPSE_CONFIG_DIR` | Config directory path | `../config` |
| `SYNAPSE_LSL_NETWORK` | LSL network scope | `local` |
| `SYNAPSE_MOCK_MODE` | Use mock data | `true` |
| `REDIS_URL` | Redis connection | `redis://localhost:6379` |

## Project Structure

```
backend/
├── main.py          # FastAPI app + routes
├── requirements.txt # Python dependencies
├── Dockerfile       # Container definition
└── .env.example     # Environment template
```

## Connecting Real Hardware

1. Set `SYNAPSE_MOCK_MODE=false` in `.env`
2. Configure `hardware.yaml` with pod details
3. Set BLE MAC addresses in `.env`:
   ```bash
   SYNAPSE_FOREARM_BLE=AA:BB:CC:DD:EE:FF
   SYNAPSE_FOREARM_SERIAL=/dev/ttyUSB0
   SYNAPSE_HEAD_BLE=11:22:33:44:55:66
   SYNAPSE_HEAD_SERIAL=/dev/ttyUSB1
   SYNAPSE_EAR_BLE=77:88:99:AA:BB:CC
   ```
4. Ensure LSL is running on the network

## Development Notes

- Mock data in `main.py` matches real API structure
- Replace mock functions with real `Tier1Coordinator`/`LSLGateway` calls
- WebSocket broadcasts status every 1 second (configurable)
- CORS configured for `localhost:5173` (Vite dev server)