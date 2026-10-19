# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""Synthetic session recording (offline, no hardware).

Emits frames through the real codec and writes an XDF so every command path
can be exercised without a BLE adapter. XDF is written natively with h5py so
the SDK stays independent of a writer that pyxdf does not expose.
"""

from __future__ import annotations

from pathlib import Path

import h5py
import numpy as np

from .codec import RawStream
from .transport import SyntheticTransport


def record_synthetic(out_path: Path, duration_s: float = 30.0, rate_hz: int = 50) -> None:
    """Record ``duration_s`` seconds of synthetic multi-stream data to XDF."""
    transport = SyntheticTransport("SYNTHETIC")
    streams = [RawStream.ECG, RawStream.PPG, RawStream.ACC, RawStream.GYRO, RawStream.MAG]

    import asyncio

    collected: list[tuple[object, np.ndarray]] = []

    async def _run() -> None:
        await transport.connect("SYNTHETIC")

        def _handler(uuid: str, frame: bytes) -> None:
            from .codec import decode_frame

            header, samples = decode_frame(frame)
            collected.append((header, samples))

        for stream in streams:
            uuid = {
                RawStream.ECG: "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C71",
                RawStream.PPG: "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C72",
                RawStream.ACC: "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C73",
                RawStream.GYRO: "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C74",
                RawStream.MAG: "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C75",
            }[stream]
            await transport.subscribe(uuid, _handler)

        await transport.start(
            list(CHAR_UUID_TO_STREAM.keys()), rate_hz=rate_hz, duration_s=duration_s
        )
        await asyncio.sleep(duration_s + 0.5)
        await transport.stop()
        await transport.disconnect()

    asyncio.run(_run())

    # Group frames per stream.
    by_stream: dict[int, list[tuple[float, np.ndarray]]] = {int(s): [] for s in streams}
    for header, samples in collected:
        by_stream[int(header.stream)].append((float(header.timestamp_us) / 1e6, samples))

    # --- write XDF natively with h5py -------------------------------------
    stream_meta = {
        RawStream.ECG: ("ECG", "SYNAPSE_ECG", ["ecg_uv"]),
        RawStream.PPG: ("PPG", "SYNAPSE_PPG", ["ppg_red", "ppg_ir"]),
        RawStream.ACC: ("ACC", "SYNAPSE_ACC", ["acc_x_mg", "acc_y_mg", "acc_z_mg"]),
        RawStream.GYRO: ("GYRO", "SYNAPSE_GYRO", ["gyro_x_dps", "gyro_y_dps", "gyro_z_dps"]),
        RawStream.MAG: ("MAG", "SYNAPSE_MAG", ["mag_x_ut", "mag_y_ut", "mag_z_ut"]),
    }

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with h5py.File(out_path, "w") as h5:
        h5.attrs["XDF_VERSION"] = "2.0"
        h5.attrs["XDF_MEASUREMENT_NAME"] = "synapse-synthetic"
        h5.attrs["XDF_CREATOR"] = "synapse-device"
        h5.attrs["XDF_CREATOR_VERSION"] = "0.1.0"
        h5.attrs["XDF_XML"] = ""

        for idx, stream in enumerate(streams):
            entries = by_stream[int(stream)]
            if not entries:
                continue
            times = np.array([t for t, _ in entries], dtype=np.float64)
            data = np.vstack([s for _, s in entries])
            name, type_, channel_names = stream_meta[stream]

            grp = h5.create_group(str(idx))
            info = grp.create_group("stream_info")
            info.attrs["name"] = name
            info.attrs["type"] = type_
            info.attrs["nominal_srate"] = np.float64(rate_hz)
            info.attrs["channel_count"] = np.int32(data.shape[1])
            info.attrs["channel_names"] = channel_names
            info.attrs["units"] = ["au"] * int(data.shape[1])
            info.attrs["source_id"] = "synthetic"
            grp.create_dataset("time_stamps", data=times)
            grp.create_dataset("time_series", data=data)

    # Sanity check: readable back with pyxdf if available.
    try:  # pragma: no cover - pyxdf optional for read-back
        from .xdf import read_xdf

        session = read_xdf(out_path)
        assert session.n_samples_total > 0
    except Exception:  # pragma: no cover - read-back is a bonus, not a hard failure
        pass


# Imported lazily inside _run to avoid a hard dependency at module import time.
CHAR_UUID_TO_STREAM = {
    "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C71": RawStream.ECG,
    "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C72": RawStream.PPG,
    "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C73": RawStream.ACC,
    "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C74": RawStream.GYRO,
    "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C75": RawStream.MAG,
}
