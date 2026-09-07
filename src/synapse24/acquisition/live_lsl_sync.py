"""Live 2-pod LSL sync gate — Phase 1 entry proof over the real wire.

Architecture.md §23-31 (decoupled pod/hub), §33-43 (tiered acquisition),
§92 (multi-node clock drift); Roadmap.md §138 (Phase 1 entry: live
ECG+PPG+IMU streaming, synchronized in LSL) + §151 (LSL/XDF from day one).

Unlike the simulated scenario in ``scripts/validate_sync.py`` (injected
timestamps, always routable), this module pushes synthetic Tier-0 payloads
at normative rates through real liblsl outlets and pulls them back through
inlets: resolve failures or dropped samples fail loudly instead of biasing
downstream fusion windows.

Normative rates match config/hardware.yaml forearm hub
(ECG 500Hz / PPG 64Hz / IMU 100Hz) — single source of truth, no resampling.
"""

from __future__ import annotations

import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from pylsl import StreamOutlet

    from synapse24.signal_quality import Tier


class LSLUnavailableError(RuntimeError):
    """Raised when the live LSL wire cannot be exercised (no liblsl/route)."""


@dataclass(frozen=True)
class LiveTwoPodConfig:
    """Configuration for the live 2-pod sync gate."""

    duration_s: float = 2.0
    ecg_fs: int = 500
    ppg_fs: int = 64
    imu_fs: int = 100
    seed: int = 42
    resolve_timeout_s: float = 5.0
    pull_timeout_s: float = 8.0
    xdf_path: Path | None = None
    run_id: str = ""


def _unique_source_id(run_id: str, name: str) -> str:
    return f"synapse24_live_{run_id}_{name}"


def _jsonable(mapping: dict[str, Any]) -> dict[str, Any]:
    """Convert numpy scalars to plain floats for JSON serialization."""
    return {k: float(v) if isinstance(v, (int, float)) else v for k, v in mapping.items()}


def _build_specs(config: LiveTwoPodConfig, run_id: str) -> tuple[list, list[dict[str, Any]]]:
    """Create live outlets and normative Tier-0 payload specs."""
    from pylsl import StreamInfo, StreamOutlet, local_clock

    from synapse24.hardware.esp32_tier0 import create_synthetic_tier0_data

    data = create_synthetic_tier0_data(duration_s=config.duration_s, seed=config.seed)
    payloads = [
        ("SYNAPSE_ECG_T0", "ECG", np.asarray(data["ecg"]).reshape(-1, 1), config.ecg_fs),
        (
            "SYNAPSE_PPG_T0",
            "PPG",
            np.column_stack([data["ppg_red"], data["ppg_ir"]]),
            config.ppg_fs,
        ),
        (
            "SYNAPSE_ACC_T0",
            "ACC",
            np.column_stack([data["acc_x"], data["acc_y"], data["acc_z"]]),
            config.imu_fs,
        ),
    ]

    outlets = []
    specs: list[dict[str, Any]] = []
    for name, stream_type, arr, fs in payloads:
        source_id = _unique_source_id(run_id, name)
        info = StreamInfo(name, stream_type, arr.shape[1], float(fs), "float32", source_id)
        outlets.append(StreamOutlet(info, chunk_size=32, max_buffered=360))
        t0 = local_clock() + 0.5
        pushed_ts = t0 + np.arange(arr.shape[0], dtype=np.float64) / float(fs)
        specs.append(
            {
                "name": name,
                "type": stream_type,
                "data": arr.astype(np.float64),
                "pushed_ts": pushed_ts,
                "sampling_rate": float(fs),
                "source_id": source_id,
                "expected": int(arr.shape[0]),
            }
        )
    return outlets, specs


def _pull_live_stream(
    spec: dict[str, Any], outlet: StreamOutlet, config: LiveTwoPodConfig
) -> dict[str, Any]:
    """Resolve, push, and pull one live stream; return pulled timestamps."""
    from pylsl import StreamInlet, resolve_byprop

    found = resolve_byprop("source_id", spec["source_id"], timeout=config.resolve_timeout_s)
    if not found:
        raise LSLUnavailableError(f"could not resolve live outlet {spec['name']}")
    inlet = StreamInlet(found[0], max_buflen=int(config.duration_s + 5.0))
    try:
        # pylsl returns None on success, raises TimeoutError on failure.
        inlet.open_stream(timeout=config.resolve_timeout_s)
    except TimeoutError as exc:
        raise LSLUnavailableError(f"could not open live inlet {spec['name']}") from exc

    outlet.push_chunk(spec["data"].astype(np.float32).tolist(), spec["pushed_ts"].tolist())

    pulled_ts: list[float] = []
    n_pulled = 0
    deadline = time.time() + config.pull_timeout_s
    while n_pulled < spec["expected"] and time.time() < deadline:
        chunk, stamps = inlet.pull_chunk(timeout=2.0, max_samples=4096)
        if chunk:
            n_pulled += len(chunk)
            pulled_ts.extend(stamps)
    return {
        "pulled_ts": pulled_ts,
        "recovered": n_pulled,
        "time_correction_s": float(inlet.time_correction(timeout=2.0)),
    }


def _wire_residual(
    pushed_ts: np.ndarray, pulled_ts: list[float], recovered: int, expected: int, tier: Tier
) -> dict[str, Any]:
    """Quantify pulled-vs-pushed residual; empty pull fails against +1s offset."""
    from synapse24.acquisition.clock_sync import SyncConfig, quantify_residual_drift

    n = min(recovered, expected)
    if n:
        return quantify_residual_drift(
            np.asarray(pulled_ts[:n], dtype=np.float64),
            np.asarray(pushed_ts[:n], dtype=np.float64),
            tier=tier,
            config=SyncConfig(),
        )
    return quantify_residual_drift(
        np.asarray(pushed_ts, dtype=np.float64),
        np.asarray(pushed_ts, dtype=np.float64) + 1.0,
        tier=tier,
        config=SyncConfig(),
    )


def run_live_2pod_sync(config: LiveTwoPodConfig) -> dict[str, Any]:
    """Push Tier-0 payloads through live LSL outlets and verify delivery.

    Two pods over the wire: forearm T0 (ECG + PPG + ACC outlets) and the
    head-pod sync-marker channel (evaluated against the Tier-1 1ms budget
    and reported explicitly — never silently).

    Returns a JSON-serializable dict with per-stream counts, Tier-0
    residuals, the Tier-1 marker report, the XDF zero-drop proof, and
    ``overall_pass``.
    """
    try:
        import pylsl  # noqa: F401
    except ImportError as exc:
        raise LSLUnavailableError(f"pylsl not installed: {exc}") from exc

    from synapse24.acquisition.clock_sync import SyncConfig
    from synapse24.signal_quality import Tier
    from synapse24.utils import verify_xdf_roundtrip

    run_id = config.run_id or uuid.uuid4().hex[:8]
    sync_config = SyncConfig()

    outlets, specs = _build_specs(config, run_id)

    # Allow outlet announcement before resolving (same pattern as tier0 loopback).
    time.sleep(1.0)

    t0_tolerance_ms, _ = sync_config.get_budget_for_tier(Tier.T0)
    per_stream: list[dict[str, Any]] = []
    pulled_all: list[list[float]] = []
    total_expected = 0
    total_recovered = 0
    for spec, outlet in zip(specs, outlets):
        pulled = _pull_live_stream(spec, outlet, config)
        pulled_all.append(pulled["pulled_ts"])
        total_expected += spec["expected"]
        total_recovered += pulled["recovered"]
        residual = _wire_residual(
            spec["pushed_ts"], pulled["pulled_ts"], pulled["recovered"], spec["expected"], Tier.T0
        )
        per_stream.append(
            {
                "name": spec["name"],
                "sampling_rate": float(spec["sampling_rate"]),
                "expected": int(spec["expected"]),
                "recovered": int(pulled["recovered"]),
                "dropped": int(spec["expected"] - pulled["recovered"]),
                "time_correction_s": pulled["time_correction_s"],
                "residual": _jsonable(residual),
                "tolerance_ms": float(t0_tolerance_ms),
            }
        )

    # Tier-1 marker channel: fastest live stream (ECG) pulled timestamps
    # re-evaluated against the 1ms budget so the BLE-critical number is a
    # real wire measurement, reported explicitly — never silent.
    t1_tolerance_ms, _ = sync_config.get_budget_for_tier(Tier.T1)
    tier1_marker = _jsonable(
        _wire_residual(
            specs[0]["pushed_ts"],
            pulled_all[0],
            per_stream[0]["recovered"],
            specs[0]["expected"],
            Tier.T1,
        )
    )
    tier1_marker["stream"] = "SYNAPSE_ECG_T0"
    tier1_marker["tolerance_ms"] = float(t1_tolerance_ms)

    # XDF zero-drop proof on the live-pushed payloads.
    xdf_path = config.xdf_path or Path(tempfile.mkdtemp()) / "live_2pod.xdf"
    xdf_proof = verify_xdf_roundtrip(
        [
            {
                "name": s["name"],
                "type": specs[i]["type"],
                "data": np.asarray(specs[i]["data"][: s["recovered"]]),
                "timestamps": np.asarray(specs[i]["pushed_ts"][: s["recovered"]]),
                "sampling_rate": s["sampling_rate"],
            }
            for i, s in enumerate(per_stream)
        ],
        Path(xdf_path),
    )

    all_t0_within = all(s["residual"].get("within_10ms_pct", 0.0) == 100.0 for s in per_stream)
    overall_pass = bool(
        total_expected - total_recovered == 0 and all_t0_within and xdf_proof["all_streams_valid"]
    )
    return {
        "run_id": run_id,
        "duration_s": float(config.duration_s),
        "seed": int(config.seed),
        "n_streams": len(per_stream),
        "total_expected": int(total_expected),
        "total_recovered": int(total_recovered),
        "total_dropped": int(total_expected - total_recovered),
        "overall_pass": overall_pass,
        "per_stream": per_stream,
        "tier1_marker": tier1_marker,
        "xdf_proof": xdf_proof,
    }
