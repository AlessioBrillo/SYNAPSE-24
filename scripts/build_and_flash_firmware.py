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
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _idf_cmd() -> list[str]:
    """Command prefix that reliably invokes idf.py on every host.

    On Windows the idf-exe trampoline (``idf.py.EXE``) cannot be launched via
    CreateProcess from a Python subprocess (WinError 193: "%1 is not a valid
    Win32 application"), even though shells can run it. The canonical fix is
    to run ``$IDF_PATH/tools/idf.py`` with the ESP-IDF Python interpreter
    (``IDF_PYTHON_ENV_PATH``, set by export.ps1). Fall back to the plain
    ``idf.py`` name on Unix, or when the environment is missing.
    """
    if platform.system() == "Windows":
        env_python = os.environ.get("IDF_PYTHON_ENV_PATH")
        idf_path = os.environ.get("IDF_PATH")
        if env_python and idf_path:
            python = Path(env_python) / "Scripts" / "python.exe"
            idf_py_script = Path(idf_path) / "tools" / "idf.py"
            if python.is_file() and idf_py_script.is_file():
                return [str(python), str(idf_py_script)]
    return ["idf.py"]


class FirmwareBuilder:
    """Manages ESP-IDF build and flash for SYNAPSE-24 firmware."""

    # Build variant -> sdkconfig.defaults suffix (None = base defaults only).
    # "band-core" is the smoke-kit rig and needs its profile appended AFTER
    # the base file, hence the explicit mapping (the file is
    # sdkconfig.defaults.core, NOT sdkconfig.defaults.band-core).
    VARIANT_SDKCONFIG_SUFFIX = {
        "band-core": "core",
        "band-full": None,
    }

    def __init__(
        self,
        firmware_dir: Path,
        build_dir: Path | None = None,
        target: str = "esp32s3",
        build_variant: str = "band-core",
    ) -> None:
        self.firmware_dir = Path(firmware_dir).resolve()
        self.build_dir = Path(build_dir).resolve() if build_dir else self.firmware_dir / "build"
        self.target = target
        # "band-core" builds the smoke-kit rig (ECG+PPG+IMU); "band-full" adds
        # GPS, temperature, FIT, provisioning and OTA. See
        # docs/hardware/ESP-IDF_SETUP.md.
        self.build_variant = build_variant
        self.bin_path = self.build_dir / "synapse-band.bin"
        self.metadata_path = self.build_dir / "firmware_metadata.json"

    def check_esp_idf(self) -> bool:
        """Verify ESP-IDF environment is set up."""
        idf_path = os.environ.get("IDF_PATH")
        if not idf_path:
            logger.error("IDF_PATH not set. Source export.sh/export.ps1 from ESP-IDF installation.")
            return False

        idf_cmd = _idf_cmd()
        if idf_cmd != ["idf.py"]:
            logger.info(f"idf.py: {' '.join(idf_cmd)}")
        else:
            idf_py = shutil.which("idf.py")
            if not idf_py:
                logger.error("idf.py not in PATH. Activate ESP-IDF environment.")
                return False
        logger.info(f"ESP-IDF found at: {idf_path}")
        return True

    def _variant_env(self) -> dict[str, str]:
        """Environment carrying the variant's SDKCONFIG_DEFAULTS.

        The variant selects sdkconfig.defaults.<suffix> in addition to the
        base defaults. Passing SDKCONFIG_DEFAULTS replaces ESP-IDF's default
        list, so both files must be named explicitly. Every idf.py invocation
        that can (re)generate sdkconfig must receive this environment —
        set-target included, or its regenerated sdkconfig would later win.
        """
        variant_suffix = self.VARIANT_SDKCONFIG_SUFFIX.get(self.build_variant, "")
        if self.build_variant not in self.VARIANT_SDKCONFIG_SUFFIX:
            raise ValueError(
                f"unknown build variant {self.build_variant!r}; "
                f"expected one of {sorted(self.VARIANT_SDKCONFIG_SUFFIX)}"
            )
        env = os.environ.copy()
        if variant_suffix:
            variant_file = self.firmware_dir / f"sdkconfig.defaults.{variant_suffix}"
            if not variant_file.is_file():
                raise FileNotFoundError(f"variant defaults not found: {variant_file}")
            env["SDKCONFIG_DEFAULTS"] = f"sdkconfig.defaults.{variant_suffix};sdkconfig.defaults"
            logger.info(
                "Build variant: %s (%s)", self.build_variant, f"sdkconfig.defaults.{variant_suffix}"
            )
        else:
            env.pop("SDKCONFIG_DEFAULTS", None)
            logger.info("Build variant: %s (base sdkconfig.defaults only)", self.build_variant)
        return env

    def set_target(self) -> bool:
        """Set the target chip (ESP32-S3)."""
        # The sdkconfig file lives OUTSIDE build/ and survives --clean. Its
        # values take precedence over every sdkconfig.defaults file, so a
        # stale one (e.g. from a band-full build) silently defeats the
        # variant profile. Delete it first: the defaults files are the
        # authoritative source and idf.py regenerates the file from them.
        stale_sdkconfig = self.firmware_dir / "sdkconfig"
        if stale_sdkconfig.is_file():
            stale_sdkconfig.unlink()
            logger.info("Deleted stale sdkconfig (regenerated from variant defaults)")
        try:
            result = subprocess.run(
                [*_idf_cmd(), "set-target", self.target],
                cwd=self.firmware_dir,
                capture_output=True,
                text=True,
                timeout=60,
                env=self._variant_env(),
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

        cmd = [*_idf_cmd(), "build"]
        if verbose:
            cmd.append("-v")

        env = self._variant_env()

        logger.info("Building firmware...")
        start = time.time()

        try:
            result = subprocess.run(
                cmd,
                cwd=self.firmware_dir,
                capture_output=not verbose,
                text=True,
                timeout=600,
                env=env,
            )
        except subprocess.TimeoutExpired:
            logger.exception("Build timeout (10 min)")
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
            "firmware": "synapse_band",
            "variant": self.build_variant,
            "version": "1.0.0",
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
                "research_service_raw": True,
                "feature_stream_consumer": True,
                "triage_int8": True,
                "power_monitor": True,
                "gps_max_m10s": self.build_variant == "band-full",
                "temperature_tmp117": self.build_variant == "band-full",
                "fit_storage": self.build_variant == "band-full",
                "provisioning": self.build_variant == "band-full",
                "ota": self.build_variant == "band-full",
            },
            # Must match config/hardware.yaml sampling_rate.* (tools/check_contract.py)
            "sampling_rates": {
                "ecg_hz": 500,
                "ppg_hz": 50,
                "imu_hz": 50,
            },
            "i2c": {"sda_gpio": 8, "scl_gpio": 9},
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

        cmd = [*_idf_cmd(), "-p", port, "-b", str(baud), "flash"]
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
            verify_cmd = [*_idf_cmd(), "-p", port, "-b", str(baud), "flash", "verify"]
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
        cmd = [*_idf_cmd(), "-p", port, "-b", str(baud), "monitor"]

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
        description="Build and flash SYNAPSE-24 Synapse Band firmware (ESP32-S3)"
    )
    parser.add_argument(
        "--firmware-dir",
        type=Path,
        default=Path("firmware/esp32_band"),
        help="Path to ESP-IDF project directory",
    )
    parser.add_argument(
        "--variant",
        default="band-core",
        choices=["band-core", "band-full"],
        help="Build variant: band-core (smoke kit) or band-full (product)",
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

    builder = FirmwareBuilder(args.firmware_dir, target=args.target, build_variant=args.variant)

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
