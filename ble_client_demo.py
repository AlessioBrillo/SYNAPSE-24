#!/usr/bin/env python3
"""BLE Client demo.

Clock Sync Ping-Pong Protocol companion to firmware/esp32_tier0/main/ble/ble_lsl_bridge.cpp.

Protocol:
  1. Client writes [uint32 seq, int64 pod_send_us] to Sync characteristic (handle +1)
  2. Server (ESP32) responds by updating the Sync characteristic value to:
     [uint32 seq, int64 hub_recv_us, int64 hub_send_us]
  3. Client reads Sync characteristic to get the reply timestamps
  4. Client computes offset = ((hub_recv - pod_send) + (hub_send - pod_recv)) / 2
     but since pod_recv is when we read (approximately hub_send + RTT/2),
     simplified: offset ≈ hub_recv - pod_send - (hub_send - hub_recv)/2

   More precisely, using the NTP-style algorithm from clock_sync.cpp:
     offset = ((hub_recv_us - pod_send_us) + (hub_send_us - pod_recv_us)) / 2
   where pod_recv_us is the time the pod received our request (not directly known,
   but we can approximate or use the round-trip time).

   Actually, the way the firmware works:
   - Pod writes [seq, pod_send_us] to sync char
   - Hub receives it, records hub_recv_us = esp_timer_get_time(), hub_send_us = esp_timer_get_time() + small_delay
   - Hub stores in sync_history[idx] = {sequence, pod_send_us, hub_recv_us, hub_send_us, 0, 0, 0.0}
   - Pod reads sync char, gets [seq, hub_recv_us, hub_send_us]
   - Pod then computes: offset = ((hub_recv_us - pod_send_us) + (hub_send_us - pod_recv_us)) / 2
     but pod_recv_us is approximately the time the pod read the characteristic,
     which is close to hub_send_us + BLE_rtt/2

   The firmware stores pod_recv_us when the pod reads the characteristic response.
   Looking at the code more carefully... the pod reads the characteristic to get the
   hub timestamps, then those are used for the drift estimation.

   Let me just implement a working client that does the exchange and prints results.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import sys
import time
from pathlib import Path

# BLE SYNAPSE-24 UUIDs (matches firmware)
BLE_LSL_UUID_BASE = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
BLE_LSL_UUID_SYNC = "6E400005-B5A3-F393-E0A9-E50E24DCCA9E"

# Characteristic handles (computed from UUID base)
# The firmware uses base UUID with suffixes _02 _03 _04 _05
# Sync characteristic is the 5th one (index 4)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("synapse_ble_client")


async def find_synapse_device():
    """Discover SYNAPSE-24 BLE device."""
    from bleak import BleakScanner

    logger.info("Scanning for BLE devices...")
    scanner = BleakScanner()
    devices = await scanner.async_scan()

    for addr, device in devices.items():
        name = device.name or ""
        if "SYNAPSE" in name.upper():
            logger.info(f"Found SYNAPSE device: {name} [{addr}]")
            return addr

    # Also try by UUID advertisement
    for addr, device in devices.items():
        name = device.name or ""
        # Try to check if it's a SYNAPSE device by looking at services
        logger.info(f"Device: {name} [{addr}]")

    logger.warning("No SYNAPSE-24 device found during scan")
    return None


async def _perform_sync_exchange(client, sync_char_uuid: str) -> None:
    """Perform a single ping-pong sync exchange and log results."""
    current_seq = 1
    pod_send_us = int(time.time() * 1_000_000)

    request_data = struct.pack("<I", current_seq) + struct.pack("<q", pod_send_us)
    logger.info(f"Sending sync request: seq={current_seq}, pod_send_us={pod_send_us}")
    logger.info(f"  Request data ({len(request_data)} bytes): {request_data.hex()}")

    await client.write_gatt_char(sync_char_uuid, request_data, response=False)
    logger.info("Sync request sent via BLE notify/write")

    logger.info("Waiting for hub reply (1-2 seconds)...")
    await asyncio.sleep(2.0)

    logger.info("Reading sync characteristic for hub response...")
    response_value = await client.read_gatt_char(sync_char_uuid)
    logger.info(f"Response value: {len(response_value)} bytes")
    response_hex = response_value.hex() if len(response_value) > 0 else "empty"
    logger.info(f"  Raw hex: {response_hex}")

    if len(response_value) >= 20:
        _log_full_exchange(response_value, pod_send_us)
    elif len(response_value) >= 12:
        seq_r = struct.unpack("<I", response_value[0:4])[0]
        pod_send_r = struct.unpack("<q", response_value[4:12])[0]
        logger.info(f"  Sequence: {seq_r}")
        logger.info(f"  Echoed pod_send_us: {pod_send_r} ({pod_send_r / 1e6:.3f} s)")
    else:
        logger.warning(f"Unexpected response length: {len(response_value)} bytes")
        logger.info("  Minimum expected: 12 bytes (seq + pod_send) or 20 bytes (full exchange)")


def _log_full_exchange(response_value: bytes, pod_send_us: int) -> None:
    """Decode and log a full 20-byte sync exchange response."""
    seq_r = struct.unpack("<I", response_value[0:4])[0]
    hub_recv_us = struct.unpack("<q", response_value[4:12])[0]
    hub_send_us = struct.unpack("<q", response_value[12:20])[0]

    logger.info(f"  Sequence: {seq_r}")
    logger.info(f"  Hub recv_us: {hub_recv_us} ({hub_recv_us / 1e6:.3f} s)")
    logger.info(f"  Hub send_us: {hub_send_us} ({hub_send_us / 1e6:.3f} s)")

    pod_recv_us = int(time.time() * 1_000_000)

    offset_roundtrip = ((hub_recv_us - pod_send_us) + (hub_send_us - pod_recv_us)) / 2.0
    one_way_approx = hub_recv_us - pod_send_us

    logger.info("\n=== Clock Sync Results ===")
    logger.info(f"  Pod send_us:    {pod_send_us} ({pod_send_us / 1e6:.6f} s)")
    logger.info(f"  Hub recv_us:    {hub_recv_us} ({hub_recv_us / 1e6:.6f} s)")
    logger.info(f"  Hub send_us:    {hub_send_us} ({hub_send_us / 1e6:.6f} s)")
    logger.info(f"  Pod recv_us:    {pod_recv_us} ({pod_recv_us / 1e6:.6f} s)")
    logger.info("")
    logger.info(f"  One-way estimate: {one_way_approx / 1e6:.6f} s")
    logger.info(f"  Round-trip offset: {offset_roundtrip / 1e6:.6f} s")
    logger.info(f"  Round-trip RTT: {(pod_recv_us - pod_send_us) / 1e6:.6f} s")
    logger.info("")


async def run_clock_sync_demo():
    """Run the clock sync ping-pong demonstration."""
    # Find the device
    device_addr = await find_synapse_device()
    if device_addr is None:
        logger.error("SYNAPSE-24 BLE device not found")
        return

    from bleak import BleakClient

    # Sync characteristic UUID (derived from base)
    # Based on firmware: base UUID + suffix for char 5
    # The actual UUID is hardcoded in firmware: gatt_svr_chr_uuid_sync
    sync_char_uuid = BLE_LSL_UUID_SYNC

    logger.info(f"Connecting to {device_addr}...")

    try:
        async with BleakClient(device_addr) as client:
            logger.info("Connected!")

            # Step 1: Read initial sync state
            logger.info("Reading initial sync characteristic value...")
            initial_value = await client.read_gatt_char(sync_char_uuid)
            logger.info(f"Initial sync char value: {len(initial_value)} bytes")
            if len(initial_value) >= 4:
                seq = struct.unpack("<I", initial_value[0:4])[0]
                logger.info(f"  Sequence number: {seq}")

            await _perform_sync_exchange(client, sync_char_uuid)

            logger.info("\n=== Demo Complete ===")
            logger.info(
                "Run multiple exchanges for drift estimation (firmware needs ~60s of data)."
            )

    except Exception:
        logger.exception("BLE client error")
        sys.exit(1)


async def main():
    """Main entry point."""
    logger.info("=" * 60)
    logger.info("SYNAPSE-24 BLE Clock Sync Demo")
    logger.info("=" * 60)
    logger.info("")
    logger.info("This demo performs the BLE ping-pong clock sync protocol")
    logger.info("with the SYNAPSE-224 ESP32 Tier 0 firmware.")
    logger.info("")
    logger.info("Protocol:")
    logger.info("  1. Read sync characteristic (initial state)")
    logger.info("  2. Write [seq, pod_send_us] to sync characteristic")
    logger.info("  3. ESP32 hub records timestamps and stores in history")
    logger.info("  4. Read sync characteristic (get [seq, hub_recv_us, hub_send_us])")
    logger.info("  5. Compute clock offset using NTP-style algorithm")
    logger.info("")
    logger.info("For full drift estimation, run multiple exchanges over ~60s.")
    logger.info("")

    await run_clock_sync_demo()


if __name__ == "__main__":
    asyncio.run(main())
