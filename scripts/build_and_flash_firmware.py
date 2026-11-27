#!/usr/bin/env python3
"""Build and flash the ESP32-S3 band firmware for SYNAPSE-24 bringup.

The Triaged INT8 model is currently OFF in every profile (the interpreter
arena bug is open); this script builds and flashes the raw research path,
not an on-device classifier. Flash settings come from the bringup config;
flash mode/size/frequency belong to sdkconfig and are deliberately absent.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

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

    # Build variant -> ordered SDKCONFIG_DEFAULTS files. ESP-IDF applies them
    # left to right, so later files override earlier ones. Every invocation
    # that can regenerate sdkconfig must receive the same list.
    VARIANT_DEFAULTS = {
        "band-core": ["sdkconfig.defaults.core", "sdkconfig.defaults"],
        "band-bench": [
            "sdkconfig.defaults.core",
            "sdkconfig.defaults.bench",
            "sdkconfig.defaults",
        ],
        "band-full": [],
        "band-product": ["sdkconfig.defaults.product", "sdkconfig.defaults"],
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

        Passing SDKCONFIG_DEFAULTS replaces ESP-IDF's default list, so every
        file must be named explicitly, in order. Every idf.py invocation that
        can (re)generate sdkconfig must receive this environment — set-target
        included, or its regenerated sdkconfig would later win.
        """
        if self.build_variant not in self.VARIANT_DEFAULTS:
            raise ValueError(
                f"unknown build variant {self.build_variant!r}; "
                f"expected one of {sorted(self.VARIANT_DEFAULTS)}"
            )
        files = self.VARIANT_DEFAULTS[self.build_variant]
        for name in files:
            if not (self.firmware_dir / name).is_file():
                raise FileNotFoundError(f"variant defaults not found: {name}")
        env = os.environ.copy()
        if files:
            env["SDKCONFIG_DEFAULTS"] = ";".join(files)
            logger.info("Build variant: %s (%s)", self.build_variant, ", ".join(files))
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
                check=False,
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
                check=False,
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

    def _sdkconfig_flags(self) -> dict[str, str]:
        """The options ESP-IDF really wrote into sdkconfig after this build."""
        path = self.firmware_dir / "sdkconfig"
        flags: dict[str, str] = {}
        if not path.is_file():
            return flags
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if line.startswith("CONFIG_") and "=" in line:
                name, value = line.split("=", 1)
                flags[name.strip()] = value.strip()
            elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
                flags[line[len("# ") : -len(" is not set")].strip()] = "n"
        return flags

    def _generate_metadata(self, bin_size: int, build_time: float) -> None:
        """Generate firmware metadata JSON from the hardware contract.

        Rates and pins used to be hardcoded here while hardware.yaml claimed
        to be the single source of truth: a build could advertise 500/50/50 Hz
        with SDA 8 while the manifest said something else. Read the manifest.
        """
        import hashlib

        with open(self.bin_path, "rb") as f:
            sha256 = hashlib.sha256(f.read()).hexdigest()

        hw = yaml.safe_load((ROOT / "config" / "hardware.yaml").read_text(encoding="utf-8"))
        pod = next(
            p
            for p in hw.get("pods", [])
            if p.get("status", "active") == "active" and p.get("is_hub")
        )
        rates = pod.get("sampling_rate", {})
        i2c = pod.get("metadata", {}).get("i2c", {})
        # Derive the feature list from the sdkconfig that was actually built,
        # not from the variant name. band-product compiles the research service
        # but never registers it; claiming raw streaming there was a false
        # metadata row used by device binding.
        flags = self._sdkconfig_flags()
        enabled = lambda name: flags.get(name) == "y"  # noqa: E731
        raw_registered = enabled("CONFIG_SYNAPSE_FEATURE_RESEARCH_SERVICE") and not enabled(
            "CONFIG_SYNAPSE_PRODUCT_MODE"
        )

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
                "research_service_raw": raw_registered,
                "research_service_registered": raw_registered,
                "feature_stream_consumer": True,
                "triage_int8": enabled("CONFIG_SYNAPSE_FEATURE_EDGE_AI"),
                "power_monitor": enabled("CONFIG_SYNAPSE_FEATURE_POWER_MONITOR"),
                "gps_max_m10s": enabled("CONFIG_SYNAPSE_FEATURE_GPS"),
                "temperature_tmp117": enabled("CONFIG_SYNAPSE_FEATURE_TEMPERATURE"),
                "fit_storage": enabled("CONFIG_SYNAPSE_FEATURE_FIT_STORAGE"),
                "provisioning": enabled("CONFIG_SYNAPSE_FEATURE_PROVISIONING"),
                "ota": enabled("CONFIG_SYNAPSE_FEATURE_OTA"),
                "factory_mode": enabled("CONFIG_SYNAPSE_FACTORY_MODE"),
            },
            "sampling_rates": {
                "ecg_hz": rates["ecg"],
                "ppg_hz": rates["ppg"],
                "imu_hz": rates["imu"],
            },
            "i2c": {"sda_gpio": i2c["sda_gpio"], "scl_gpio": i2c["scl_gpio"]},
            "power_profile": ("kit" if enabled("CONFIG_SYNAPSE_POWER_PROFILE_KIT") else "system"),
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
                check=False,
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
                    check=False,
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
                subprocess.run(cmd, cwd=self.firmware_dir, check=False)
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
        default=None,
        choices=sorted(FirmwareBuilder.VARIANT_DEFAULTS),
        help="Build variant (default: bringup config, normally band-core)",
    )
    parser.add_argument("--target", default="esp32s3", help="Target chip")
    parser.add_argument("--port", help="Serial port (default: bringup config / COM3)")
    parser.add_argument("--baud", type=int, help="Baud rate (default: bringup config)")
    parser.add_argument("--clean", action="store_true", help="Clean build")
    parser.add_argument("--verbose", action="store_true", help="Verbose build output")
    parser.add_argument("--erase-flash", action="store_true", help="Erase flash before programming")
    parser.add_argument("--no-verify", action="store_true", help="Skip flash verification")
    parser.add_argument("--monitor", action="store_true", help="Open monitor after flash")
    parser.add_argument("--monitor-duration", type=int, help="Monitor duration in seconds")
    parser.add_argument("--build-only", action="store_true", help="Only build, don't flash")
    parser.add_argument("--flash-only", action="store_true", help="Only flash, don't build")

    args = parser.parse_args()

    # The bringup config owns flash settings; flash mode/size/frequency stay
    # in sdkconfig (they are build properties, not per-session choices).
    bringup = yaml.safe_load(
        (ROOT / "config" / "hardware_bringup.yaml").read_text(encoding="utf-8")
    )["bringup"]["firmware"]
    variant = args.variant or bringup.get("build_variant", "band-core")
    port = args.port or os.environ.get("SYNAPSE_FOREARM_SERIAL") or bringup.get("port") or "COM3"
    port = re.sub(r"\$\{[^}]*:-?([^}]*)\}", r"\1", str(port)) or "COM3"
    baud = args.baud or int(bringup.get("baud", 921600))
    verify = bool(bringup.get("verify_after_flash", True)) and not args.no_verify

    builder = FirmwareBuilder(args.firmware_dir, target=args.target, build_variant=variant)

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
            baud=baud,
            verify=verify,
            erase_flash=args.erase_flash,
        ):
            return 1

    # Monitor
    if args.monitor:
        builder.monitor(port=port, baud=baud, duration=args.monitor_duration)

    logger.info("Done!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
