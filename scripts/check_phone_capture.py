#!/usr/bin/env python3
"""Check what a phone recorded from a band against what the band says it sent.

The virtual band (``scripts/virtual_band.py --log band.jsonl``) logs every notification it
delivers. The phone (Averyn) stores what it received as two sidecars (ADR-0030, record =
``u16 length | i64 t_rx_ns | frame``). If the radio and the app both work, each sidecar is a
**contiguous slice** of what the band delivered, byte for byte. This script checks that, plus the
GATT conversation the band logged (set_time on every connection, MTU, notification order, consent).

    # copy the newest activity's sidecars from a USB-debugging Android phone
    uv run python scripts/check_phone_capture.py pull --out capture/
    # check them
    uv run python scripts/check_phone_capture.py check --band-log band.jsonl
        --features capture/x.synapse.bin --raw capture/x.synapse-raw.bin

Exit code 1 if anything is an error; notes are observations (what the phone did) and never fail.
"""

from __future__ import annotations

import argparse
import json
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk" / "python"))

from synapse_device import consumer  # noqa: E402

APP_ID = "dev.averyn.app"
ACTIVITIES = "files/activities"
RECORD = struct.Struct("<Hq")
RAW_NAMES = ("ecg", "ppg", "acc", "gyro", "mag", "power")
STREAM_IDS = {1: "ecg", 2: "ppg", 3: "acc", 4: "gyro", 5: "mag", 7: "power"}
#: Fields a missing consent turns into "not available" on the phone (same rule as the server).
CONSENT_MASKED = ("rr_last", "rr_count", "rmssd_ms", "sdnn_ms", "stress_level")
MIN_FEATURE_MTU = 30  # 27 bytes + the 3-byte ATT header


@dataclass(frozen=True)
class Finding:
    """One line of the report: an ``error`` fails the check, a ``note`` only informs."""

    level: str
    text: str


def error(text: str) -> Finding:
    return Finding("error", text)


def note(text: str) -> Finding:
    return Finding("note", text)


# --------------------------------------------------------------------------- reading


def read_sidecar(blob: bytes) -> tuple[list[tuple[int, bytes]], list[Finding]]:
    """``(t_rx_ns, frame)`` records of a sidecar, and what is wrong with the file itself."""
    records: list[tuple[int, bytes]] = []
    findings: list[Finding] = []
    pos = 0
    while pos < len(blob):
        if pos + RECORD.size > len(blob):
            findings.append(error(f"truncated record header at byte {pos}"))
            break
        length, t_ns = RECORD.unpack_from(blob, pos)
        pos += RECORD.size
        if pos + length > len(blob):
            findings.append(error(f"truncated record at byte {pos - RECORD.size}"))
            break
        records.append((t_ns, blob[pos : pos + length]))
        pos += length
    times = [t for t, _ in records]
    if any(b < a for a, b in zip(times, times[1:], strict=False)):
        findings.append(
            error("t_rx_ns goes backwards: the phone's monotonic clock is not monotonic")
        )
    return records, findings


def read_band_log(text: str) -> list[dict]:
    """The band's JSONL log."""
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# --------------------------------------------------------------------------- comparison


def mask_consent_fields(frame: bytes) -> bytes:
    """A feature frame with the consent-gated fields set to "not available"."""
    if len(frame) != consumer.FEATURE_BYTES or frame[0] != consumer.FEATURE_VERSION:
        return frame
    out = bytearray(frame)
    for field in consumer.FEATURE_FIELDS:
        if field.name in CONSENT_MASKED and field.na is not None:
            size = struct.calcsize("<" + field.fmt)
            out[field.offset : field.offset + size] = field.na.to_bytes(size, "little")
    return bytes(out)


def find_slice(delivered: list[bytes], phone: list[bytes]) -> int | None:
    """Index where ``phone`` appears in ``delivered`` as a contiguous run, or None."""
    n = len(phone)
    for i in range(len(delivered) - n + 1):
        if delivered[i : i + n] == phone:
            return i
    return None


def compare_slice(name: str, delivered: list[bytes], phone: list[bytes]) -> list[Finding]:
    """The phone's frames must be a contiguous run of what the band delivered."""
    if not phone:
        return [error(f"{name}: the phone recorded nothing ({len(delivered)} frames delivered)")]
    at = find_slice(delivered, phone)
    masked = False
    if at is None:  # a missing consent legitimately blanks some feature fields on the phone
        masked_delivered = [mask_consent_fields(f) for f in delivered]
        at = find_slice(masked_delivered, [mask_consent_fields(f) for f in phone])
        masked = at is not None
    if at is None:
        return [
            error(
                f"{name}: the phone's {len(phone)} frames are not a slice of the band's {len(delivered)}"
            )
        ]
    out = [note(f"{name}: {len(phone)} of {len(delivered)} frames, from #{at} (byte for byte)")]
    if masked:
        out.append(note(f"{name}: differs only in {', '.join(CONSENT_MASKED)} (consent masking)"))
    if at:
        out.append(note(f"{name}: the phone missed the first {at} frames (recording started late)"))
    tail = len(delivered) - at - len(phone)
    if tail:
        out.append(note(f"{name}: the phone stopped {tail} frames before the end"))
    return out


def delivered_frames(log: list[dict]) -> tuple[list[bytes], dict[str, list[bytes]]]:
    """From the band log: the feature/status frames in order, and the raw frames per stream."""
    l1: list[bytes] = []
    raw: dict[str, list[bytes]] = {n: [] for n in RAW_NAMES}
    for rec in log:
        if rec.get("event") != "notify":
            continue
        frame = bytes.fromhex(rec["hex"])
        if rec["char"] in ("feature", "status"):
            l1.append(frame)
        elif rec["char"] in raw:
            raw[rec["char"]].append(frame)
    return l1, raw


def check_sidecars(
    log: list[dict],
    features: bytes | None,
    raw: bytes | None,
    *,
    signal_quality: bool = True,
    raw_signals: bool = True,
) -> list[Finding]:
    """Both sidecars against the band's delivered frames, honouring the consents."""
    findings: list[Finding] = []
    d_l1, d_raw = delivered_frames(log)
    if features is None or not signal_quality:
        if features:
            findings.append(error("features sidecar written without the signal_quality consent"))
    else:
        records, bad = read_sidecar(features)
        findings += bad + compare_slice("features", d_l1, [f for _, f in records])
    if raw is None or not raw_signals:
        if raw:
            findings.append(error("raw sidecar written without the raw_signals consent"))
    else:
        records, bad = read_sidecar(raw)
        findings += bad
        per_stream: dict[str, list[bytes]] = {n: [] for n in RAW_NAMES}
        unknown = 0
        for _, frame in records:
            name = STREAM_IDS.get(frame[1])
            if name is None:
                unknown += 1
            else:
                per_stream[name].append(frame)
        for name in RAW_NAMES:
            if d_raw[name] or per_stream[name]:
                findings += compare_slice(f"raw/{name}", d_raw[name], per_stream[name])
        if unknown:
            findings.append(error(f"raw sidecar holds {unknown} frame(s) of an unknown stream"))
    return findings


# --------------------------------------------------------------------------- the GATT conversation


def check_conversation(log: list[dict], *, raw_signals: bool = True) -> list[Finding]:
    """What the phone did on the radio, from the band's own log."""
    findings: list[Finding] = []
    connections: list[list[dict]] = []
    for rec in log:
        if rec["event"] == "connect":
            connections.append([])
        elif connections:
            connections[-1].append(rec)
    if not connections:
        return [error("the phone never connected")]
    findings.append(note(f"{len(connections)} connection(s)"))
    for n, conn in enumerate(connections, 1):
        controls = [r for r in conn if r["event"] == "control"]
        set_times = [r for r in controls if r.get("op") == "set_time" or _is_set_time(r)]
        if not set_times:
            findings.append(error(f"connection {n}: no set_time (the band has no other clock)"))
        elif all(r["error"] for r in set_times):
            findings.append(note(f"connection {n}: every set_time was refused; the phone gave up"))
        elif len(set_times) > 1:
            findings.append(note(f"connection {n}: set_time retried {len(set_times) - 1} time(s)"))
        subs = [r["char"] for r in conn if r["event"] == "subscribe" and r["on"]]
        findings.append(note(f"connection {n}: subscribed in order {subs}"))
        if "feature" not in subs or "status" not in subs:
            findings.append(error(f"connection {n}: feature and status were not both subscribed"))
        if not raw_signals and any(s in RAW_NAMES for s in subs):
            findings.append(
                error(f"connection {n}: subscribed to raw without the raw_signals consent")
            )
        if raw_signals and not any(s in RAW_NAMES for s in subs):
            findings.append(error(f"connection {n}: raw consented but no raw stream subscribed"))
        if raw_signals and any(r.get("op") == "stop" and not r["error"] for r in controls):
            findings.append(note(f"connection {n}: the phone stopped the raw streams"))
    mtus = [m for r in log if r["event"] == "mtu" for m in r["mtu"]]
    if not mtus:
        findings.append(error("no MTU was observed"))
    elif min(mtus) < MIN_FEATURE_MTU:
        findings.append(
            error(f"MTU {min(mtus)} cannot carry a 27-byte feature frame (needs {MIN_FEATURE_MTU})")
        )
    else:
        findings.append(note(f"MTU {min(mtus)}..{max(mtus)} (the phone asks for 247)"))
    return findings


def _is_set_time(rec: dict) -> bool:
    raw = bytes.fromhex(rec.get("hex", ""))
    return bool(raw) and raw[0] == 4


# --------------------------------------------------------------------------- adb


def newest_activity(ls_output: str) -> str | None:
    """The id of the newest activity that has a features sidecar (``ls -t`` output)."""
    for name in ls_output.split():
        if name.endswith(".synapse.bin"):
            return name.removesuffix(".synapse.bin")
    return None


def _adb(*args: str) -> bytes:
    return subprocess.run(["adb", *args], check=True, capture_output=True).stdout


def pull(out: Path) -> list[Path]:
    """Copy the newest activity's sidecars off a USB-debugging phone (debug build, run-as)."""
    listing = _adb("shell", f"run-as {APP_ID} ls -t {ACTIVITIES}").decode()
    activity = newest_activity(listing)
    if activity is None:
        raise SystemExit("no activity with a .synapse.bin sidecar on the phone")
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for suffix in (".synapse.bin", ".synapse-raw.bin"):
        try:
            blob = _adb("exec-out", f"run-as {APP_ID} cat {ACTIVITIES}/{activity}{suffix}")
        except subprocess.CalledProcessError:
            continue  # a sidecar the consents did not allow does not exist
        target = out / f"{activity}{suffix}"
        target.write_bytes(blob)
        written.append(target)
    return written


# --------------------------------------------------------------------------- command line


def report(findings: list[Finding]) -> int:
    for f in findings:
        print(f"{'ERROR' if f.level == 'error' else 'note '}  {f.text}")
    errors = sum(f.level == "error" for f in findings)
    print(f"{errors} error(s)")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pull", help="copy the newest activity's sidecars with adb")
    p.add_argument("--out", type=Path, default=Path("capture"))
    c = sub.add_parser("check", help="check a capture against the band's log")
    c.add_argument("--band-log", type=Path, required=True)
    c.add_argument("--features", type=Path)
    c.add_argument("--raw", type=Path)
    c.add_argument("--no-signal-quality", action="store_true", help="that consent was not given")
    c.add_argument("--no-raw-signals", action="store_true", help="that consent was not given")
    args = ap.parse_args(argv)
    if args.cmd == "pull":
        for path in pull(args.out):
            print(path)
        return 0
    log = read_band_log(args.band_log.read_text(encoding="utf-8"))
    raw_ok = not args.no_raw_signals
    findings = check_conversation(log, raw_signals=raw_ok)
    findings += check_sidecars(
        log,
        args.features.read_bytes() if args.features else None,
        args.raw.read_bytes() if args.raw else None,
        signal_quality=not args.no_signal_quality,
        raw_signals=raw_ok,
    )
    return report(findings)


if __name__ == "__main__":
    raise SystemExit(main())
