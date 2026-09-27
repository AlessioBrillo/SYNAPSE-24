#!/usr/bin/env python3
"""Build and flash ESP32-S3 Tier 0 firmware for SYNAPSE-24 Phase 1 bringup.

This script automates the ESP-IDF build process and flashing for the
forearm hub (ECG+PPG+IMU) firmware with embedded triage INT8 model.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class FirmwareBuilder:
    """Manages ESP-IDF build and flash for SYNAPSE-24 firmware."""

    def __init__(
        self,
        firmware_dir: Path,
        build_dir: Path | None = None,
        target: str = "esp32s3",
    ) -> None:
        self.firmware_dir = Path(firmware_dir).resolve()
        self.build_dir = Path(build_dir).resolve() if build_dir else self.firmware_dir / "build"
        self.target = target
        self.bin_path = self.build_dir / "synapse_tier0.bin"
        self.metadata_path = self.build_dir / "firmware_metadata.json"

    def check_esp_idf(self) -> bool:
        """Verify ESP-IDF environment is set up."""
        idf_path = os.environ.get("IDF_PATH")
        if not idf_path:
            logger.error("IDF_PATH not set. Source export.sh from ESP-IDF installation.")
            return False

        idf_py = shutil.which("idf.py")
        if not idf_py:
            logger.error("idf.py not in PATH. Activate ESP-IDF environment.")
            return False

        logger.info(f"ESP-IDF found at: {idf_path}")
        return True

    def set_target(self) -> bool:
        """Set the target chip (ESP32-S3)."""
        try:
            result = subprocess.run(
                ["idf.py", "set-target", self.target],
                cwd=self.firmware_dir,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                logger.error(f"Failed to set target: {result.stderr}")
                return False
            logger.info(f"Target set to {self.target}")
            return True
        except subprocess.TimeoutExpired:
            logger.exception("Timeout setting target")
            return False
        except Exception:
            logger.exception("Error setting target")
            return False

    def build(self, clean: bool = False, verbose: bool = False) -> bool:
        """Build the firmware."""
        if clean:
            logger.info("Cleaning previous build...")
            if self.build_dir.exists():
                shutil.rmtree(self.build_dir)

        cmd = ["idf.py", "build"]
        if verbose:
            cmd.append("-v")

        logger.info("Building firmware...")
        start = time.time()

        try:
            result = subprocess.run(
                cmd,
                cwd=self.firmware_dir,
                capture_output=not verbose,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            logger.exception("Build timeout (5 min)")
            return False

        elapsed = time.time() - start

        if result.returncode != 0:
            logger.error(f"Build failed:\n{result.stderr}")
            return False

        logger.info(f"Build successful in {elapsed:.1f}s")

        # Verify binary exists
        if not self.bin_path.exists():
            logger.error(f"Binary not found at {self.bin_path}")
            return False

        # Get binary size
        bin_size = self.bin_path.stat().st_size
        logger.info(f"Firmware binary: {self.bin_path} ({bin_size / 1024:.1f} KB)")

        # Generate metadata
        self._generate_metadata(bin_size, elapsed)

        return True

    def _generate_metadata(self, bin_size: int, build_time: float) -> None:
        """Generate firmware metadata JSON."""
        import hashlib

        with open(self.bin_path, "rb") as f:
            sha256 = hashlib.sha256(f.read()).hexdigest()

        metadata = {
            "firmware": "synapse_tier0",
            "version": "0.1.0",
            "target": self.target,
            "bin_path": str(self.bin_path),
            "bin_size_bytes": bin_size,
            "bin_size_kb": round(bin_size / 1024, 1),
            "sha256": sha256,
            "build_time_seconds": round(build_time, 1),
            "build_timestamp": time.time(),
            "features": {
                "ecg_ad8232": True,
                "ppg_max30102": True,
                "imu_icm20948": True,
                "ble_lsl_bridge": True,
                "triage_int8": True,
                "ppg_sqi_100hz": True,
                "wired_sync_gpio": 27,
                "power_monitor": True,
            },
            "sampling_rates": {
                "ecg_hz": 500,
                "ppg_hz": 64,
                "imu_hz": 100,
            },
        }

        with open(self.metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)

        logger.info(f"Metadata written to {self.metadata_path}")

    def flash(
        self,
        port: str,
        baud: int = 921600,
        verify: bool = True,
        erase_flash: bool = False,
    ) -> bool:
        """Flash firmware to device."""
        if not self.bin_path.exists():
            logger.error(f"Binary not found: {self.bin_path}. Run build first.")
            return False

        cmd = ["idf.py", "-p", port, "-b", str(baud), "flash"]
        if erase_flash:
            cmd.insert(-1, "erase-flash")

        logger.info(f"Flashing to {port} at {baud} baud...")

        try:
            result = subprocess.run(
                cmd,
                cwd=self.firmware_dir,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            logger.exception("Flash timeout (2 min)")
            return False

        if result.returncode != 0:
            logger.error(f"Flash failed:\n{result.stderr}")
            return False

        logger.info("Flash successful")

        if verify:
            logger.info("Verifying flash...")
            verify_cmd = ["idf.py", "-p", port, "-b", str(baud), "flash", "verify"]
            try:
                result = subprocess.run(
                    verify_cmd,
                    cwd=self.firmware_dir,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if result.returncode != 0:
                    logger.warning(f"Verification failed: {result.stderr}")
                else:
                    logger.info("Verification passed")
            except subprocess.TimeoutExpired:
                logger.warning("Verification timeout")

        return True

    def monitor(self, port: str, baud: int = 921600, duration: int | None = None) -> bool:
        """Open serial monitor (non-blocking with optional duration)."""
        cmd = ["idf.py", "-p", port, "-b", str(baud), "monitor"]

        logger.info(f"Starting monitor on {port}...")

        try:
            if duration:
                # Run with timeout
                proc = subprocess.Popen(cmd, cwd=self.firmware_dir)
                time.sleep(duration)
                proc.terminate()
                proc.wait(timeout=5)
            else:
                # Blocking - user exits with Ctrl+]
                subprocess.run(cmd, cwd=self.firmware_dir)
        except KeyboardInterrupt:
            logger.info("Monitor stopped by user")
        except Exception:
            logger.exception("Monitor error")
            return False

        return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build and flash SYNAPSE-24 ESP32-S3 Tier 0 firmware"
    )
    parser.add_argument(
        "--firmware-dir",
        type=Path,
        default=Path("firmware/esp32_tier0"),
        help="Path to ESP-IDF project directory",
    )
    parser.add_argument("--target", default="esp32s3", help="Target chip")
    parser.add_argument("--port", default="COM3", help="Serial port for flash/monitor")
    parser.add_argument("--baud", type=int, default=921600, help="Baud rate")
    parser.add_argument("--clean", action="store_true", help="Clean build")
    parser.add_argument("--verbose", action="store_true", help="Verbose build output")
    parser.add_argument("--erase-flash", action="store_true", help="Erase flash before programming")
    parser.add_argument("--no-verify", action="store_true", help="Skip flash verification")
    parser.add_argument("--monitor", action="store_true", help="Open monitor after flash")
    parser.add_argument("--monitor-duration", type=int, help="Monitor duration in seconds")
    parser.add_argument("--build-only", action="store_true", help="Only build, don't flash")
    parser.add_argument("--flash-only", action="store_true", help="Only flash, don't build")

    args = parser.parse_args()

    builder = FirmwareBuilder(args.firmware_dir, target=args.target)

    # Check ESP-IDF
    if not builder.check_esp_idf():
        return 1

    # Build
    if not args.flash_only:
        if not builder.set_target():
            return 1
        if not builder.build(clean=args.clean, verbose=args.verbose):
            return 1

    # Flash
    if not args.build_only:
        if not builder.flash(
            port=args.port,
            baud=args.baud,
            verify=not args.no_verify,
            erase_flash=args.erase_flash,
        ):
            return 1

    # Monitor
    if args.monitor:
        builder.monitor(port=args.port, baud=args.baud, duration=args.monitor_duration)

    logger.info("Done!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
