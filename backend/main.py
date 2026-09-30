"""SYNAPSE-24 Dashboard Backend API.

FastAPI server exposing real-time data from Tier1Coordinator, LSLGateway,
and signal quality modules to the React frontend via REST + WebSocket.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global state for connected WebSocket clients
connected_clients: set[WebSocket] = set()

# Mock data for development (replace with real Tier1Coordinator/LSLGateway)
MOCK_POD_STATUS = {
    "current_tier": "T1",
    "tier1_pods": {
        "head_pod": {
            "name": "Head Pod (Cerelog)",
            "connected": True,
            "streaming": True,
            "impedance_checked": True,
            "quality_passed": True,
            "clock_offset_ms": 0.3,
            "error_count": 0,
            "last_data_age_s": 0.1,
        }
    },
    "tier0_pods": {
        "forearm_hub": {
            "name": "Forearm Hub (ESP32)",
            "connected": True,
            "streaming": True,
            "clock_offset_ms": -1.2,
            "error_count": 0,
            "last_data_age_s": 0.05,
        }
    },
    "inear_pods": {
        "in_ear_satellite": {
            "name": "In-Ear Satellite",
            "connected": True,
            "streaming": True,
            "clock_offset_ms": 0.8,
            "error_count": 0,
            "battery_remaining_pct": 87.0,
            "last_data_age_s": 0.2,
        }
    },
    "sync": {
        "pods": {
            "head_pod": {
                "offset_ms": 0.3,
                "drift_ppm": 0.1,
                "within_tolerance": True,
                "tier_evaluated": "T1",
            },
            "forearm_hub": {
                "offset_ms": -1.2,
                "drift_ppm": 0.5,
                "within_tolerance": True,
                "tier_evaluated": "T0",
            },
        }
    },
    "controller": {
        "current_tier": "T1",
        "tier1_duration_s": 1423.5,
        "tier2_duration_s": None,
        "transition_count": 3,
        "recent_transitions": [
            {"from": "T0", "to": "T1", "reason": "night_window_start", "timestamp": 1700000000.0},
            {"from": "T1", "to": "T0", "reason": "movement_detected", "timestamp": 1700001000.0},
            {"from": "T0", "to": "T1", "reason": "immobility_detected", "timestamp": 1700002000.0},
        ],
        "motion_gate": {
            "sqi_min": 0.5,
            "map_max": 0.5,
            "required_consecutive_clean": 2,
            "sqi_staleness_max_s": 30.0,
            "latest_sqi": 0.82,
            "latest_map": 0.15,
            "latest_sqi_age_s": 2.1,
            "consecutive_clean": 5,
            "armed": True,
        },
    },
}


class TierChangeRequest(BaseModel):
    """Request to change acquisition tier."""
    target_tier: str  # "T0", "T1", "T2"
    reason: str = "user_requested"


class RecordingControl(BaseModel):
    """Request to start/stop XDF recording."""
    action: str  # "start" or "stop"
    session_name: str | None = None
    duration_s: float | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    logger.info("Starting SYNAPSE-24 Dashboard API")
    # TODO: Initialize real Tier1Coordinator, LSLGateway here
    yield
    logger.info("Shutting down SYNAPSE-24 Dashboard API")


app = FastAPI(
    title="SYNAPSE-24 Dashboard API",
    description="Real-time API for multimodal biosignal acquisition dashboard",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS for frontend dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==================== REST Endpoints ====================

@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "service": "synapse24-dashboard-api"}


@app.get("/api/status")
async def get_status():
    """Get comprehensive system status from Tier1Coordinator."""
    # TODO: Replace with real coordinator.get_status()
    return MOCK_POD_STATUS


@app.get("/api/sync")
async def get_sync_status():
    """Get clock synchronization status."""
    return MOCK_POD_STATUS["sync"]


@app.get("/api/quality/{pod_id}")
async def get_signal_quality(pod_id: str):
    """Get real-time signal quality metrics for a pod."""
    # TODO: Connect to real signal_quality module
    mock_quality = {
        "head_pod": {
            "eeg": {"snr_db": 24.5, "impedance_kohm": [5.2, 4.8, 5.1, 4.9, 5.0, 5.3, 4.7, 5.1], "quality": "good"},
            "ecg": {"hr_bpm": 72, "rmssd_ms": 42.3, "snr_db": 18.7, "quality": "good"},
        },
        "forearm_hub": {
            "ppg": {"hr_bpm": 71, "sqi": 0.85, "map": 0.12, "quality": "good"},
            "ecg": {"hr_bpm": 72, "rmssd_ms": 41.8, "snr_db": 16.2, "quality": "good"},
            "acc": {"motion_intensity": 0.03, "posture": "supine", "quality": "good"},
        },
        "in_ear_satellite": {
            "eeg": {"snr_db": 18.2, "impedance_kohm": [12.1, 11.8], "quality": "warning"},
        },
    }
    return mock_quality.get(pod_id, {"error": "Pod not found"})


@app.get("/api/live/{pod_id}")
async def get_live_data(pod_id: str, duration_s: float = 10.0):
    """Get recent live data from LSLGateway ring buffer."""
    # TODO: Connect to real LSLGateway.get_recent_data()
    # Return mock time-series data
    import numpy as np
    n_samples = int(duration_s * 100)  # 100 Hz mock
    t = np.linspace(0, duration_s, n_samples)
    
    if pod_id == "head_pod":
        # Mock EEG (8 channels)
        data = np.random.randn(8, n_samples) * 10 + 50 * np.sin(2 * np.pi * 10 * t)
        return {
            "pod_id": pod_id,
            "timestamps": t.tolist(),
            "data": data.tolist(),
            "channels": [f"EEG{i+1}" for i in range(8)],
            "sampling_rate": 100,
        }
    elif pod_id == "forearm_hub":
        # Mock PPG + ACC
        ppg = 100000 + 5000 * np.sin(2 * np.pi * 1.2 * t) + np.random.randn(n_samples) * 100
        acc = np.random.randn(3, n_samples) * 0.1
        return {
            "pod_id": pod_id,
            "timestamps": t.tolist(),
            "data": {"ppg": ppg.tolist(), "acc": acc.tolist()},
            "sampling_rate": 100,
        }
    return {"error": "Pod not found"}


@app.post("/api/control/tier")
async def control_tier(request: TierChangeRequest):
    """Request tier change (T0 <-> T1 <-> T2)."""
    # TODO: Connect to real AcquisitionController
    valid_tiers = ["T0", "T1", "T2"]
    if request.target_tier not in valid_tiers:
        return {"error": f"Invalid tier. Must be one of {valid_tiers}"}
    
    logger.info(f"Tier change requested: {request.target_tier} ({request.reason})")
    return {"success": True, "target_tier": request.target_tier, "reason": request.reason}


@app.post("/api/control/record")
async def control_recording(request: RecordingControl):
    """Start or stop XDF recording."""
    # TODO: Connect to real recording system
    logger.info(f"Recording {request.action}: {request.session_name}")
    return {
        "success": True,
        "action": request.action,
        "session_name": request.session_name or f"session_{int(asyncio.get_event_loop().time())}",
    }


@app.get("/api/config")
async def get_config():
    """Get hardware configuration from hardware.yaml."""
    # TODO: Load from actual config/hardware.yaml
    return {
        "pods": [
            {
                "pod_id": "forearm_hub",
                "name": "Forearm Hub (Tier 0 Continuous)",
                "modalities": ["ecg", "ppg", "imu"],
                "tier": 0,
                "sampling_rate": {"ecg": 500, "ppg": 64, "imu": 100},
            },
            {
                "pod_id": "head_pod",
                "name": "Head Pod (Tier 1 High-Density Rest/Sleep)",
                "modalities": ["eeg", "fnirs", "ecg_rest"],
                "tier": 1,
                "sampling_rate": {"eeg": 500, "fnirs": 10, "ecg_rest": 500},
            },
            {
                "pod_id": "in_ear_satellite",
                "name": "In-Ear Satellite (Tier 0 Continuous EEG)",
                "modalities": ["eeg_ear"],
                "tier": 0,
                "sampling_rate": {"eeg_ear": 250},
            },
        ],
        "sync": {
            "tier_budgets": {
                "T0": {"max_residual_drift_ms": 10.0, "sync_interval_s": 60.0},
                "T1": {"max_residual_drift_ms": 1.0, "sync_interval_s": 10.0},
            }
        },
        "power_budget": {
            "hub_battery_mah": 3000,
            "target_lifetime_h": 24,
            "tier_profiles": {
                "T0": {"avg_mw": 5.0, "peak_mw": 10.0},
                "T1": {"avg_mw": 50.0, "peak_mw": 100.0, "max_duration_h": 10.0},
                "T2": {"avg_mw": 100.0, "peak_mw": 200.0, "max_duration_h": 0.5},
            },
        },
    }


# ==================== WebSocket for Real-time Updates ====================

@app.websocket("/ws/live")
async def websocket_live(websocket: WebSocket):
    """WebSocket endpoint for real-time data push."""
    await websocket.accept()
    connected_clients.add(websocket)
    logger.info(f"WebSocket connected. Total clients: {len(connected_clients)}")
    
    try:
        while True:
            # Send periodic updates (in production, push on data arrival)
            await asyncio.sleep(1.0)
            await websocket.send_json({
                "type": "status_update",
                "data": MOCK_POD_STATUS,
            })
    except WebSocketDisconnect:
        connected_clients.discard(websocket)
        logger.info(f"WebSocket disconnected. Total clients: {len(connected_clients)}")


async def broadcast_to_clients(message: dict[str, Any]):
    """Broadcast message to all connected WebSocket clients."""
    disconnected = set()
    for client in connected_clients:
        try:
            await client.send_json(message)
        except Exception:
            disconnected.add(client)
    connected_clients.difference_update(disconnected)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)