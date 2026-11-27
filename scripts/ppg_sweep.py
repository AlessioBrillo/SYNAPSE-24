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
import yaml

from synapse24.export import load_xdf_streams
from synapse24.signal_quality.ppg import compute_ppg_sqi

PPG_STREAM = "SYNAPSE_PPG_T0"
DEFAULT_PAIRS = ("1A,1A", "1F,1F", "24,24")
DEFAULT_MIN_SQI = 0.3
DEFAULT_MIN_SAMPLES = 1500  # 30 s at the declared 50 Hz
DEFAULT_MIN_DURATION_S = 30.0


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
    if stamps.size > 2 and stamps.size == data.shape[0]:
        keep = stamps >= stamps[-1] - window_s
        data, stamps = data[keep], stamps[keep]
    red = data[:, 0]
    rate = int(round(float(stream.nominal_srate or 50) or 50))
    mean = float(np.mean(red))
    if mean <= 0:
        raise ValueError(f"{path}: red channel mean is not positive")
    return {
        "samples": int(red.size),
        # The last timestamp minus the first is one sample period short of a
        # 30 s window at 50 Hz; the gate is about how many samples were
        # acquired, so derive the duration from the sample count and rate.
        "duration_s": float(red.size) / float(rate) if rate > 0 else 0.0,
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
    min_sqi: float = DEFAULT_MIN_SQI,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_duration_s: float = DEFAULT_MIN_DURATION_S,
) -> dict[str, Any]:
    """Pure orchestration: write LEDs, record, analyse, choose, persist.

    A max() over noisy rows always picks *something*, even when every capture
    is useless. Only rows that clear the SQI and length floors are eligible;
    with none eligible the report says so and the verdict is FAIL.
    """
    rows: list[dict[str, Any]] = []
    for pair in pairs:
        apply_led(pair)
        path = capture(pair)
        result = analyse(path)
        result["led_pair"] = pair
        result["qualifies"] = (
            float(result.get("sqi", 0.0) or 0.0) >= min_sqi
            and int(result.get("samples", 0) or 0) >= min_samples
            and float(result.get("duration_s", 0.0) or 0.0) >= min_duration_s
        )
        rows.append(result)
    if not rows:
        raise ValueError("no LED pairs given")
    eligible = [row for row in rows if row["qualifies"]]
    report: dict[str, Any] = {
        "pairs": rows,
        "source": "ppg_sweep",
        "min_sqi": min_sqi,
        "min_samples": min_samples,
        "min_duration_s": min_duration_s,
    }
    if eligible:
        best = max(eligible, key=lambda row: (row["sqi"], row["perfusion_index"]))
        report.update({"chosen": best["led_pair"], "verdict": "PASS", "reason": None})
    else:
        report.update(
            {
                "chosen": None,
                "verdict": "FAIL",
                "reason": f"no capture reached SQI >= {min_sqi:g} and {min_duration_s:g} s",
            }
        )
    write(report)
    return report


def _xdf_dirs(config: Path) -> list[Path]:
    """Both places the validator writes XDFs: human sessions and bench runs."""
    payload = yaml.safe_load(config.read_text(encoding="utf-8"))
    validation = payload.get("bringup", payload).get("validation", {})
    dirs = [validation.get("xdf_output_dir"), validation.get("bench_xdf_output_dir")]
    result = [Path(item) for item in dirs if item]
    for fallback in ("data/processed/live_validation", "data/bench"):
        if Path(fallback) not in result:
            result.append(Path(fallback))
    return result


def _live_capture(
    duration_s: int,
    config: Path,
    manifest: Path,
    verified_by: str | None = None,
) -> Callable[[str], Path]:
    out_dirs = _xdf_dirs(config)
    gate_report = Path("data/live_validation/ppg_sweep_gate.json")

    def capture(_pair: str) -> Path:
        before = {directory: set(directory.glob("*.xdf")) for directory in out_dirs}
        command = [
            sys.executable,
            str(Path(__file__).with_name("validate_phase1_entry.py")),
            "--live-hardware",
            "--config",
            str(config),
            "--duration",
            str(duration_s),
            "--manifest",
            str(manifest),
            "--gate-report",
            str(gate_report),
        ]
        if verified_by:
            command += ["--verified-by", str(verified_by)]
        proc = subprocess.run(command, check=False)  # noqa: S603
        # A failed gate is not evidence: the capture must come from a run that
        # passed, otherwise we would choose an LED pair from a broken session.
        if proc.returncode != 0:
            raise RuntimeError(f"validator exited {proc.returncode}")
        try:
            report = json.loads(gate_report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = {}
        artifact = report.get("artifact") or {}
        recorded = str(artifact.get("path") or "") if isinstance(artifact, dict) else ""
        if recorded and Path(recorded).is_file():
            return Path(recorded)
        new = [
            path
            for directory in out_dirs
            for path in directory.glob("*.xdf")
            if path not in before.get(directory, set())
        ]
        if not new:
            raise RuntimeError("validator produced no XDF (check artifact path and output dirs)")
        return max(new, key=lambda path: path.stat().st_mtime)

    return capture


def _reboot_band(transport: Any) -> None:
    """Apply a newly saved LED current: NVS is read by the drivers at boot."""
    reboot = getattr(transport, "reboot", None)
    if not callable(reboot):
        raise RuntimeError(  # noqa: TRY004 - a CLI refusal, not a type error
            "this serial transport has no reboot method: power-cycle the band "
            "after 'cal led' / 'cal save' before recording the next pair"
        )
    reboot()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="COM3")
    parser.add_argument("--pairs", nargs="+", default=list(DEFAULT_PAIRS))
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--config", type=Path, default=Path("config/hardware_bringup.yaml"))
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("data/bench/manifest.json"),
        help="bench session manifest required by the live validator",
    )
    parser.add_argument("--verified-by", help="maintainer who verified the hand-filled rows")
    parser.add_argument("--min-sqi", type=float, default=DEFAULT_MIN_SQI)
    parser.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    parser.add_argument("--min-duration", type=float, default=DEFAULT_MIN_DURATION_S)
    parser.add_argument(
        "--reboot-between",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="reboot the band after each LED write (default: on)",
    )
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
                if args.reboot_between:
                    _reboot_band(transport)

            capture = _live_capture(args.duration, args.config, args.manifest, args.verified_by)

        report = run_sweep(
            pairs,
            apply_led=apply_led,
            capture=capture,
            analyse=analyse_capture,
            write=lambda payload: (
                args.out.parent.mkdir(parents=True, exist_ok=True),
                args.out.write_text(json.dumps(payload, indent=2, sort_keys=True)),
            ),
            min_sqi=args.min_sqi,
            min_samples=args.min_samples,
            min_duration_s=args.min_duration,
        )
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"PPG sweep failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("verdict") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
