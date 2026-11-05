#!/usr/bin/env python3
"""Synthetic band sessions for the consumers of the contract (Averyn's parsers and screens).

There is no band yet. Until there is, a consumer needs input that has the shape of the real thing,
including everything that goes wrong on a radio link, and an answer key. This writes
``contracts/fixtures/band_scenarios_v1.json``:

* the **raw** scenarios: frames built by ``synapse_device.codec.encode_frame`` with the framing of
  the research service (flush sizes, sequence per stream, wrap flag), interleaved the way a phone
  receives them, each with the phone's monotonic reception time;
* the **L1** scenario: feature frames built by ``synapse_device.consumer.encode_feature``;
* for each scenario, ``expected``: what a correct consumer must conclude. It is computed here by a
  small reference implementation of the rules in ``contracts/raw_frame_v1.json`` (the same
  sequence rule as Averyn's ``SynapseSequenceTracker``), and ``tests/test_band_fixtures.py`` checks
  the reference against independent facts (the number of frames removed, the restart that was
  injected), so the answer key is not just the generator agreeing with itself.

The signals are synthetic and say so (``"synthetic": true``): an ECG with one 1 mV R wave per
second, a 1 Hz PPG pulse, gravity on the Z axis. Nothing here is a measurement.

    uv run python scripts/make_band_fixtures.py          # write
    uv run python scripts/make_band_fixtures.py --check  # fail on drift
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk" / "python"))

from synapse_device import codec, consumer  # noqa: E402

OUT = ROOT / "contracts" / "fixtures" / "band_scenarios_v1.json"

#: A step back of up to this many frames is a repeat or a late frame; a longer one is a restart.
OUT_OF_ORDER_WINDOW = 64

#: stream -> (rate Hz, channels, format, samples per frame). Flush sizes are the research
#: service's (ble_research_service.c init_stream_table).
STREAMS: dict[codec.RawStream, tuple[int, int, codec.RawFormat, int]] = {
    codec.RawStream.ECG: (500, 1, codec.RawFormat.S16, 64),
    codec.RawStream.PPG: (50, 2, codec.RawFormat.S32, 16),
    codec.RawStream.ACC: (50, 3, codec.RawFormat.S16, 32),
    codec.RawStream.GYRO: (50, 3, codec.RawFormat.S16, 32),
    codec.RawStream.MAG: (50, 3, codec.RawFormat.S16, 32),
}


# --------------------------------------------------------------------------- synthetic signals


def _signal(stream: codec.RawStream, n: int, t0_s: float) -> np.ndarray:
    """``n`` samples of ``stream`` starting ``t0_s`` seconds into the session, wire integers."""
    rate, channels, _fmt, _flush = STREAMS[stream]
    t = t0_s + np.arange(n) / rate
    rng = np.random.RandomState(int(t0_s * 1000) % 2**31 + int(stream))
    noise = rng.randint(-2, 3, size=(n, channels))
    if stream is codec.RawStream.ECG:
        phase = (t % 1.0) * 1000.0  # ms into the beat; one R wave per second
        r_wave = np.where(phase < 40.0, 1000.0 * (1.0 - np.abs(phase - 20.0) / 20.0), 0.0)
        return (r_wave[:, None] + noise * 10).astype(np.int64)  # microvolts
    if stream is codec.RawStream.PPG:
        pulse = 0.5 * (1.0 - np.cos(2.0 * math.pi * t))  # 60 bpm
        red = 100_000 + 4_000 * pulse
        ir = 120_000 + 6_000 * pulse
        return np.stack([red, ir], axis=1).astype(np.int64) + noise * 3
    if stream is codec.RawStream.ACC:
        base = np.array([10, -20, 1000])  # mg: gravity on Z
        return base[None, :] + noise
    if stream is codec.RawStream.GYRO:
        return noise.astype(np.int64)  # mrad/s around zero
    return np.array([3000, -1200, 4000])[None, :] + noise  # 0.01 uT


# --------------------------------------------------------------------------- framing


def _frames(
    stream: codec.RawStream, seconds: float, *, t0_us: int, first_sequence: int = 0
) -> list[tuple[int, bytes]]:
    """(reception time in ms, frame) for one stream, the way the research service frames it."""
    rate, channels, fmt, flush = STREAMS[stream]
    total = int(seconds * rate)
    out: list[tuple[int, bytes]] = []
    previous_low: int | None = None
    seq = first_sequence
    for start in range(0, total - flush + 1, flush):
        first_us = t0_us + int(round(start * 1_000_000 / rate))
        low = first_us & 0xFFFFFFFF
        # The wrap witness (raw_frame_v1.json): this frame's low timestamp is lower than the
        # previous transmitted frame's of the same stream.
        wrapped = previous_low is not None and low < previous_low
        flags = codec.FLAG_TS_EPOCH_WRAPPED if wrapped else 0
        values = _signal(stream, flush, start / rate).reshape(-1)
        frame = codec.encode_frame(stream, seq & 0xFFFF, low, flags, fmt, values)
        # Notified when the last sample of the frame exists, plus ~20 ms of stack and radio.
        last_us = first_us + int(round((flush - 1) * 1_000_000 / rate))
        out.append(((last_us - t0_us) // 1000 + 20, frame))
        previous_low = low
        seq += 1
    return out


def _session(
    seconds: float, *, t0_us: int = 1_000_000
) -> dict[codec.RawStream, list[tuple[int, bytes]]]:
    return {s: _frames(s, seconds, t0_us=t0_us) for s in STREAMS}


def _interleave(streams: dict[Any, list[tuple[int, bytes]]]) -> list[tuple[int, bytes]]:
    return sorted((f for frames in streams.values() for f in frames), key=lambda f: f[0])


# --------------------------------------------------------------------------- the reference oracle


class _Tracker:
    """The sequence rule of the contract (and of Averyn's SynapseSequenceTracker)."""

    def __init__(self) -> None:
        self.last: int | None = None
        self.lost = 0
        self.out_of_order = 0
        self.restarts = 0

    def on_frame(self, sequence: int) -> str:
        """Returns 'ok', 'lost', 'out_of_order' or 'restart'; the counters are updated."""
        previous, self.last = self.last, sequence
        if previous is None:
            return "ok"
        missed = (sequence - previous - 1) & 0xFFFF
        if missed >= 0x8000:
            if ((previous - sequence) & 0xFFFF) > OUT_OF_ORDER_WINDOW:
                self.restarts += 1
                return "restart"
            self.out_of_order += 1
            self.last = previous
            return "out_of_order"
        self.lost += missed
        return "lost" if missed else "ok"


_REASONS = (
    ("need at least", "too_short"),
    ("zero samples", "zero_samples"),
    ("declares", "truncated"),
    ("frame version", "unknown_version"),
    ("stream id", "unknown_stream"),
    ("sample format", "unknown_format"),
)


def _invalid_reason(error: str) -> str:
    """Why the SDK codec refused a frame, as the contract names it."""
    return next((reason for needle, reason in _REASONS if needle in error), "invalid")


def oracle(frames: list[tuple[int, bytes]]) -> dict[str, Any]:
    """What a correct consumer concludes from ``frames`` (reception time ms, frame bytes)."""
    trackers: dict[str, _Tracker] = {}
    epoch: dict[str, int] = {}
    summary: dict[str, dict[str, Any]] = {}
    invalid: dict[str, int] = {}
    for _recv_ms, raw in frames:
        try:
            header, values = codec.decode_frame(raw)
        except codec.RawCodecError as exc:
            reason = _invalid_reason(str(exc))
            invalid[reason] = invalid.get(reason, 0) + 1
            continue
        name = header.stream.name.lower()
        tracker = trackers.setdefault(name, _Tracker())
        s = summary.setdefault(
            name,
            {
                "frames": 0,
                "values": 0,
                "device_dropped": 0,
                "wraps": 0,
                "timeline_us": [],
                "events": [],
            },
        )
        verdict = tracker.on_frame(header.sequence)
        if verdict == "out_of_order":
            s["events"].append({"sequence": header.sequence, "event": "out_of_order"})
            continue  # a repeat or a late frame carries nothing new for the timeline
        if verdict == "restart":
            epoch[name] = 0  # a rebooted band restarts its clock too
            s["events"].append({"sequence": header.sequence, "event": "restart"})
        elif verdict == "lost":
            s["events"].append({"sequence": header.sequence, "event": "gap"})
        if header.ts_epoch_wrapped:
            epoch[name] = epoch.get(name, 0) + 1
            s["wraps"] += 1
        s["frames"] += 1
        s["values"] += int(values.size)
        s["device_dropped"] += header.dropped
        s["timeline_us"].append((epoch.get(name, 0) << 32) | header.timestamp_us)
    for name, tracker in trackers.items():
        summary[name]["lost_frames"] = tracker.lost
        summary[name]["out_of_order"] = tracker.out_of_order
        summary[name]["restarts"] = tracker.restarts
    return {"streams": summary, "invalid_frames": invalid}


# --------------------------------------------------------------------------- scenarios


def _scenario(name: str, description: str, frames: list[tuple[int, bytes]]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "synthetic": True,
        "frames": [{"recv_ms": ms, "hex": raw.hex()} for ms, raw in frames],
        "expected": oracle(frames),
    }


def raw_scenarios() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    out.append(
        _scenario(
            "clean_20s",
            "20 s of ECG, PPG, ACC, GYRO and MAG, no loss: 20 R waves, 60 bpm.",
            _interleave(_session(20.0)),
        )
    )

    streams = _session(6.0)
    ecg = streams[codec.RawStream.ECG]
    del ecg[4:7]  # three consecutive ECG frames never arrive
    out.append(
        _scenario(
            "ecg_frames_lost",
            "Three consecutive ECG frames (sequences 4, 5, 6) never reach the phone.",
            _interleave(streams),
        )
    )

    # The sequence rule can only tell a restart from a late frame when the counter was past the
    # out-of-order window (64): reboot after 80 frames (10 s of ECG).
    ecg = _frames(codec.RawStream.ECG, 10.3, t0_us=1_000_000)
    reboot = _frames(codec.RawStream.ECG, 2.0, t0_us=500_000)  # uptime and sequence restart
    out.append(
        _scenario(
            "band_restart",
            "The band reboots during the ECG stream after 80 frames: sequence and device clock "
            "start again. A restart is followed, not counted as thousands of lost frames.",
            [*ecg[:80], *[(ecg[79][0] + 200 + ms, raw) for ms, raw in reboot]],
        )
    )

    wrap_t0 = (1 << 32) - 3_000_000  # the 32-bit microsecond counter wraps 3 s in
    out.append(
        _scenario(
            "timestamp_wrap",
            "The device microsecond counter wraps 2**32 three seconds into the ECG and PPG streams.",
            _interleave(
                {
                    s: _frames(s, 6.0, t0_us=wrap_t0)
                    for s in (codec.RawStream.ECG, codec.RawStream.PPG)
                }
            ),
        )
    )

    frames = _frames(codec.RawStream.PPG, 4.0, t0_us=1_000_000)
    lossy: list[tuple[int, bytes]] = []
    for i, (ms, original) in enumerate(frames):
        raw = original
        if i in (2, 5):  # the band reports it dropped samples before these frames
            header, values = codec.decode_frame(original)
            raw = codec.encode_frame(
                header.stream,
                header.sequence,
                header.timestamp_us,
                header.flags,
                header.sample_format,
                values,
                dropped=7 if i == 2 else 300,  # the second saturates the byte at 255
            )
        lossy.append((ms, raw))
    out.append(
        _scenario(
            "device_reported_loss",
            "The band reports samples it dropped (frames 2 and 5 of the PPG stream): 7 and 255.",
            lossy,
        )
    )

    good = _frames(codec.RawStream.ECG, 2.0, t0_us=1_000_000)
    base = bytearray(good[0][1])

    def mutated(index: int, value: int) -> bytes:
        b = bytearray(base)
        b[index] = value
        return bytes(b)

    broken = [
        (1, mutated(0, 2)),  # unknown version
        (2, mutated(1, 0x7F)),  # unknown stream
        (3, mutated(10, 0x09)),  # unknown sample format
        (4, bytes(base[:-1])),  # one byte short
        (5, bytes(base[:8])),  # not even a header
        (6, mutated(9, 0)),  # declares no samples
    ]
    out.append(
        _scenario(
            "invalid_frames",
            "Valid ECG frames mixed with six that must be refused and counted, never interpreted.",
            sorted([*good, *broken], key=lambda f: f[0]),
        )
    )

    frames = _frames(codec.RawStream.ECG, 4.0, t0_us=1_000_000)
    # frame 3 arrives twice (a repeat), and frames 4 and 5 swap places (4 is late)
    arrival = [*frames[:4], frames[3], frames[5], frames[4], *frames[6:]]
    period_ms = 128  # an ECG frame is 64 samples at 500 Hz
    out.append(
        _scenario(
            "repeat_and_late_frame",
            "A repeated frame and a late one: both are counted apart and neither moves the counter "
            "back. The late frame was counted missing when its successor arrived (the rule does not "
            "retract), and neither extra frame adds samples to the timeline.",
            [(frames[0][0] + k * period_ms, raw) for k, (_, raw) in enumerate(arrival)],
        )
    )
    return out


def l1_scenario() -> dict[str, Any]:
    """Two minutes of feature frames at 1 Hz: values, sentinels, a loss and a restart."""
    frames: list[dict[str, Any]] = []
    sequence = 0
    for second in range(120):
        if second in (20, 21):
            sequence += 1  # two frames lost on the link
        if second == 90:
            sequence = 0  # the band rebooted (past the out-of-order window, so it is a restart)
        values: dict[str, int | None] = {
            "flags": 4,  # transmitting
            "sequence": sequence & 0xFFFF,
            "t_dev_ms": second * 1000 if second < 90 else (second - 90) * 1000,
            "heart_rate_bpm": 60 + (second % 21),
            "ppg_sqi": 200 + second % 50,
            "motion_artifact": 10 + second % 20,
            "motion_level": 15,
            "battery_pct": 90,
            "sensor_health": 0x17,
        }
        if second < 10:  # warming up: nothing but the heart rate is known yet
            for key in ("ppg_sqi", "motion_artifact", "motion_level"):
                values[key] = None
        frames.append(
            {
                "recv_ms": second * 1000 + 15,
                "values": values,
                "hex": consumer.encode_feature(values).hex(),
            }
        )
        sequence += 1
    bad = bytearray(bytes.fromhex(frames[30]["hex"]))
    bad[0] = 3  # a version this contract does not define
    frames.insert(31, {"recv_ms": 30_500, "values": None, "hex": bad.hex()})

    tracker = _Tracker()
    unknown = 0
    for f in frames:
        raw = bytes.fromhex(f["hex"])
        if raw[0] != consumer.FEATURE_VERSION:
            unknown += 1
            continue
        tracker.on_frame(int.from_bytes(raw[2:4], "little"))
    return {
        "name": "l1_session_120s",
        "description": "120 s of feature frames: 2 lost at 20 s, a band restart at 90 s, "
        "one frame of an unknown version, a warm-up with sentinels.",
        "synthetic": True,
        "frames": frames,
        "expected": {
            "lost_frames": tracker.lost,
            "restarts": tracker.restarts,
            "out_of_order": tracker.out_of_order,
            "unknown_version_frames": unknown,
            "accepted_frames": len(frames) - unknown,
        },
    }


def build() -> str:
    doc = {
        "description": (
            "Synthetic band sessions with an answer key. GENERATED by scripts/make_band_fixtures.py "
            "from synapse_device.codec / synapse_device.consumer; nothing here is a measurement. "
            "`recv_ms` is the phone's monotonic reception time since the start of the recording."
        ),
        "contract": {
            "raw_frame": "contracts/raw_frame_v1.json",
            "consumer": "contracts/gatt_v2.json",
        },
        "out_of_order_window": OUT_OF_ORDER_WINDOW,
        "raw": raw_scenarios(),
        "l1": l1_scenario(),
    }
    return json.dumps(doc, separators=(",", ":")) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    content = build()
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.is_file() else ""
        if current != content:
            print(f"FIXTURE DRIFT: {OUT} is stale: run scripts/make_band_fixtures.py")
            return 1
        print("BAND FIXTURES OK")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(content, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(content) // 1024} kB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
