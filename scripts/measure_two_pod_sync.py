#!/usr/bin/env python3
"""Measure the pod↔pod clock residual (G1 test T10).

Two rigidly coupled bands produce two XDF recordings. Their accelerometers
see the same motion but their LSL clocks are independent, so the residual is
the lag that maximises the cross-correlation of the two motion magnitudes.
This is the number PROCUREMENT.md uses to decide the Cerelog order; a recording
with uncorrelated ACC signals is NOT evidence and returns no residual.

    uv run python scripts/measure_two_pod_sync.py \
        --pods pod_a.xdf pod_b.xdf --out data/live_validation/pod_pair_sync.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pyxdf
import yaml

ROOT = Path(__file__).resolve().parents[1]
ACC_STREAM = "SYNAPSE_ACC_T0"


def _load_acc(path: Path) -> tuple[np.ndarray, np.ndarray, str]:
    """Return (timestamps, |acceleration|, source_id) of one pod's ACC stream."""
    streams, _header = pyxdf.load_xdf(str(path))
    for stream in streams:
        info = stream["info"]
        name = info.get("name", [""])[0]
        if str(name) != ACC_STREAM:
            continue
        source_id = str(info.get("source_id", [""])[0] or "")
        stamps = np.asarray(stream["time_stamps"], dtype=np.float64)
        values = np.asarray(stream["time_series"], dtype=np.float64)
        if values.ndim == 1:
            values = values.reshape(-1, 1)
        if stamps.size < 2 or values.shape[0] != stamps.size:
            raise ValueError(f"{path}: {ACC_STREAM} has no usable samples")
        # Motion magnitude: independent of the board's roll/pitch on the bench.
        magnitude = np.linalg.norm(values[:, :3], axis=1)
        return stamps, magnitude, source_id
    raise ValueError(f"{path}: no {ACC_STREAM} stream (is this a band-core capture?)")


def residual_ms(
    stamps_a: np.ndarray,
    values_a: np.ndarray,
    stamps_b: np.ndarray,
    values_b: np.ndarray,
    *,
    window_s: float = 30.0,
    min_correlation: float = 0.7,
    min_overlap_s: float = 1800.0,
) -> dict[str, Any]:
    """Lag of best ACC agreement, or a refusal when the signals do not correlate.

    Both streams are resampled on a common 1 kHz grid over their overlap, and
    several windows across the run are measured. The reported residual is the
    WORST window: a median would hide a clock drifting out of tolerance at the
    end of the run, which is exactly what the T10 criterion measures.
    """
    start = max(float(stamps_a[0]), float(stamps_b[0]))
    end = min(float(stamps_a[-1]), float(stamps_b[-1]))
    overlap = end - start
    # T10 is a 30-minute claim: a 40-second lab curiosity cannot prove it.
    required_overlap = max(float(min_overlap_s), float(window_s))
    if overlap < required_overlap:
        return {
            "correlated": False,
            "residual_ms": None,
            "reason": (
                f"overlap {overlap:.1f} s is shorter than the {required_overlap:g} s minimum"
            ),
            "overlap_s": round(overlap, 2),
        }

    # Use a few windows across the whole recording, not only the first one:
    # a clock that drifts would otherwise look perfect at the start.
    # Interpolate on a 1 kHz grid so a 7 ms residual is seven samples, not a
    # fraction of one. FFT correlation keeps this affordable for long runs.
    rate = 1000.0
    lags = np.arange(-1000, 1001)  # +/- 1 s, in samples
    per_window: list[dict[str, Any]] = []
    for offset in np.linspace(0.0, max(0.0, overlap - window_s), num=6):
        t0 = start + float(offset)
        grid = np.arange(t0, t0 + window_s, 1.0 / rate)
        a = np.interp(grid, stamps_a, values_a)
        b = np.interp(grid, stamps_b, values_b)
        a = a - np.mean(a)
        b = b - np.mean(b)
        norm = np.linalg.norm(a) * np.linalg.norm(b)
        if norm == 0 or not np.all(np.isfinite(a)) or not np.all(np.isfinite(b)):
            return {"correlated": False, "reason": "a window has no motion or non-finite samples"}
        from scipy.signal import correlate  # noqa: PLC0415 - only for the FFT path

        corr = correlate(a, b, mode="full", method="fft") / norm
        # corr index len-1 is lag 0; the sign says which pod leads, and only
        # the magnitude is the residual the T10 gate cares about.
        segment = corr[len(a) - 1 + lags]
        best = int(np.argmax(segment))
        coefficient = float(segment[best])
        per_window.append(
            {
                "lag_ms": round(float(lags[best]) * 1000.0 / rate, 3),
                "correlation": round(coefficient, 4),
            }
        )

    worst = min(item["correlation"] for item in per_window)
    correlated = worst >= min_correlation
    # The claim is "<= 10 ms over 30 min": a clock that drifts past the limit
    # in the last window must fail even if the other five windows are fine.
    residual = round(max(abs(item["lag_ms"]) for item in per_window), 3)
    return {
        "correlated": correlated,
        "correlation_min": round(worst, 4),
        "residual_ms": residual if correlated else None,
        "overlap_s": round(overlap, 2),
        "min_overlap_s": round(required_overlap, 2),
        "window_s": window_s,
        "method": "ACC magnitude cross-correlation, 6 windows across the overlap",
        "per_window": per_window,
        "reason": None if correlated else f"min correlation {worst:.3f} < {min_correlation}",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pods", nargs=2, required=True, metavar=("XDF_A", "XDF_B"))
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "hardware.yaml")
    parser.add_argument("--threshold-ms", type=float, default=None, help="T0 sync budget in ms")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        manifest = yaml.safe_load(args.config.read_text(encoding="utf-8"))
        sync_cfg = manifest["sync"]
        threshold_ms = float(
            args.threshold_ms
            if args.threshold_ms is not None
            else sync_cfg["tier_budgets"]["T0"]["max_residual_drift_ms"]
        )
        stamps_a, values_a, source_a = _load_acc(Path(args.pods[0]))
        stamps_b, values_b, source_b = _load_acc(Path(args.pods[1]))
        # Two recordings from the same band are not a pod-to-pod measurement:
        # refuse when both XDFs name the same source (tolerant when absent).
        if source_a and source_b and source_a == source_b:
            raise ValueError(  # noqa: TRY301 - a duplicate source is a hard refusal
                f"both XDFs declare the same source_id {source_a!r}: not two different pods"
            )
        result = residual_ms(
            stamps_a,
            values_a,
            stamps_b,
            values_b,
            window_s=float(sync_cfg["acc_correlation_window_s"]),
            min_correlation=float(sync_cfg["min_acc_correlation"]),
        )
        result.update(
            {
                "pods": [str(Path(p).name) for p in args.pods],
                "source_ids": [source_a, source_b],
                "threshold_ms": threshold_ms,
                "pass": bool(
                    result["correlated"]
                    and result.get("residual_ms") is not None
                    and float(result["residual_ms"]) <= threshold_ms
                ),
            }
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["pass"] else 1
    except (ValueError, KeyError, OSError) as exc:
        print(f"two-pod sync measurement failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
