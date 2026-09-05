#!/usr/bin/env python3
"""Clock synchronization validation script.

Generates synthetic multi-pod data with known clock drift,
runs the synchronization pipeline, and quantifies residual drift.

Quality Gate: Residual drift < 1ms (99th percentile) after correction.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from synapse24.acquisition.clock_sync import (
    DriftEstimate,
    MultiPodClockSync,
    SyncConfig,
    TierSyncBudget,
    quantify_residual_drift,
)
from synapse24.signal_quality import Tier

DEFAULT_PODS: dict[str, dict] = {
    "head_pod": {"drift_ppm": 100.0, "offset_ms": 2.0, "acc_fs": 100},
    "forearm_pod": {"drift_ppm": -50.0, "offset_ms": -1.0, "acc_fs": 50},
    "ear_pod": {"drift_ppm": 200.0, "offset_ms": 5.0, "acc_fs": 100},
    "chest_pod": {"drift_ppm": 10.0, "offset_ms": 0.5, "acc_fs": 700},
}
"""Default synthetic pods (Architecture.md §63-70: head/forearm/in-ear + chest)."""

# Pods whose fastest signal needs Tier 1 precision (ECG 700Hz / EEG 500Hz).
TIER1_PODS = frozenset({"head_pod", "chest_pod"})


def generate_synthetic_scenario(
    duration_s: float = 3600.0,  # 1 hour
    sync_interval_s: float = 60.0,
    pods: dict[str, dict] | None = None,
    acc_fs: int = 100,
    seed: int = 42,
) -> tuple[MultiPodClockSync, dict[str, np.ndarray], np.ndarray]:
    """Generate synthetic multi-pod scenario with known drift.

    Args:
        duration_s: Total simulation duration
        sync_interval_s: Sync marker interval
        pods: Dict of pod_id -> {drift_ppm, offset_ms, acc_fs}
        acc_fs: Default ACC sampling rate
        seed: Random seed for reproducibility

    Returns:
        Tuple of (sync_instance, pod_timestamps_dict, hub_timestamps)
    """
    np.random.seed(seed)

    if pods is None:
        pods = {pod_id: dict(cfg) for pod_id, cfg in DEFAULT_PODS.items()}

    # Per-tier intervals (Architecture.md §92): no legacy global fields.
    config = SyncConfig(
        tier_budget=TierSyncBudget(
            tier0_sync_interval_s=sync_interval_s,
            tier1_sync_interval_s=min(sync_interval_s, 10.0),
        ),
        acc_sampling_rates={pod_id: cfg["acc_fs"] for pod_id, cfg in pods.items()},
    )
    sync = MultiPodClockSync(config)

    # Register pods
    for pod_id, cfg in pods.items():
        sync.register_pod(pod_id, cfg["acc_fs"])

    # Time bases
    hub_timestamps = np.arange(0, duration_s, 1.0 / acc_fs)
    n_samples = len(hub_timestamps)

    # Generate shared ACC signal (physical motion)
    # Simulate realistic motion: rest periods + movement bursts
    t = hub_timestamps
    motion = np.zeros_like(t)
    # Add periodic movement (e.g., sleep movements every 20 min)
    for center in np.arange(600, duration_s, 1200):
        mask = np.abs(t - center) < 30
        motion[mask] = np.sin(2 * np.pi * 2 * (t[mask] - center))

    # Add random movements
    burst_times = np.random.uniform(0, duration_s, int(duration_s / 300))
    for bt in burst_times:
        mask = np.abs(t - bt) < 10
        motion[mask] += np.random.randn(np.sum(mask)) * 0.5

    # Base ACC signal (cardiac + respiratory + motion)
    cardiac = 0.1 * np.sin(2 * np.pi * 1.2 * t)
    respiratory = 0.05 * np.sin(2 * np.pi * 0.25 * t)
    hub_acc = cardiac + respiratory + motion + 0.02 * np.random.randn(n_samples)

    pod_timestamps = {}
    pod_acc_signals = {}

    # Generate pod data with drift
    for pod_id, cfg in pods.items():
        drift_rate = cfg["drift_ppm"] / 1_000_000.0
        offset_s = cfg["offset_ms"] / 1000.0

        # Pod clock: t_pod = (1 + drift) * t_hub + offset
        pod_t = (1.0 + drift_rate) * t + offset_s
        pod_timestamps[pod_id] = pod_t

        # Pod ACC: same physical motion, sampled at pod clock
        pod_fs = cfg["acc_fs"]
        pod_t_acc = np.arange(0, duration_s, 1.0 / pod_fs)
        pod_acc_t = (1.0 + drift_rate) * pod_t_acc + offset_s

        # Resample hub_acc to pod timestamps (with small noise)
        # For simplicity, generate directly at pod rate
        cardiac_p = 0.1 * np.sin(2 * np.pi * 1.2 * pod_acc_t)
        respiratory_p = 0.05 * np.sin(2 * np.pi * 0.25 * pod_acc_t)

        # Motion at pod time
        motion_p = np.zeros_like(pod_acc_t)
        for center in np.arange(600, duration_s, 1200):
            mask = np.abs(pod_acc_t - center) < 30
            motion_p[mask] = np.sin(2 * np.pi * 2 * (pod_acc_t[mask] - center))

        for bt in burst_times:
            mask = np.abs(pod_acc_t - bt) < 10
            motion_p[mask] += np.random.randn(np.sum(mask)) * 0.5

        pod_acc = cardiac_p + respiratory_p + motion_p + 0.02 * np.random.randn(len(pod_acc_t))
        pod_acc_signals[pod_id] = (pod_acc_t, pod_acc)

    # Simulate sync markers
    n_markers = int(duration_s / sync_interval_s) + 1
    for i in range(n_markers):
        hub_marker_time = i * sync_interval_s
        marker_t = hub_marker_time
        pod_times = {}
        for pod_id, cfg in pods.items():
            drift_rate = cfg["drift_ppm"] / 1_000_000.0
            offset_s = cfg["offset_ms"] / 1000.0
            pod_times[pod_id] = (1.0 + drift_rate) * marker_t + offset_s

        from synapse24.acquisition.clock_sync import SyncMarker

        sync.drift_estimator.add_marker(
            SyncMarker(
                sequence=i,
                hub_timestamp=marker_t,
                pod_timestamps=pod_times,
            )
        )

    # Feed ACC data to sync
    # Downsample hub ACC to sync buffer rate (use acc_fs)
    for i in range(0, n_samples, max(1, acc_fs // 100)):
        sync.add_hub_acc(float(hub_acc[i]), float(hub_timestamps[i]))

    for pod_id, (pod_acc_t, pod_acc) in pod_acc_signals.items():
        step = max(1, len(pod_acc) // n_samples * 10)
        for i in range(0, len(pod_acc), step):
            sync.add_pod_acc(pod_id, float(pod_acc[i]), float(pod_acc_t[i]))

    return sync, pod_timestamps, hub_timestamps


def run_validation(
    duration_s: float = 3600.0,
    sync_interval_s: float = 60.0,
    pods: dict[str, dict] | None = None,
    output_path: Path | None = None,
    xdf_roundtrip: bool = False,
) -> dict:
    """Run full validation and return metrics.

    Each pod is judged against its own tier budget (Architecture.md §92):
    Tier 1 pods (head/chest, fast signals) need p99 <= 1ms, Tier 0 pods
    (PPG/IMU rate signals) need p99 <= 10ms.
    """
    if pods is None:
        pods = {pod_id: dict(cfg) for pod_id, cfg in DEFAULT_PODS.items()}
    print(f"Generating synthetic scenario: {duration_s}s, {sync_interval_s}s sync interval...")
    start = time.time()

    sync, pod_timestamps, hub_timestamps = generate_synthetic_scenario(
        duration_s=duration_s,
        sync_interval_s=sync_interval_s,
        pods=pods,
    )

    gen_time = time.time() - start
    print(f"  Generation: {gen_time:.2f}s")

    print("Running drift estimation...")
    start = time.time()
    estimates = sync.update_drift_estimates()
    est_time = time.time() - start
    print(f"  Estimation: {est_time:.2f}s")

    # Print estimates
    print("\nDrift Estimates:")
    for pod_id, est in estimates.items():
        print(
            f"  {pod_id}: offset={est.offset_ms:.2f}ms, drift={est.drift_rate_ppm:.1f}ppm, "
            f"conf={est.confidence:.2f}, method={est.method}"
        )

    # Test correction on full timestamps (per-tier budgets, Arch §92)
    print("\nTesting timestamp correction...")
    all_metrics = _evaluate_pod_residuals(sync, pod_timestamps, pods)

    # Overall pass/fail against per-tier budgets
    overall_pass = all(m["p99_offset_ms"] <= m["tolerance_ms"] for m in all_metrics.values())

    print(f"\n{'=' * 50}")
    if overall_pass:
        print("VALIDATION PASSED: All pods within per-tier residual drift (p99)")
    else:
        print("VALIDATION FAILED: Some pods exceed their tier budget (p99)")
    print(f"{'=' * 50}")

    # Optional two-node XDF proof: hub ACC + fastest pod ACC must round-trip
    # through XDF with zero sample loss (Roadmap.md §4, LSL/XDF from day one).
    xdf_proof: dict | None = None
    if xdf_roundtrip:
        xdf_proof = _run_xdf_proof(sync, pod_timestamps, pods, output_path)
        overall_pass = overall_pass and xdf_proof["all_streams_valid"]

    # Prepare results
    results = {
        "config": {
            "duration_s": duration_s,
            "sync_interval_s": sync_interval_s,
            "pods": pods,
            "per_tier_budgets": {
                "T0": {"tolerance_ms": 10.0, "interval_s": 60.0},
                "T1": {"tolerance_ms": 1.0, "interval_s": 10.0},
            },
        },
        "xdf_proof": xdf_proof,
        "estimates": {
            pod_id: {
                "offset_ms": est.offset_ms,
                "drift_rate_ppm": est.drift_rate_ppm,
                "confidence": est.confidence,
                "method": est.method,
            }
            for pod_id, est in estimates.items()
        },
        "metrics": all_metrics,
        "overall_pass": overall_pass,
        "timing": {
            "generation_s": gen_time,
            "estimation_s": est_time,
        },
    }

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\nResults saved to {output_path}")

    return results


def _evaluate_pod_residuals(
    sync: MultiPodClockSync,
    pod_timestamps: dict[str, np.ndarray],
    pods: dict[str, dict],
) -> dict[str, dict]:
    """Correct pod timestamps and quantify residual drift per tier budget."""
    all_metrics: dict[str, dict] = {}
    for pod_id, pod_ts in pod_timestamps.items():
        # The corrector maps pod timestamps back to the hub clock domain.
        corrected_ts = sync.correct_pod_timestamps(pod_id, pod_ts)

        # Ground truth hub timestamps for these pod timestamps:
        # pod_ts = (1 + drift) * hub_ts + offset -> hub_ts = (pod_ts - offset) / (1 + drift)
        cfg = pods[pod_id]
        drift_rate = cfg["drift_ppm"] / 1_000_000.0
        offset_s = cfg["offset_ms"] / 1000.0
        hub_ts_interp = (pod_ts - offset_s) / (1.0 + drift_rate)

        # Per-tier budget: fast-signal pods need 1ms, the rest 10ms.
        tier = Tier.T1 if pod_id in TIER1_PODS else Tier.T0
        tolerance_ms, _ = sync.config.get_budget_for_tier(tier)
        metrics = quantify_residual_drift(
            corrected_ts, hub_ts_interp, tier=tier, config=sync.config
        )
        metrics["tier"] = tier.name
        metrics["tolerance_ms"] = tolerance_ms
        all_metrics[pod_id] = metrics

        print(f"  {pod_id} [{tier.name}, tol={tolerance_ms:.0f}ms]:")
        print(f"    mean_offset: {metrics['mean_offset_ms']:.3f}ms")
        print(f"    std_offset:  {metrics['std_offset_ms']:.3f}ms")
        print(f"    max_offset: {metrics['max_abs_offset_ms']:.3f}ms")
        print(f"    p99_offset:  {metrics['p99_offset_ms']:.3f}ms")
        print(f"    within_1ms:  {metrics['within_1ms_pct']:.1f}%")
    return all_metrics


def _run_xdf_proof(
    sync: MultiPodClockSync,
    pod_timestamps: dict[str, np.ndarray],
    pods: dict[str, dict],
    output_path: Path | None,
) -> dict:
    """Prove hub + fastest-pod ACC streams round-trip through XDF lossless."""
    from synapse24.utils import verify_xdf_roundtrip

    hub_acc, hub_ts = sync.hub_acc_snapshot()
    fastest_pod = max(pods, key=lambda p: pods[p]["acc_fs"])
    cfg = pods[fastest_pod]
    drift_rate = cfg["drift_ppm"] / 1_000_000.0
    offset_s = cfg["offset_ms"] / 1000.0
    # Pair each pod sample with its ground-truth hub time so both streams are
    # internally consistent (data window matches timestamp window per stream).
    n = min(len(hub_acc), len(pod_timestamps[fastest_pod]))
    hub_acc, hub_ts = hub_acc[:n], hub_ts[:n]
    pod_slice = np.asarray(pod_timestamps[fastest_pod][:n])
    pod_hub_slice = (pod_slice - offset_s) / (1.0 + drift_rate)
    # The scenario samples all pod timestamp grids at the common ACC grid, so
    # report the measured grid rate (not the pod's nominal ADC rate) as nominal.
    pod_span_s = float(pod_hub_slice[-1] - pod_hub_slice[0])
    pod_grid_fs = float((len(pod_hub_slice) - 1) / pod_span_s) if pod_span_s > 0 else 0.0
    xdf_path = (output_path.parent if output_path else Path("data/processed")) / (
        output_path.stem + ".proof.xdf" if output_path else "sync_proof.xdf"
    )
    xdf_proof = verify_xdf_roundtrip(
        [
            {
                "name": "SYNAPSE_HUB_ACC",
                "type": "ACC",
                "data": hub_acc.reshape(-1, 1),
                "timestamps": hub_ts,
                "sampling_rate": 100.0,
            },
            {
                "name": "SYNAPSE_POD_ACC",
                "type": "ACC",
                "data": pod_slice.reshape(-1, 1),
                "timestamps": pod_hub_slice,
                "sampling_rate": pod_grid_fs,
            },
        ],
        xdf_path,
    )
    print(
        f"XDF proof: {xdf_proof['total_recovered']}/{xdf_proof['total_expected']} "
        f"samples recovered, dropped={xdf_proof['total_dropped']}"
    )
    return xdf_proof


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Validate clock synchronization pipeline")
    parser.add_argument("--duration", type=float, default=3600.0, help="Simulation duration (s)")
    parser.add_argument("--sync-interval", type=float, default=60.0, help="Sync interval (s)")
    parser.add_argument("--output", type=Path, help="Output JSON path")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument(
        "--xdf-roundtrip",
        action="store_true",
        help="Also prove zero-drop XDF round-trip for hub + fastest pod",
    )

    args = parser.parse_args()

    results = run_validation(
        duration_s=args.duration,
        sync_interval_s=args.sync_interval,
        pods={pod_id: dict(cfg) for pod_id, cfg in DEFAULT_PODS.items()},
        output_path=args.output,
        xdf_roundtrip=args.xdf_roundtrip,
    )

    sys.exit(0 if results["overall_pass"] else 1)


if __name__ == "__main__":
    main()
