# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""Synthetic session recording (offline, no hardware).

Emits frames through the real codec and writes an **XDF 1.0** file with the
canonical writer (``synapse_device.xdf_writer``), so the result is read back
by ``pyxdf`` — a third-party parser — and not by a home-grown reader that
would happily accept a file no other tool can open.

The round-trip is a hard check: if ``pyxdf`` cannot read the file back with
the expected number of samples, the recording fails instead of leaving a
corrupt session behind.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import numpy as np

from .codec import RawHeader, RawStream, decode_frame
from .transport import SyntheticTransport
from .xdf_writer import write_xdf

# Stream metadata: name, LSL type, channel names and units.
STREAM_META: dict[RawStream, tuple[str, str, list[str], list[str]]] = {
    RawStream.ECG: ("ECG", "ECG", ["ecg_uv"], ["uV"]),
    RawStream.PPG: ("PPG", "PPG", ["ppg_red", "ppg_ir"], ["au", "au"]),
    RawStream.ACC: ("ACC", "IMU", ["acc_x_mg", "acc_y_mg", "acc_z_mg"], ["mg", "mg", "mg"]),
    RawStream.GYRO: ("GYRO", "IMU", ["gyro_x_dps", "gyro_y_dps", "gyro_z_dps"], ["dps"] * 3),
    RawStream.MAG: ("MAG", "IMU", ["mag_x_ut", "mag_y_ut", "mag_z_ut"], ["uT"] * 3),
}

CHAR_UUID_TO_STREAM = {
    "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C71": RawStream.ECG,
    "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C72": RawStream.PPG,
    "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C73": RawStream.ACC,
    "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C74": RawStream.GYRO,
    "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C75": RawStream.MAG,
}

# The nominal rate of every stream is in config/hardware.yaml
# (config/hardware.yaml -> raw_frame / sampling_rate). The synthetic
# transport emits every stream at the same rate, so the recording declares
# the rate it actually produced: a file that announces 500 Hz and carries
# 50 Hz samples is a lie the reader can only discover downstream.


class XdfRoundTripError(RuntimeError):
    """Raised when the XDF file just written cannot be read back."""


def record_synthetic(out_path: Path, duration_s: float = 30.0, rate_hz: int = 50) -> None:
    """Record ``duration_s`` seconds of synthetic multi-stream data to an XDF file.

    Args:
        out_path: Destination ``.xdf`` file.
        duration_s: Length of the synthetic recording.
        rate_hz: Rate used by the synthetic transport.

    Raises:
        XdfRoundTripError: if the file cannot be read back with the expected
            number of samples (a silent corruption would poison every
            downstream analysis).

    """
    transport = SyntheticTransport("SYNTHETIC")
    streams = list(STREAM_META)

    collected: list[tuple[RawHeader, np.ndarray]] = []

    async def _run() -> None:
        await transport.connect("SYNTHETIC")

        def _handler(uuid: str, frame: bytes) -> None:
            header, samples = decode_frame(frame)
            collected.append((header, samples))

        for stream in streams:
            uuid = next(u for u, s in CHAR_UUID_TO_STREAM.items() if s == stream)
            await transport.subscribe(uuid, _handler)

        await transport.start(
            list(CHAR_UUID_TO_STREAM.keys()), rate_hz=rate_hz, duration_s=duration_s
        )
        await asyncio.sleep(duration_s + 0.5)
        await transport.stop()
        await transport.disconnect()

    asyncio.run(_run())

    # Group frames per stream and rebuild the per-sample timeline.
    by_stream: dict[RawStream, list[tuple[float, np.ndarray]]] = {s: [] for s in streams}
    for header, samples in collected:
        by_stream[RawStream(header.stream)].append((float(header.timestamp_us) / 1e6, samples))

    t0 = time.time()
    payload: list[dict[str, object]] = []
    expected_total = 0
    for stream in streams:
        entries = by_stream[stream]
        if not entries:
            continue
        data = np.vstack([samples for _, samples in entries])
        # The frame header carries the timestamp of the first sample; the
        # following samples are spaced by the stream period. The synthetic
        # transport emits one frame per stream per period, so the rate of
        # the recording is the rate of the transport.
        rate = float(rate_hz)
        timestamps = np.concatenate(
            [entries[i][0] + np.arange(entries[i][1].shape[0]) / rate for i in range(len(entries))]
        )
        name, type_, channels, units = STREAM_META[stream]
        expected_total += int(data.shape[0])
        payload.append(
            {
                "name": name,
                "type": type_,
                "channel_count": int(data.shape[1]),
                "sampling_rate": rate,
                "channel_format": "float32",
                "channel_names": channels,
                "channel_units": units,
                "source_id": f"synapse-device-synthetic-{name.lower()}",
                "data": data,
                "timestamps": timestamps + t0,
            }
        )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_xdf(out_path, payload)

    recovered = _verify_roundtrip(out_path)
    if recovered != expected_total:
        raise XdfRoundTripError(
            f"XDF round-trip mismatch for {out_path}: wrote {expected_total} samples, "
            f"read back {recovered}"
        )


def _verify_roundtrip(path: Path) -> int:
    """Read the XDF back with pyxdf and return the number of recovered samples."""
    try:
        import pyxdf
    except ImportError as exc:  # pragma: no cover - pyxdf is an optional extra
        raise XdfRoundTripError(
            "pyxdf is required to verify the XDF round-trip: pip install synapse-device[xdf]"
        ) from exc
    streams, _header = pyxdf.load_xdf(str(path))
    return int(sum(len(s.get("time_stamps", [])) for s in streams))
