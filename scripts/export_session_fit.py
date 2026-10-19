#!/usr/bin/env python3
r"""Export a SYNAPSE-24 XDF session to a FIT file with Synapse developer data.

Produces a FIT file readable by Averyn's SynapseFitImporter (see
`docs/integrations/synapse.md` in the Averyn repo). Flat scalar developer
fields only, per the FIT spec (no structs), matching the canonical table:

    synapse_stress   uint8   0=baseline, 1=stress, 2=artifact, 3=unknown
    synapse_sqi      uint8   0-255 (PPG signal quality index)
    synapse_map      uint8   0-255 (motion artifact probability)
    synapse_rmssd    uint16  ms, RMSSD over the window (0xFFFF if invalid)
    synapse_sdnn     uint16  ms, SDNN over the window (0xFFFF if invalid)
    synapse_lf_hf    uint16  x100, LF/HF ratio (0xFFFF if invalid)

Developer field IDs are fixed (0-7) and must match the consumer contract;
field description messages are written so a standard FIT parser can decode
the developer data without prior knowledge.

Usage:
    uv run python scripts/export_session_fit.py session.xdf out.fit \\
        [--user-id USER_ID] [--window-s SEC] [--device-id SERIAL]

The script is intentionally dependency-light: pyxdf (already a project dep)
for XDF reading, neurokit2 for R-peak detection, and a hand-rolled FIT
writer for the record/session/developer-data messages.
"""

from __future__ import annotations

import argparse
import struct
import sys
from datetime import UTC, datetime, timezone
from pathlib import Path

import numpy as np
import pyxdf

# --- project imports -------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from synapse24.signal_quality.ecg import (  # noqa: E402
    compute_hrv_metrics,
    detect_r_peaks_neurokit,
)

# ---------------------------------------------------------------------------
# FIT constants
# ---------------------------------------------------------------------------

FIT_FILE_HEADER_SIZE = 14
FIT_CRC_SIZE = 2
FIT_FILE_HEADER_SIZE_TOTAL = FIT_FILE_HEADER_SIZE + FIT_CRC_SIZE

FIT_BASE_TYPE_UINT8 = 2
FIT_BASE_TYPE_UINT16 = 132
FIT_BASE_TYPE_UINT32 = 134
FIT_BASE_TYPE_FLOAT32 = 136
FIT_BASE_TYPE_SINT16 = 131
FIT_BASE_TYPE_ENUM = 0
FIT_BASE_TYPE_STRING = 7

FIT_MESG_FILE_ID = 0
FIT_MESG_DEVICE_INFO = 23
FIT_MESG_SESSION = 18
FIT_MESG_RECORD = 20
FIT_MESG_DEVELOPER_DATA_ID = 206
FIT_MESG_FIELD_DESCRIPTION = 207
FIT_MESG_DEVELOPER_DATA = 209

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=UTC)
INVALID_U8 = 0xFF
INVALID_U16 = 0xFFFF
INVALID_U32 = 0xFFFFFFFF

# Synapse developer field IDs (fixed, must match the consumer contract).
SYNAPSE_DEV_FIELD_STRESS = 0
SYNAPSE_DEV_FIELD_SQI = 1
SYNAPSE_DEV_FIELD_MAP = 2
SYNAPSE_DEV_FIELD_ECG_QUALITY = 3
SYNAPSE_DEV_FIELD_SLEEP_STAGE = 4
SYNAPSE_DEV_FIELD_RMSSD = 5
SYNAPSE_DEV_FIELD_SDNN = 6
SYNAPSE_DEV_FIELD_LF_HF = 7

SYNAPSE_DEV_FIELD_NAMES = {
    SYNAPSE_DEV_FIELD_STRESS: "synapse_stress",
    SYNAPSE_DEV_FIELD_SQI: "synapse_sqi",
    SYNAPSE_DEV_FIELD_MAP: "synapse_map",
    SYNAPSE_DEV_FIELD_ECG_QUALITY: "synapse_ecg_quality",
    SYNAPSE_DEV_FIELD_SLEEP_STAGE: "synapse_sleep_stage",
    SYNAPSE_DEV_FIELD_RMSSD: "synapse_rmssd",
    SYNAPSE_DEV_FIELD_SDNN: "synapse_sdnn",
    SYNAPSE_DEV_FIELD_LF_HF: "synapse_lf_hf",
}

# Field definition numbers used inside the developer-data block of a record.
# FIT reserves field definition numbers 0-253 for standard fields; developer
# data fields are addressed by their developer_data_index in message 209.
DEV_FIELD_NUM_STRESS = 0
DEV_FIELD_NUM_SQI = 1
DEV_FIELD_NUM_MAP = 2
DEV_FIELD_NUM_ECG_QUALITY = 3
DEV_FIELD_NUM_SLEEP_STAGE = 4
DEV_FIELD_NUM_RMSSD = 5
DEV_FIELD_NUM_SDNN = 6
DEV_FIELD_NUM_LF_HF = 7

# Application: "SYNAPSE" as a 4-byte identifier stored in the high 32 bits of
# the developer application version (FIT convention).
DEV_APP_VERSION = 0x53594E41  # "SYNA" ASCII


def fit_crc16(data: bytes, crc: int = 0) -> int:
    """FIT CRC-16 (CCITT, poly 0x1021, init 0)."""
    for b in data:
        crc = (crc >> 8) | (crc << 8)
        crc &= 0xFFFF
        crc ^= b
        crc ^= (crc & 0xFF) >> 4
        crc ^= (crc << 12) & 0xFFFF
        crc ^= (crc & 0xFF) << 5
        crc &= 0xFFFF
    return crc & 0xFFFF


def write_crc16(buf: bytearray) -> None:
    """Append CRC-16 over buf contents so far (little-endian)."""
    crc = fit_crc16(bytes(buf))
    buf += struct.pack("<H", crc)


def fit_timestamp(dt: datetime) -> int:
    """Seconds from FIT epoch (1989-12-31 UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int((dt - FIT_EPOCH).total_seconds())


def mesg_header(local_msg_type: int, is_definition: bool = False) -> int:
    """Build the first byte of a FIT message."""
    # bit7 = 1 definition / 0 data; bit6 = 1 has developer data; bits 5-0 = local msg type
    return (0x80 if is_definition else 0x00) | (local_msg_type & 0x3F)


# ---------------------------------------------------------------------------
# FIT writer helpers
# ---------------------------------------------------------------------------


class FitWriter:
    """Minimal FIT file writer. Appends to a binary buffer."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self._crc = 0

    def bytes(self) -> bytes:
        return bytes(self.buf)

    def write(self, data) -> None:
        if isinstance(data, int):
            data = bytes([data])
        self.buf += data

    def write_u8(self, v: int) -> None:
        self.buf += struct.pack("<B", v & 0xFF)

    def write_u16(self, v: int) -> None:
        self.buf += struct.pack("<H", v & 0xFFFF)

    def write_u32(self, v: int) -> None:
        self.buf += struct.pack("<I", v & 0xFFFFFFFF)

    def write_s16(self, v: int) -> None:
        self.buf += struct.pack("<h", v)

    def write_f32(self, v: float) -> None:
        self.buf += struct.pack("<f", v)

    def write_str(self, s: str, n: int) -> None:
        b = s.encode("ascii")[:n]
        self.buf += b
        self.buf += b"\x00" * (n - len(b))

    def crc_update(self, data: bytes) -> None:
        self._crc = fit_crc16(data, self._crc)

    def write_crc(self) -> None:
        self.write_u16(self._crc)


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------


def write_file_id(w: FitWriter, serial: str) -> None:
    """FIT File ID message (mesg 0)."""
    # Definition
    w.write(mesg_header(0, True))  # local msg type 0
    w.write_u8(0)  # reserved
    w.write_u8(0)  # arch little-endian
    w.write_u16(FIT_MESG_FILE_ID)
    w.write_u8(5)  # num fields
    fields = [
        (4, 1, FIT_BASE_TYPE_UINT32),  # serial_number
        (1, 1, FIT_BASE_TYPE_UINT16),  # manufacturer
        (2, 2, FIT_BASE_TYPE_UINT16),  # product
        (3, 1, FIT_BASE_TYPE_UINT16),  # time_created (uint32 stored as 2B? no, field 3 is uint32)
        (5, 1, FIT_BASE_TYPE_UINT8),  # number
    ]
    field_defs = [
        (4, 4, FIT_BASE_TYPE_UINT32),
        (1, 2, FIT_BASE_TYPE_UINT16),
        (2, 2, FIT_BASE_TYPE_UINT16),
        (3, 4, FIT_BASE_TYPE_UINT32),
        (5, 1, FIT_BASE_TYPE_UINT8),
    ]
    for fnum, size, btype in field_defs:
        w.write_u8(fnum)
        w.write_u8(size)
        w.write_u8(btype)
    write_crc16(w.buf)

    # Data
    w.write(mesg_header(0, False))
    w.write_u32(int(serial.replace("SYN-", "")) if serial.startswith("SYN-") else 0)
    w.write_u16(0x01)  # manufacturer (Synapse)
    w.write_u16(0x01)  # product
    w.write_u32(fit_timestamp(datetime.now(UTC)))
    w.write_u8(1)
    write_crc16(w.buf)


def write_device_info(w: FitWriter, serial: str) -> None:
    """FIT Device Info message (mesg 23)."""
    # Definition: local msg type 1
    w.write(mesg_header(1, True))
    w.write_u8(0)
    w.write_u8(0)
    w.write_u16(FIT_MESG_DEVICE_INFO)
    w.write_u8(6)
    field_defs = [
        (0, 4, FIT_BASE_TYPE_UINT32),  # timestamp
        (3, 1, FIT_BASE_TYPE_UINT8),  # device_index
        (4, 4, FIT_BASE_TYPE_UINT32),  # serial_number
        (10, 1, FIT_BASE_TYPE_UINT8),  # battery_level
        (12, 2, FIT_BASE_TYPE_UINT16),  # product
        (13, 1, FIT_BASE_TYPE_UINT8),  # software_version
    ]
    for fnum, size, btype in field_defs:
        w.write_u8(fnum)
        w.write_u8(size)
        w.write_u8(btype)
    write_crc16(w.buf)

    # Data
    w.write(mesg_header(1, False))
    w.write_u32(fit_timestamp(datetime.now(UTC)))
    w.write_u8(0)
    w.write_u32(int(serial.replace("SYN-", "")) if serial.startswith("SYN-") else 0)
    w.write_u8(INVALID_U8)  # battery unknown
    w.write_u16(0x01)
    w.write_u8(1)
    write_crc16(w.buf)


def write_session(w: FitWriter, start_dt: datetime, duration_s: float, sport: int = 0) -> None:
    """FIT Session message (mesg 18)."""
    # Definition: local msg type 2
    w.write(mesg_header(2, True))
    w.write_u8(0)
    w.write_u8(0)
    w.write_u16(FIT_MESG_SESSION)
    w.write_u8(5)
    field_defs = [
        (0, 4, FIT_BASE_TYPE_UINT32),  # timestamp
        (2, 2, FIT_BASE_TYPE_UINT16),  # sport
        (3, 4, FIT_BASE_TYPE_UINT32),  # start_time
        (4, 2, FIT_BASE_TYPE_UINT16),  # total_elapsed_time (uint32 in spec, use uint16)
        (253, 4, FIT_BASE_TYPE_UINT32),  # local timestamp (dev convenience)
    ]
    for fnum, size, btype in field_defs:
        w.write_u8(fnum)
        w.write_u8(size)
        w.write_u8(btype)
    write_crc16(w.buf)

    # Data
    w.write(mesg_header(2, False))
    w.write_u32(fit_timestamp(start_dt))
    w.write_u16(sport)
    w.write_u32(fit_timestamp(start_dt))
    w.write_u16(int(duration_s) & 0xFFFF)
    w.write_u32(fit_timestamp(start_dt))
    write_crc16(w.buf)


def write_developer_data_id(w: FitWriter, app_version: int = DEV_APP_VERSION) -> None:
    """FIT Developer Data ID message (mesg 206): declares dev field indices."""
    # Definition: local msg type 3
    w.write(mesg_header(3, True))
    w.write_u8(0)
    w.write_u8(0)
    w.write_u16(FIT_MESG_DEVELOPER_DATA_ID)
    w.write_u8(2)
    field_defs = [
        (0, 1, FIT_BASE_TYPE_UINT8),  # developer_data_index
        (4, 2, FIT_BASE_TYPE_UINT16),  # developer_application_version
    ]
    for fnum, size, btype in field_defs:
        w.write_u8(fnum)
        w.write_u8(size)
        w.write_u8(btype)
    write_crc16(w.buf)

    # Data: one message per developer field index (0-7)
    for idx in range(8):
        w.write(mesg_header(3, False))
        w.write_u8(idx)
        w.write_u16(app_version)
        write_crc16(w.buf)


def write_field_description(w: FitWriter) -> None:
    """FIT Field Description message (mesg 207): names + base types of dev fields."""
    # Definition: local msg type 4
    w.write(mesg_header(4, True))
    w.write_u8(0)
    w.write_u8(0)
    w.write_u16(FIT_MESG_FIELD_DESCRIPTION)
    w.write_u8(4)
    field_defs = [
        (0, 1, FIT_BASE_TYPE_UINT8),  # developer_data_index
        (1, 1, FIT_BASE_TYPE_UINT16),  # fit_base_type_id
        (2, 16, FIT_BASE_TYPE_STRING),  # field_name (16 bytes)
        (3, 1, FIT_BASE_TYPE_UINT8),  # field_definition_number
    ]
    for fnum, size, btype in field_defs:
        w.write_u8(fnum)
        w.write_u8(size)
        w.write_u8(btype)
    write_crc16(w.buf)

    base_type_by_field = {
        SYNAPSE_DEV_FIELD_STRESS: FIT_BASE_TYPE_ENUM,
        SYNAPSE_DEV_FIELD_SQI: FIT_BASE_TYPE_UINT8,
        SYNAPSE_DEV_FIELD_MAP: FIT_BASE_TYPE_UINT8,
        SYNAPSE_DEV_FIELD_ECG_QUALITY: FIT_BASE_TYPE_UINT8,
        SYNAPSE_DEV_FIELD_SLEEP_STAGE: FIT_BASE_TYPE_ENUM,
        SYNAPSE_DEV_FIELD_RMSSD: FIT_BASE_TYPE_UINT16,
        SYNAPSE_DEV_FIELD_SDNN: FIT_BASE_TYPE_UINT16,
        SYNAPSE_DEV_FIELD_LF_HF: FIT_BASE_TYPE_UINT16,
    }
    field_num_by_field = {
        SYNAPSE_DEV_FIELD_STRESS: DEV_FIELD_NUM_STRESS,
        SYNAPSE_DEV_FIELD_SQI: DEV_FIELD_NUM_SQI,
        SYNAPSE_DEV_FIELD_MAP: DEV_FIELD_NUM_MAP,
        SYNAPSE_DEV_FIELD_ECG_QUALITY: DEV_FIELD_NUM_ECG_QUALITY,
        SYNAPSE_DEV_FIELD_SLEEP_STAGE: DEV_FIELD_NUM_SLEEP_STAGE,
        SYNAPSE_DEV_FIELD_RMSSD: DEV_FIELD_NUM_RMSSD,
        SYNAPSE_DEV_FIELD_SDNN: DEV_FIELD_NUM_SDNN,
        SYNAPSE_DEV_FIELD_LF_HF: DEV_FIELD_NUM_LF_HF,
    }
    for idx in range(8):
        name = SYNAPSE_DEV_FIELD_NAMES[idx]
        w.write(mesg_header(4, False))
        w.write_u8(idx)
        w.write_u8(base_type_by_field[idx])
        w.write_str(name, 16)
        w.write_u8(field_num_by_field[idx])
        write_crc16(w.buf)


RECORD_LOCAL_TYPE = 10
DEV_DATA_LOCAL_TYPE = 11


def dev_size(field_id: int) -> int:
    return (
        2
        if field_id in (SYNAPSE_DEV_FIELD_RMSSD, SYNAPSE_DEV_FIELD_SDNN, SYNAPSE_DEV_FIELD_LF_HF)
        else 1
    )


def write_record_definitions(w: FitWriter) -> None:
    """Write FIT definition messages once (record lt=10, developer data lt=11)."""
    dev_fields = [
        (DEV_FIELD_NUM_STRESS, SYNAPSE_DEV_FIELD_STRESS),
        (DEV_FIELD_NUM_SQI, SYNAPSE_DEV_FIELD_SQI),
        (DEV_FIELD_NUM_MAP, SYNAPSE_DEV_FIELD_MAP),
        (DEV_FIELD_NUM_ECG_QUALITY, SYNAPSE_DEV_FIELD_ECG_QUALITY),
        (DEV_FIELD_NUM_SLEEP_STAGE, SYNAPSE_DEV_FIELD_SLEEP_STAGE),
        (DEV_FIELD_NUM_RMSSD, SYNAPSE_DEV_FIELD_RMSSD),
        (DEV_FIELD_NUM_SDNN, SYNAPSE_DEV_FIELD_SDNN),
        (DEV_FIELD_NUM_LF_HF, SYNAPSE_DEV_FIELD_LF_HF),
    ]

    # Record definition (local msg type 10)
    w.write(mesg_header(RECORD_LOCAL_TYPE, True))
    w.write_u8(0)
    w.write_u8(0)
    w.write_u16(FIT_MESG_RECORD)
    w.write_u8(2)
    for fnum, size, btype in ((253, 4, FIT_BASE_TYPE_UINT32), (3, 1, FIT_BASE_TYPE_UINT8)):
        w.write_u8(fnum)
        w.write_u8(size)
        w.write_u8(btype)
    write_crc16(w.buf)

    # Developer data definition (local msg type 11)
    w.write(mesg_header(DEV_DATA_LOCAL_TYPE, True))
    w.write_u8(0)
    w.write_u8(0)
    w.write_u16(FIT_MESG_DEVELOPER_DATA)
    w.write_u8(2 + len(dev_fields))
    for fnum, size, btype in ((253, 4, FIT_BASE_TYPE_UINT32), (0, 1, FIT_BASE_TYPE_UINT8)):
        w.write_u8(fnum)
        w.write_u8(size)
        w.write_u8(btype)
    for fnum, fid in dev_fields:
        w.write_u8(fnum)
        w.write_u8(dev_size(fid))
        w.write_u8(FIT_BASE_TYPE_UINT16 if dev_size(fid) == 2 else FIT_BASE_TYPE_UINT8)
    write_crc16(w.buf)


def write_record_data(
    w: FitWriter,
    timestamp_s: int,
    hr_bpm: int,
    ppg_sqi: int,
    ppg_map: int,
    ecg_quality: int,
    stress: int,
    sleep_stage: int,
    rmssd_ms: int,
    sdnn_ms: int,
    lf_hf: int,
) -> None:
    """FIT Record + Developer Data messages (definitions already emitted)."""
    dev_fields = [
        (DEV_FIELD_NUM_STRESS, stress, SYNAPSE_DEV_FIELD_STRESS),
        (DEV_FIELD_NUM_SQI, ppg_sqi, SYNAPSE_DEV_FIELD_SQI),
        (DEV_FIELD_NUM_MAP, ppg_map, SYNAPSE_DEV_FIELD_MAP),
        (DEV_FIELD_NUM_ECG_QUALITY, ecg_quality, SYNAPSE_DEV_FIELD_ECG_QUALITY),
        (DEV_FIELD_NUM_SLEEP_STAGE, sleep_stage, SYNAPSE_DEV_FIELD_SLEEP_STAGE),
        (DEV_FIELD_NUM_RMSSD, rmssd_ms, SYNAPSE_DEV_FIELD_RMSSD),
        (DEV_FIELD_NUM_SDNN, sdnn_ms, SYNAPSE_DEV_FIELD_SDNN),
        (DEV_FIELD_NUM_LF_HF, lf_hf, SYNAPSE_DEV_FIELD_LF_HF),
    ]

    # Record data (local msg type 10)
    w.write(mesg_header(RECORD_LOCAL_TYPE, False))
    w.write_u32(timestamp_s)
    w.write_u8(hr_bpm if (0 <= hr_bpm <= 255) else INVALID_U8)
    write_crc16(w.buf)

    # Developer data values (local msg type 11)
    w.write(mesg_header(DEV_DATA_LOCAL_TYPE, False))
    w.write_u32(timestamp_s)
    w.write_u8(0)  # developer_data_index 0
    for fnum, val, fid in dev_fields:
        size = dev_size(fid)
        u = 0 if val is None else int(val)
        if size == 2:
            w.write_u16(u if u != INVALID_U16 else INVALID_U16)
        else:
            w.write_u8(u if u != INVALID_U8 else INVALID_U8)
    write_crc16(w.buf)


# ---------------------------------------------------------------------------
# XDF reading
# ---------------------------------------------------------------------------


def load_streams(xdf_path: Path):
    """Return list of stream dicts from an XDF (robust to missing type/name)."""
    streams, _ = pyxdf.load_xdf(str(xdf_path))
    out = []
    for s in streams:
        time_stamps = s.get("time_stamps")
        time_series = s.get("time_series")
        if time_stamps is None or time_series is None:
            continue
        ts = np.asarray(time_stamps, dtype=np.float64).ravel()
        data = np.asarray(time_series)
        out.append(
            {
                "name": s.get("name") or "",
                "type": s.get("type") or "",
                "timestamps": ts,
                "data": data,
            }
        )
    return out


def pick_primary_stream(streams):
    """Largest stream by sample count (assumed to be the main signal)."""
    if not streams:
        return None
    return max(streams, key=lambda s: len(s["timestamps"]))


# ---------------------------------------------------------------------------
# Main export
# ---------------------------------------------------------------------------


def export_fit(xdf_path: Path, out_path: Path, serial: str, window_s: float = 30.0) -> None:
    streams = load_streams(xdf_path)
    primary = pick_primary_stream(streams)
    if primary is None:
        raise SystemExit("No usable stream found in XDF.")

    sig = primary["data"]
    if sig.ndim > 1:
        sig = sig[:, 0]
    sig = sig.astype(np.float64)
    ts = primary["timestamps"]
    t0 = float(ts[0])
    t1 = float(ts[-1])
    duration_s = t1 - t0
    fs = _estimate_fs(ts)

    print(
        f"Primary stream: {len(ts)} samples @ {fs:.1f} Hz, duration {duration_s:.1f}s",
        file=sys.stderr,
    )

    w = FitWriter()

    # File header
    w.buf += struct.pack(
        "<BBHI4sH",
        FIT_FILE_HEADER_SIZE,
        0x10,  # header size, protocol ver 1.0
        0x0815,  # profile version
        0,  # data size placeholder (fixed at end)
        b".FIT",  # magic
        0,
    )  # header CRC placeholder
    w.crc_update(w.buf[:12])
    w.buf[12:14] = struct.pack("<H", fit_crc16(bytes(w.buf[:12])))

    # Fixed messages
    write_file_id(w, serial)
    write_device_info(w, serial)
    write_session(w, datetime.now(UTC), duration_s)
    write_developer_data_id(w)
    write_field_description(w)

    # Record definitions once (record lt=10, developer data lt=11)
    write_record_definitions(w)

    # Records: one per window_s seconds
    n_records = 0

    try:
        r_peaks = detect_r_peaks_neurokit(sig, fs)
    except Exception as e:
        print(f"WARNING: R-peak detection failed: {e}", file=sys.stderr)
        r_peaks = np.array([], dtype=np.int64)

    rr_times = r_peaks / fs if len(r_peaks) else np.array([])

    n_windows = max(1, int(duration_s // window_s))
    for wi in range(n_windows):
        win_t0 = t0 + wi * window_s
        win_t1 = min(t0 + (wi + 1) * window_s, t1)
        mask = (rr_times >= win_t0) & (rr_times < win_t1)
        win_peaks = r_peaks[mask]
        hrv = compute_hrv_metrics(win_peaks, fs) if len(win_peaks) >= 2 else {}

        rmssd = hrv.get("rmssd_ms", INVALID_U16)
        sdnn = hrv.get("sdnn_ms", INVALID_U16)
        lf_hf_raw = hrv.get("lf_hf_ratio", None)
        lf_hf = int(round(lf_hf_raw * 100)) if lf_hf_raw is not None else INVALID_U16

        hr = int(round(hrv.get("hr_mean_bpm", INVALID_U8))) if hrv else INVALID_U8

        ts_fit = fit_timestamp(datetime.fromtimestamp(win_t0, tz=UTC))

        write_record_data(
            w,
            timestamp_s=ts_fit,
            hr_bpm=hr,
            ppg_sqi=INVALID_U8,
            ppg_map=INVALID_U8,
            ecg_quality=INVALID_U8,
            stress=3,  # unknown until triage wired (see decision D8)
            sleep_stage=INVALID_U8,
            rmssd_ms=int(rmssd) if rmssd != INVALID_U16 else INVALID_U16,
            sdnn_ms=int(sdnn) if sdnn != INVALID_U16 else INVALID_U16,
            lf_hf=lf_hf,
        )
        n_records += 1

    # Fix data size in header (bytes 4-7)
    data_size = len(w.buf) - FIT_FILE_HEADER_SIZE_TOTAL
    w.buf[4:8] = struct.pack("<I", data_size)

    out_path.write_bytes(w.bytes())
    print(f"Exported {n_records} record(s) to {out_path} ({data_size} bytes data).")


def _estimate_fs(timestamps: np.ndarray) -> float:
    if len(timestamps) < 2:
        return 500.0
    d = np.diff(timestamps)
    d = d[d > 0]
    if len(d) == 0:
        return 500.0
    return float(1.0 / np.median(d))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xdf", type=Path, help="Input XDF file")
    parser.add_argument("out", type=Path, help="Output FIT file")
    parser.add_argument("--device-id", default="SYN-000000", help="Device serial (SYN-XXXXXX)")
    parser.add_argument("--window-s", type=float, default=30.0, help="HRV window in seconds")
    args = parser.parse_args()

    if not args.xdf.is_file():
        parser.error(f"XDF not found: {args.xdf}")

    export_fit(args.xdf, args.out, args.device_id, args.window_s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
