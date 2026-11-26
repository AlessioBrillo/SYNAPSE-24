#!/usr/bin/env python3
"""Compare PPG quality at several LED currents and pick the best pair.

The live path writes each pair into the band's calibration record over USB,
records a short validation run, and analyses the red channel of the resulting
XDF. ``--analyse-only`` skips the hardware and analyses captures you already
have, which is how the behaviour is tested in CI.

    uv run python scripts/ppg_sweep.py --port COM3 --pairs 1A,1A 1F,1F 24,24 \
        --duration 30 --out data/calibration/ppg_sweep.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from synapse24.export import load_xdf_streams
from synapse24.signal_quality.ppg import compute_ppg_sqi

PPG_STREAM = "SYNAPSE_PPG_T0"
DEFAULT_PAIRS = ("1A,1A", "1F,1F", "24,24")


def analyse_capture(path: Path, *, window_s: float = 30.0) -> dict[str, Any]:
    """SQI and a simple perfusion index from the red channel of an XDF."""
    streams = {s.name: s for s in load_xdf_streams(path)}
    if PPG_STREAM not in streams:
        raise ValueError(f"{path}: no {PPG_STREAM} stream")
    stream = streams[PPG_STREAM]
    data = np.asarray(stream.data, dtype=np.float64)
    stamps = np.asarray(stream.timestamps, dtype=np.float64)
    if data.ndim != 2 or data.shape[1] < 1 or data.shape[0] < 50:
        raise ValueError(f"{path}: unusable PPG data {data.shape}")
    if stamps.size > 2:
        data = data[stamps >= stamps[-1] - window_s]
    red = data[:, 0]
    rate = int(round(float(stream.nominal_srate or 50) or 50))
    mean = float(np.mean(red))
    if mean <= 0:
        raise ValueError(f"{path}: red channel mean is not positive")
    return {
        "samples": int(red.size),
        "sqi": float(compute_ppg_sqi(red, rate)),
        "perfusion_index": float(np.std(red) / mean),
    }


def run_sweep(
    pairs: list[str],
    *,
    apply_led: Callable[[str], None],
    capture: Callable[[str], Path],
    analyse: Callable[[Path], dict[str, Any]],
    write: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """Pure orchestration: write LEDs, record, analyse, choose, persist."""
    rows: list[dict[str, Any]] = []
    for pair in pairs:
        apply_led(pair)
        path = capture(pair)
        result = analyse(path)
        result["led_pair"] = pair
        rows.append(result)
    if not rows:
        raise ValueError("no LED pairs given")
    best = max(rows, key=lambda row: (row["sqi"], row["perfusion_index"]))
    report = {"pairs": rows, "chosen": best["led_pair"], "source": "ppg_sweep"}
    write(report)
    return report


def _live_capture(duration_s: int, config: Path) -> Callable[[str], Path]:
    out_dir = Path("data/processed/live_validation")
    gate_report = Path("data/live_validation/ppg_sweep_gate.json")

    def capture(_pair: str) -> Path:
        before = set(out_dir.glob("synapse_live_*.xdf"))
        command = [
            sys.executable,
            str(Path(__file__).with_name("validate_phase1_entry.py")),
            "--live-hardware",
            "--config",
            str(config),
            "--duration",
            str(duration_s),
            "--gate-report",
            str(gate_report),
        ]
        proc = subprocess.run(command, check=False)  # noqa: S603
        if proc.returncode not in (0, 1):  # 1 = a gate failed, but the XDF still exists
            raise RuntimeError(f"validator exited {proc.returncode}")
        after = set(out_dir.glob("synapse_live_*.xdf"))
        new = after - before
        if not new:
            raise RuntimeError("validator produced no XDF")
        return max(new, key=lambda p: p.stat().st_mtime)

    return capture


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="COM3")
    parser.add_argument("--pairs", nargs="+", default=list(DEFAULT_PAIRS))
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--config", type=Path, default=Path("config/hardware_bringup.yaml"))
    parser.add_argument("--out", type=Path, default=Path("data/calibration/ppg_sweep.json"))
    parser.add_argument(
        "--analyse-only",
        nargs="+",
        metavar="PAIR=XDF",
        help="analyse existing captures as PAIR=path, no hardware needed",
    )
    args = parser.parse_args()

    try:
        if args.analyse_only:
            pairs: list[str] = []
            captures: dict[str, Path] = {}
            for item in args.analyse_only:
                pair, path = item.split("=", 1)
                pairs.append(pair)
                captures[pair] = Path(path)
            apply_led = lambda _pair: None  # noqa: E731
            capture = lambda pair: captures[pair]  # noqa: E731
        else:
            from factory_calibrate import SerialTransport

            transport = SerialTransport(args.port)

            def apply_led(pair: str) -> None:
                red, ir = (part.strip() for part in pair.split(","))
                for command in (f"cal led {red} {ir}", "cal save"):
                    reply = transport.send(command)
                    if reply.startswith("ERR"):
                        raise RuntimeError(f"{command}: {reply}")  # noqa: TRY301

            capture = _live_capture(args.duration, args.config)

        report = run_sweep(
            pairs,
            apply_led=apply_led,
            capture=capture,
            analyse=analyse_capture,
            write=lambda payload: (
                args.out.parent.mkdir(parents=True, exist_ok=True),
                args.out.write_text(json.dumps(payload, indent=2, sort_keys=True)),
            ),
        )
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"PPG sweep failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
