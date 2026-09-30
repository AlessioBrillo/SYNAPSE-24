"""BLE LSL bridge host client for SYNAPSE-24.

Host-side counterpart of ``firmware/common/ble/ble_lsl_bridge.cpp``.

Protocol (read-after-write ping-pong on the SYNC characteristic):
    1. Host writes ``[uint32 seq, int64 pod_send_us]`` (12 bytes, little-endian).
    2. ESP32 records ``hub_recv_us`` / ``hub_send_us`` into its sync history.
    3. Host reads ``[uint32 seq, int64 hub_recv_us, int64 hub_send_us]`` (20 bytes).
    4. Host computes NTP-style offset and RTT and feeds a linear drift model.

This module deliberately has **no** ``pylsl`` dependency so it can be unit
tested on any host (including Windows CI without ``lsl.dll``). LSL outlet
wiring lives in ``scripts/ble_lsl_stream.py``.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field

# GATT UUIDs — must match firmware/common/ble/ble_lsl_bridge.h
# and config/hardware_bringup.yaml bringup.ble.characteristics.
GATT_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
GATT_CHAR_UUID_PPG = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
GATT_CHAR_UUID_ECG = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"
GATT_CHAR_UUID_IMU = "6E400004-B5A3-F393-E0A9-E50E24DCCA9E"
GATT_CHAR_UUID_SYNC = "6E400005-B5A3-F393-E0A9-E50E24DCCA9E"
GATT_CHAR_UUID_POWER = "6E400006-B5A3-F393-E0A9-E50E24DCCA9E"

SYNC_REQUEST_SIZE = 12  # uint32 seq + int64 pod_send_us
SYNC_RESPONSE_SIZE = 20  # uint32 seq + int64 hub_recv_us + int64 hub_send_us

# Sensor type ids — must match sensor_type_t in sensor_scheduler.h.
SENSOR_TYPE_ECG = 0
SENSOR_TYPE_PPG = 1
SENSOR_TYPE_IMU = 2
SENSOR_TYPE_EEG_IN_EAR = 3

# Raw firmware sensor_sample_t notify payload (ESP32, little-endian, 4B aligned):
#   int32 type | int64 timestamp_us | int64 lsl_timestamp_us |
#   uint16 sequence | 2B pad | union payload (36B max: 9x float32 for IMU)
_SAMPLE_HEADER_FMT = "<iqqH"
_SAMPLE_HEADER_SIZE = struct.calcsize(_SAMPLE_HEADER_FMT)  # 22
_SAMPLE_PAD = 2
_SAMPLE_PAYLOAD_FLOATS = 9
_SAMPLE_PAYLOAD_FMT = "<9f"
_SAMPLE_PAYLOAD_SIZE = struct.calcsize(_SAMPLE_PAYLOAD_FMT)  # 36
SAMPLE_NOTIFY_SIZE = _SAMPLE_HEADER_SIZE + _SAMPLE_PAD + _SAMPLE_PAYLOAD_SIZE  # 60


@dataclass
class SyncExchange:
    """One completed ping-pong clock-sync exchange (microseconds)."""

    sequence: int
    pod_send_us: int
    hub_recv_us: int
    hub_send_us: int
    pod_recv_us: int
    offset_us: int = 0
    rtt_us: int = 0

    def compute(self) -> SyncExchange:
        """Fill NTP-style offset/RTT in place and return self."""
        self.offset_us = int(
            ((self.hub_recv_us - self.pod_send_us) + (self.hub_send_us - self.pod_recv_us)) / 2
        )
        self.rtt_us = int(
            (self.pod_recv_us - self.pod_send_us) - (self.hub_send_us - self.hub_recv_us)
        )
        return self


def pack_sync_request(sequence: int, pod_send_us: int) -> bytes:
    """Pack a 12-byte sync request ``[seq, pod_send_us]`` (little-endian)."""
    return struct.pack("<I", sequence) + struct.pack("<q", pod_send_us)


def unpack_sync_response(data: bytes) -> tuple[int, int, int]:
    """Unpack a 20-byte sync response into ``(seq, hub_recv_us, hub_send_us)``."""
    if len(data) < SYNC_RESPONSE_SIZE:
        raise ValueError(f"sync response too short: {len(data)} < {SYNC_RESPONSE_SIZE}")
    seq = struct.unpack("<I", data[0:4])[0]
    hub_recv_us = struct.unpack("<q", data[4:12])[0]
    hub_send_us = struct.unpack("<q", data[12:20])[0]
    return seq, hub_recv_us, hub_send_us


def parse_sensor_sample(data: bytes) -> dict:
    """Parse a raw firmware ``sensor_sample_t`` notify payload.

    Returns a dict with ``type``, ``timestamp_us``, ``lsl_timestamp_us``,
    ``sequence`` and a ``values`` list of up to 9 floats (interpretation
    depends on ``type``: ECG=1 float, PPG=3 floats, IMU=9 floats).
    """
    if len(data) < SAMPLE_NOTIFY_SIZE:
        raise ValueError(f"sensor sample too short: {len(data)} < {SAMPLE_NOTIFY_SIZE}")
    sensor_type, timestamp_us, lsl_ts_us, sequence = struct.unpack(
        _SAMPLE_HEADER_FMT, data[0:_SAMPLE_HEADER_SIZE]
    )
    payload = struct.unpack(
        _SAMPLE_PAYLOAD_FMT, data[_SAMPLE_HEADER_SIZE + _SAMPLE_PAD : SAMPLE_NOTIFY_SIZE]
    )
    return {
        "type": sensor_type,
        "timestamp_us": timestamp_us,
        "lsl_timestamp_us": lsl_ts_us,
        "sequence": sequence,
        "values": list(payload),
    }


@dataclass
class ClockSyncHost:
    """Host-side linear drift model mirroring firmware clock_sync.cpp.

    Keeps the last ``max_history`` completed exchanges, refits
    ``offset(t) = slope_us_per_s * t + intercept_us`` when enough samples
    exist, and corrects pod timestamps into the hub clock domain.
    """

    max_history: int = 120
    min_samples_for_drift: int = 10
    exchanges: list[SyncExchange] = field(default_factory=list)
    slope_us_per_s: float = 0.0
    intercept_us: float = 0.0
    model_valid: bool = False

    def record_exchange(self, exchange: SyncExchange) -> SyncExchange:
        """Compute offset/RTT, store, and refit the drift model if ready."""
        exchange.compute()
        self.exchanges.append(exchange)
        if len(self.exchanges) > self.max_history:
            self.exchanges.pop(0)
        if len(self.exchanges) >= self.min_samples_for_drift:
            self._refit()
        return exchange

    def _refit(self) -> None:
        xs = [e.hub_recv_us / 1e6 for e in self.exchanges if e.hub_recv_us > 0]
        ys = [float(e.offset_us) for e in self.exchanges if e.hub_recv_us > 0]
        n = len(xs)
        if n < 2:
            return
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        denom = sum((x - mean_x) ** 2 for x in xs)
        if denom <= 1e-12:
            self.slope_us_per_s = 0.0
            self.intercept_us = mean_y
        else:
            slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
            self.slope_us_per_s = slope
            self.intercept_us = mean_y - slope * mean_x
        self.model_valid = True

    def correct_timestamp(self, pod_timestamp_us: int) -> int:
        """Map a pod timestamp into the hub clock domain (microseconds)."""
        if not self.model_valid:
            if self.exchanges:
                return int(pod_timestamp_us - self.exchanges[-1].offset_us)
            return int(pod_timestamp_us)
        t_s = pod_timestamp_us / 1e6
        offset = self.slope_us_per_s * t_s + self.intercept_us
        return int(pod_timestamp_us - offset)

    @property
    def last_rtt_us(self) -> int:
        """Most recent round-trip time, or 0 when no exchange completed."""
        return self.exchanges[-1].rtt_us if self.exchanges else 0

    @property
    def last_offset_us(self) -> int:
        """Most recent clock offset, or 0 when no exchange completed."""
        return self.exchanges[-1].offset_us if self.exchanges else 0


class SynapseBleClient:
    """Async BLE client for SYNAPSE-24 pods (bringup + streaming).

    ``bleak`` is imported lazily so the module stays importable (and
    testable) on hosts without BLE hardware.
    """

    def __init__(self, address: str | None = None, device_name_filter: str = "SYNAPSE") -> None:
        self.address = address
        self.device_name_filter = device_name_filter
        self.sync = ClockSyncHost()
        self._next_seq = 0

    @staticmethod
    def now_us() -> int:
        """Wall-clock time in microseconds (host clock domain)."""
        return int(time.time() * 1_000_000)

    async def scan(self, timeout_s: float = 10.0) -> str | None:
        """Scan for a SYNAPSE device; return its address or ``None``."""
        from bleak import BleakScanner

        devices = await BleakScanner.discover(timeout=timeout_s)
        filt = self.device_name_filter.upper()
        for d in devices:
            if (d.name or "").upper().find(filt) >= 0:
                self.address = d.address
                return d.address
        return None

    async def sync_exchange(self, client: object, num_exchanges: int = 1) -> list[SyncExchange]:
        """Run ``num_exchanges`` ping-pong exchanges on an open BleakClient."""
        from bleak import BleakClient  # noqa: F401  (type check only)

        results: list[SyncExchange] = []
        ble_client = client  # typed as object to avoid hard bleak dependency
        for _ in range(num_exchanges):
            self._next_seq += 1
            pod_send = self.now_us()
            await ble_client.write_gatt_char(  # type: ignore[attr-defined]
                GATT_CHAR_UUID_SYNC, pack_sync_request(self._next_seq, pod_send), response=False
            )
            raw = bytes(await ble_client.read_gatt_char(GATT_CHAR_UUID_SYNC))  # type: ignore[attr-defined]
            pod_recv = self.now_us()
            seq, hub_recv, hub_send = unpack_sync_response(raw)
            results.append(
                self.sync.record_exchange(
                    SyncExchange(
                        sequence=seq,
                        pod_send_us=pod_send,
                        hub_recv_us=hub_recv,
                        hub_send_us=hub_send,
                        pod_recv_us=pod_recv,
                    )
                )
            )
        return results
