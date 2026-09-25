#!/usr/bin/env python3
"""SYNAPSE-24 Hardware Bringup: BLE → LSL Bridge with Live Validation.

Single-pod Tier 0 validation (forearm_hub with ESP32-S3).
Streams live ECG, PPG, IMU via BLE to LSL outlets.
Runs MultiPodClockSync with sync markers.
Executes Phase 1 entry gate validation on live data.

Usage:
    uv run python scripts/hardware_bringup.py --config config/hardware_bringup.yaml [--flash] [--duration 60]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import yaml

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from synapse24.acquisition.clock_sync import MultiPodClockSync, SyncConfig, TierSyncBudget
from synapse24.acquisition.coordinator import SensorPodCoordinator
from synapse24.acquisition.immobility import ImmobilityDetector
from synapse24.acquisition.night_window import NightWindowScheduler, SleepWindowConfig
from synapse24.acquisition.power_budget import PowerBudgetManager
from synapse24.acquisition.state_machine import (
    AcquisitionController,
    MotionGateConfig,
    Tier,
)
from synapse24.utils import verify_xdf_roundtrip

try:
    from bleak import BleakClient, BleakScanner
    from bleak.backends.characteristic import BleakGATTCharacteristic
    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData
except ImportError:
    BleakClient = None
    BleakScanner = None
    print("bleak not installed. Install with: uv add bleak", file=sys.stderr)

try:
    import pylsl
except ImportError:
    pylsl = None
    print("pylsl not installed. Install with: uv add pylsl", file=sys.stderr)


@dataclass
class SensorSample:
    """Single sensor sample with timestamp."""

    timestamp: float
    ecg: np.ndarray | None = None  # shape (1,)
    ppg: np.ndarray | None = None  # shape (2,) [red, ir]
    acc: np.ndarray | None = None  # shape (3,)
    gyro: np.ndarray | None = None  # shape (3,)
    mag: np.ndarray | None = None  # shape (3,)


@dataclass
class LSLStreamManager:
    """Manages LSL outlets for all sensor streams."""

    outlets: dict[str, Any] = field(default_factory=dict)
    stream_configs: dict[str, dict] = field(default_factory=dict)

    def create_outlets(self, stream_configs: list[dict]) -> None:
        """Create LSL outlets from configuration."""
        if pylsl is None:
            raise RuntimeError("pylsl not available")

        for cfg in stream_configs:
            info = pylsl.StreamInfo(
                name=cfg["name"],
                type=cfg["type"],
                channel_count=cfg["channel_count"],
                nominal_srate=cfg["sampling_rate"],
                channel_format=cfg["channel_format"],
                source_id=cfg["source_id"],
            )
            # Add tier metadata
            info.desc().append_child_value("tier", str(cfg.get("tier", 0)))

            outlet = pylsl.StreamOutlet(info, chunk_size=cfg.get("chunk_size", 16))
            self.outlets[cfg["name"]] = outlet
            self.stream_configs[cfg["name"]] = cfg

    def push_sample(self, name: str, sample: np.ndarray, timestamp: float) -> None:
        """Push a sample to an LSL outlet."""
        if name in self.outlets:
            self.outlets[name].push_sample(sample.astype(np.float32), timestamp)

    def close(self) -> None:
        """Close all outlets."""
        for outlet in self.outlets.values():
            try:
                outlet.__del__()
            except Exception:
                pass


class BLEDataParser:
    """Parses BLE notifications from ESP32-S3 firmware."""

    # Expected packet formats (little-endian):
    # ECG:  1 int16 = 2 bytes
    # PPG:  2 int16 = 4 bytes (red, ir)
    # IMU:  9 int16 = 18 bytes (ax, ay, az, gx, gy, gz, mx, my, mz)
    # All scaled by firmware to physical units

    ECG_FMT = "<h"  # 1 int16
    PPG_FMT = "<hh"  # 2 int16
    IMU_FMT = "<hhhhhhhhh"  # 9 int16

    ECG_SIZE = struct.calcsize(ECG_FMT)
    PPG_SIZE = struct.calcsize(PPG_FMT)
    IMU_SIZE = struct.calcsize(IMU_FMT)

    # Scaling factors (must match firmware)
    ECG_SCALE = 1.0 / 1100.0 * 3300.0 / 4096.0  # V -> mV (AD8232 gain 1100, 12-bit ADC, 3.3V ref)
    PPG_SCALE = 1.0  # Already in nA from MAX30102
    ACC_SCALE = 8.0 / 32768.0  # ±8g range
    GYRO_SCALE = 1000.0 / 32768.0  # ±1000 dps
    MAG_SCALE = 4912.0 / 32768.0  # ±4912 uT (ICM-20948 mag)

    def parse_ecg(self, data: bytes) -> np.ndarray:
        """Parse ECG notification -> shape (1,) in mV."""
        if len(data) != self.ECG_SIZE:
            return np.array([0.0], dtype=np.float32)
        val = struct.unpack(self.ECG_FMT, data)[0]
        return np.array([val * self.ECG_SCALE], dtype=np.float32)

    def parse_ppg(self, data: bytes) -> np.ndarray:
        """Parse PPG notification -> shape (2,) [red, ir] in nA."""
        if len(data) != self.PPG_SIZE:
            return np.array([0.0, 0.0], dtype=np.float32)
        red, ir = struct.unpack(self.PPG_FMT, data)
        return np.array([red * self.PPG_SCALE, ir * self.PPG_SCALE], dtype=np.float32)

    def parse_imu(self, data: bytes) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Parse IMU notification -> (acc, gyro, mag) each shape (3,)."""
        if len(data) != self.IMU_SIZE:
            return (
                np.zeros(3, dtype=np.float32),
                np.zeros(3, dtype=np.float32),
                np.zeros(3, dtype=np.float32),
            )
        vals = struct.unpack(self.IMU_FMT, data)
        acc = np.array(vals[0:3], dtype=np.float32) * self.ACC_SCALE
        gyro = np.array(vals[3:6], dtype=np.float32) * self.GYRO_SCALE
        mag = np.array(vals[6:9], dtype=np.float32) * self.MAG_SCALE
        return acc, gyro, mag


class HardwareBringup:
    """Main hardware bringup orchestration."""

    def __init__(self, config_path: Path):
        self.config = self._load_config(config_path)
        self.logger = self._setup_logging()
        self.parser = BLEDataParser()
        self.lsl_manager = LSLStreamManager()
        self.clock_sync: MultiPodClockSync | None = None
        self.controller: AcquisitionController | None = None
        self.client: BleakClient | None = None
        self.device: BLEDevice | None = None
        self.running = False
        self.start_time = 0.0
        self.sample_counts = {"ecg": 0, "ppg": 0, "imu": 0}
        self.stream_buffers: dict[str, list] = {}
        self.stream_timestamps: dict[str, list] = {}
        self._setup_signal_handlers()

    def _load_config(self, config_path: Path) -> dict:
        """Load and validate configuration."""
        with open(config_path) as f:
            config = yaml.safe_load(f)

        # Expand environment variables
        return self._expand_env_vars(config)

    def _expand_env_vars(self, obj: Any) -> Any:
        """Recursively expand ${VAR} or ${VAR:-default} in strings."""
        if isinstance(obj, str):
            # Handle ${VAR:-default} syntax
            import re

            def replace_var(match):
                var_expr = match.group(1)
                if ":-" in var_expr:
                    var, default = var_expr.split(":-", 1)
                    return os.environ.get(var, default)
                return os.environ.get(var_expr, match.group(0))

            return re.sub(r"\$\{([^}]+)\}", replace_var, obj)
        if isinstance(obj, dict):
            return {k: self._expand_env_vars(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._expand_env_vars(v) for v in obj]
        return obj

    def _setup_logging(self) -> logging.Logger:
        """Configure logging."""
        log_cfg = self.config["bringup"]["logging"]
        logger = logging.getLogger("hardware_bringup")
        logger.setLevel(getattr(logging, log_cfg["level"]))

        # Console handler
        if log_cfg["console"]:
            ch = logging.StreamHandler()
            ch.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s"))
            logger.addHandler(ch)

        # File handler
        log_file = log_cfg["file"].format(timestamp=time.strftime("%Y%m%d_%H%M%S"))
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file)
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"))
        logger.addHandler(fh)

        return logger

    def _setup_signal_handlers(self) -> None:
        """Handle graceful shutdown."""

        def signal_handler(signum, frame):
            self.logger.info(f"Received signal {signum}, shutting down...")
            self.running = False

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    async def flash_firmware(self) -> bool:
        """Flash firmware to ESP32-S3 using esptool."""
        fw_cfg = self.config["bringup"]["firmware"]
        firmware_path = Path(fw_cfg["path"])

        if not firmware_path.exists():
            self.logger.error(f"Firmware not found: {firmware_path}")
            self.logger.info("Build firmware with: cd firmware && idf.py build")
            return False

        self.logger.info(f"Flashing firmware: {firmware_path}")
        self.logger.info(f"Port: {fw_cfg['port']}, Baud: {fw_cfg['baud']}")

        try:
            import subprocess

            cmd = [
                "esptool.py",
                "--port",
                fw_cfg["port"],
                "--baud",
                str(fw_cfg["baud"]),
                "--chip",
                "esp32s3",
                "write_flash",
                "--flash_mode",
                fw_cfg["flash_mode"],
                "--flash_freq",
                fw_cfg["flash_freq"],
                "--flash_size",
                fw_cfg["flash_size"],
                "0x0",
                str(firmware_path),
            ]
            if fw_cfg["verify_after_flash"]:
                cmd.extend(["--verify"])

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                self.logger.info("Firmware flashed successfully")
                return True
            self.logger.error(f"Flash failed: {result.stderr}")
            return False
        except subprocess.TimeoutExpired:
            self.logger.exception("Flash timeout")
            return False
        except Exception:
            self.logger.exception("Flash error")
            return False

    async def discover_device(self) -> BLEDevice | None:
        """Discover SYNAPSE BLE device."""
        ble_cfg = self.config["bringup"]["ble"]
        address = ble_cfg["address"]

        if address != "auto":
            self.logger.info(f"Connecting to known address: {address}")
            device = await BleakScanner.find_device_by_address(
                address, timeout=ble_cfg["scan_timeout_s"]
            )
            return device

        self.logger.info("Scanning for SYNAPSE device...")
        devices = await BleakScanner.discover(timeout=ble_cfg["scan_timeout_s"])
        for d in devices:
            # Look for SYNAPSE in name or service UUID
            if d.name and "SYNAPSE" in d.name.upper():
                self.logger.info(f"Found SYNAPSE device: {d.name} ({d.address})")
                return d
            # Could also check advertisement data for service UUID

        self.logger.warning("No SYNAPSE device found in scan")
        return None

    def _create_lsl_outlets(self) -> None:
        """Create LSL outlets from config."""
        streams = self.config["bringup"]["lsl"]["streams"]
        self.lsl_manager.create_outlets(streams)

        # Initialize buffers
        for stream in streams:
            self.stream_buffers[stream["name"]] = []
            self.stream_timestamps[stream["name"]] = []

        self.logger.info(f"Created {len(streams)} LSL outlets")

    def _setup_clock_sync(self) -> None:
        """Initialize MultiPodClockSync for live synchronization."""
        sync_cfg = self.config["bringup"]["sync"]
        tier_budget = TierSyncBudget(
            tier0_max_residual_drift_ms=sync_cfg["tier_budgets"]["T0"]["max_residual_drift_ms"],
            tier1_max_residual_drift_ms=sync_cfg["tier_budgets"]["T1"]["max_residual_drift_ms"],
            tier0_sync_interval_s=sync_cfg["tier_budgets"]["T0"]["sync_interval_s"],
            tier1_sync_interval_s=sync_cfg["tier_budgets"]["T1"]["sync_interval_s"],
        )
        config = SyncConfig(
            tier_budget=tier_budget,
            acc_corr_window_s=sync_cfg["acc_correlation_window_s"],
            min_acc_correlation=sync_cfg["min_acc_correlation"],
            acc_sampling_rates={"forearm_hub": 100},
        )
        self.clock_sync = MultiPodClockSync(config)
        self.clock_sync.register_pod("forearm_hub", 100)

        # Set up LSL marker outlet for sync
        marker_info = pylsl.StreamInfo(
            name="SYNAPSE_MARKERS",
            type="Markers",
            channel_count=1,
            nominal_srate=0,
            channel_format="string",
            source_id="synapse_markers_001",
        )
        marker_outlet = pylsl.StreamOutlet(marker_info)
        self.clock_sync.marker_manager.set_marker_stream(marker_outlet)

    def _setup_acquisition_controller(self) -> None:
        """Initialize AcquisitionController with live components."""
        bringup_cfg = self.config["bringup"]
        pb_cfg = bringup_cfg["power_budget"]
        mg_cfg = bringup_cfg["motion_gate"]

        power_budget = PowerBudgetManager(
            hub_battery_mah=pb_cfg["hub_battery_mah"],
            target_lifetime_h=pb_cfg["target_lifetime_h"],
            tier0_avg_mw=pb_cfg["tier_profiles"]["T0"]["avg_mw"],
            tier1_avg_mw=pb_cfg["tier_profiles"]["T1"]["avg_mw"],
            tier1_max_h=pb_cfg["tier_profiles"]["T1"]["max_duration_h"],
            tier2_avg_mw=pb_cfg["tier_profiles"]["T2"]["avg_mw"],
            tier2_max_burst_min=pb_cfg["tier_profiles"]["T2"]["max_duration_h"] * 60,
            reserve_mah=pb_cfg["reserve_mah"],
        )

        immobility_detector = ImmobilityDetector(
            accel_sampling_rate=100,
            window_duration_s=bringup_cfg["immobility"]["window_s"],
            magnitude_threshold=bringup_cfg["immobility"]["accel_threshold_g"],
            min_immobility_min=bringup_cfg["immobility"]["required_windows"]
            * bringup_cfg["immobility"]["window_s"]
            / 60,
        )

        night_scheduler = NightWindowScheduler(
            SleepWindowConfig(
                start_hour=bringup_cfg["night_window"]["sleep_start_hour"],
                end_hour=bringup_cfg["night_window"]["sleep_end_hour"],
                timezone=bringup_cfg["night_window"]["timezone"],
            )
        )

        pod_coordinator = SensorPodCoordinator()

        self.controller = AcquisitionController(
            immobility_detector=immobility_detector,
            night_scheduler=night_scheduler,
            power_budget=power_budget,
            pod_coordinator=pod_coordinator,
            motion_gate=MotionGateConfig(
                sqi_min=mg_cfg["sqi_min"],
                map_max=mg_cfg["map_max"],
                required_consecutive_clean=mg_cfg["required_consecutive_clean"],
            ),
        )

    def _notification_handler(
        self, characteristic: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle BLE notifications from ESP32-S3."""
        timestamp = time.time()
        uuid_str = str(characteristic.uuid).lower()

        ble_chars = self.config["bringup"]["ble"]["characteristics"]

        try:
            if uuid_str == ble_chars["ecg"].lower():
                ecg = self.parser.parse_ecg(bytes(data))
                self._push_to_lsl("SYNAPSE_ECG_T0", ecg, timestamp)
                self.sample_counts["ecg"] += 1

            elif uuid_str == ble_chars["ppg"].lower():
                ppg = self.parser.parse_ppg(bytes(data))
                self._push_to_lsl("SYNAPSE_PPG_T0", ppg, timestamp)
                self.sample_counts["ppg"] += 1

            elif uuid_str == ble_chars["imu"].lower():
                acc, gyro, mag = self.parser.parse_imu(bytes(data))
                self._push_to_lsl("SYNAPSE_ACC_T0", acc, timestamp)
                self._push_to_lsl("SYNAPSE_GYRO_T0", gyro, timestamp)
                self._push_to_lsl("SYNAPSE_MAG_T0", mag, timestamp)
                self.sample_counts["imu"] += 1

                # Update clock sync with ACC
                if self.clock_sync:
                    acc_mag = float(np.linalg.norm(acc))
                    self.clock_sync.add_hub_acc(acc_mag, timestamp)
                    self.clock_sync.add_pod_acc("forearm_hub", acc_mag, timestamp)

                # Update acquisition controller
                if self.controller:
                    self.controller.update_imu(
                        accel_magnitude=float(np.linalg.norm(acc)),
                        timestamp=timestamp,
                    )

        except Exception:
            self.logger.exception("Error parsing notification")

    def _push_to_lsl(self, stream_name: str, sample: np.ndarray, timestamp: float) -> None:
        """Push sample to LSL and buffer for XDF."""
        self.lsl_manager.push_sample(stream_name, sample, timestamp)

        if stream_name in self.stream_buffers:
            self.stream_buffers[stream_name].append(sample.copy())
            self.stream_timestamps[stream_name].append(timestamp)

    async def _subscribe_characteristics(self) -> None:
        """Subscribe to all sensor characteristics."""
        ble_chars = self.config["bringup"]["ble"]["characteristics"]

        for name, char_uuid in ble_chars.items():
            self.logger.info(f"Subscribing to {name}: {char_uuid}")
            await self.client.start_notify(char_uuid, self._notification_handler)

    async def _run_validation_loop(self, duration_s: float) -> dict:  # noqa: PLR0915
        """Run the Phase 1 validation loop on live data."""
        val_cfg = self.config["bringup"]["validation"]
        tier_model_cfg = self.config["bringup"]["triage_model"]

        self.logger.info(f"Starting validation loop for {duration_s}s...")
        self.start_time = time.time()
        self.running = True

        # State for tier promotion validation
        promotion_occurred = False
        demotion_occurred = False
        immobility_start = None
        movement_detected = False
        last_controller_tick = 0.0
        last_sync_update = 0.0
        last_sync_broadcast = 0.0
        last_log = 0.0

        # Load triage model if available
        triage_interpreter = None
        try:
            import tflite_runtime.interpreter as tflite

            model_path = Path(tier_model_cfg["path"])
            if model_path.exists():
                triage_interpreter = tflite.Interpreter(model_path=str(model_path))
                triage_interpreter.allocate_tensors()
                self.logger.info(f"Loaded triage model: {model_path}")
        except Exception as e:
            self.logger.warning(f"Could not load triage model: {e}")

        imu_window = []  # Rolling window for triage inference

        while self.running and (time.time() - self.start_time) < duration_s:
            now = time.time()
            elapsed = now - self.start_time

            # Periodic controller tick (every 100ms)
            if elapsed - last_controller_tick >= 0.1:
                self.controller.tick(elapsed)
                last_controller_tick = elapsed

            # Periodic clock sync update (every 500ms)
            if elapsed - last_sync_update >= 0.5:
                if self.clock_sync:
                    self.clock_sync.update_drift_estimates()
                last_sync_update = elapsed

            # Broadcast sync marker based on tier
            if self.clock_sync and self.controller:
                tier = self.controller.state_machine.current_tier
                if self.clock_sync.marker_manager.should_broadcast(elapsed, tier):
                    self.clock_sync.broadcast_sync(elapsed)
                    last_sync_broadcast = elapsed

            # Triage model inference on IMU window
            if triage_interpreter and len(imu_window) >= 30:
                # Run inference
                input_data = np.array(imu_window[-30:], dtype=np.float32).reshape(1, 30, 9)
                input_details = triage_interpreter.get_input_details()
                output_details = triage_interpreter.get_output_details()
                triage_interpreter.set_tensor(input_details[0]["index"], input_data)
                triage_interpreter.invoke()
                output = triage_interpreter.get_tensor(output_details[0]["index"])
                pred_class = int(np.argmax(output))
                class_names = tier_model_cfg["output_classes"]
                self.logger.debug(
                    f"Triage: {class_names[pred_class]} (conf={output[0][pred_class]:.3f})"
                )

            # Check promotion (T0 -> T1 on immobility)
            if (
                self.controller
                and self.controller.state_machine.is_tier0()
                and self.controller.immobility_detector is not None
                and self.controller.immobility_detector.current_window_immobile
            ):
                if immobility_start is None:
                    immobility_start = elapsed
                elif elapsed - immobility_start >= val_cfg["immobility_hold_s"]:
                    if (
                        self.controller.power_budget.can_afford_tier1(2.0)
                        and self.controller._motion_gate_armed()
                    ):
                        self.logger.info(f"PROMOTION: T0 -> T1 at {elapsed:.1f}s (immobility held)")
                        promotion_occurred = True
                        immobility_start = None  # Reset
            else:
                immobility_start = None

            # Check demotion (T1 -> T0 on movement)
            if (
                self.controller
                and self.controller.state_machine.is_tier1()
                and not movement_detected
            ):
                # Get latest ACC magnitude from buffer
                if self.stream_buffers.get("SYNAPSE_ACC_T0"):
                    latest_acc = self.stream_buffers["SYNAPSE_ACC_T0"][-1]
                    acc_mag = float(np.linalg.norm(latest_acc))
                    if acc_mag > val_cfg["movement_threshold_g"]:
                        self.logger.info(
                            f"DEMOTION: T1 -> T0 at {elapsed:.1f}s (movement: {acc_mag:.2f}g)"
                        )
                        demotion_occurred = True
                        movement_detected = True

            # Reset movement detection when calm
            if movement_detected and self.stream_buffers.get("SYNAPSE_ACC_T0"):
                latest_acc = self.stream_buffers["SYNAPSE_ACC_T0"][-1]
                acc_mag = float(np.linalg.norm(latest_acc))
                if acc_mag <= val_cfg["movement_threshold_g"]:
                    movement_detected = False

            # Periodic status log
            if elapsed - last_log >= 10.0:
                tier = (
                    self.controller.state_machine.current_tier.name
                    if self.controller
                    else "UNKNOWN"
                )
                self.logger.info(
                    f"Status: t={elapsed:.1f}s tier={tier} "
                    f"samples: ECG={self.sample_counts['ecg']} "
                    f"PPG={self.sample_counts['ppg']} IMU={self.sample_counts['imu']}"
                )
                last_log = elapsed

            await asyncio.sleep(0.01)  # 10ms loop

        self.logger.info("Validation loop complete")
        return {
            "promotion_occurred": promotion_occurred,
            "demotion_occurred": demotion_occurred,
            "final_tier": self.controller.state_machine.current_tier.name
            if self.controller
            else "T0",
            "sample_counts": self.sample_counts.copy(),
        }

    def _save_xdf_and_verify(self) -> dict:
        """Save recorded streams to XDF and verify round-trip."""
        val_cfg = self.config["bringup"]["validation"]
        output_dir = Path(val_cfg["xdf_output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        xdf_path = output_dir / val_cfg["xdf_filename_template"].format(timestamp=timestamp)

        self.logger.info(f"Saving XDF to {xdf_path}...")

        # Prepare streams for XDF
        streams_for_xdf = []
        for name, data in self.stream_buffers.items():
            if not data:
                continue
            timestamps = np.array(self.stream_timestamps[name], dtype=np.float64)
            data_arr = np.array(data, dtype=np.float64)
            if data_arr.ndim == 1:
                data_arr = data_arr.reshape(-1, 1)

            # Get stream config
            cfg = self.lsl_manager.stream_configs.get(name, {})
            stream_type = cfg.get("type", "Other")
            channel_count = data_arr.shape[1]
            sampling_rate = cfg.get("sampling_rate", 0)
            tier = cfg.get("tier", 0)

            streams_for_xdf.append(
                {
                    "name": name,
                    "type": stream_type,
                    "data": data_arr,
                    "timestamps": timestamps,
                    "sampling_rate": sampling_rate,
                    "tier": tier,
                }
            )

        # XDF round-trip verification
        try:
            xdf_proof = verify_xdf_roundtrip(streams_for_xdf, xdf_path)
            self.logger.info(f"XDF verification: {xdf_proof}")
            return xdf_proof
        except Exception:
            self.logger.exception("XDF verification failed")
            return {
                "all_streams_valid": False,
                "total_dropped": -1,
                "total_expected": 0,
                "total_recovered": 0,
            }

    def _collect_final_results(self, loop_results: dict, xdf_proof: dict) -> dict:
        """Collect all results for Phase 1 gate report."""
        sync_status = {}
        tier1_sync_status = {}
        if self.clock_sync:
            sync_status = self.clock_sync.get_sync_status(Tier.T0)
            tier1_sync_status = self.clock_sync.get_sync_status(Tier.T1)

        power_status = None
        if self.controller and self.controller.power_budget:
            power_status = self.controller.power_budget.get_status()

        transitions = []
        if self.controller:
            transitions = [
                {
                    "from": t.from_tier.name,
                    "to": t.to_tier.name,
                    "transition": t.transition.value,
                    "reason": t.reason,
                    "timestamp": t.timestamp,
                }
                for t in self.controller.state_machine.transition_history
            ]

        return {
            "promotion_occurred": loop_results["promotion_occurred"],
            "demotion_occurred": loop_results["demotion_occurred"],
            "final_tier": loop_results["final_tier"],
            "sample_counts": loop_results["sample_counts"],
            "transitions": transitions,
            "xdf_proof": xdf_proof,
            "tier0_sync_residuals": sync_status,
            "tier1_sync_residuals": tier1_sync_status,
            "power_budget": {
                "battery_remaining_mah": power_status.battery_remaining_mah if power_status else 0,
                "estimated_remaining_h": power_status.estimated_remaining_h if power_status else 0,
                "tier0_h_used": power_status.tier0_h_used if power_status else 0,
                "tier1_h_used": power_status.tier1_h_used if power_status else 0,
                "can_afford_tier1": power_status.can_afford_tier1 if power_status else False,
                "power_draw_mw": power_status.power_draw_mw if power_status else 0,
            }
            if power_status
            else {},
        }

    async def run(self, flash: bool = False, duration_s: float | None = None) -> dict:
        """Run the complete hardware bringup and validation."""
        bringup_cfg = self.config["bringup"]
        val_cfg = bringup_cfg["validation"]

        if duration_s is None:
            duration_s = val_cfg["duration_s"]

        self.logger.info("=" * 60)
        self.logger.info("SYNAPSE-24 Hardware Bringup — Phase 1 Tier 0 Validation")
        self.logger.info("=" * 60)

        # Step 1: Flash firmware if requested
        if flash:
            success = await self.flash_firmware()
            if not success:
                return {"success": False, "error": "Firmware flash failed"}
            # Wait for device to reboot
            await asyncio.sleep(3)

        # Step 2: Discover and connect to BLE device
        self.device = await self.discover_device()
        if self.device is None:
            return {"success": False, "error": "No BLE device found"}

        self.logger.info(f"Connecting to {self.device.address}...")
        self.client = BleakClient(self.device, timeout=bringup_cfg["ble"]["connect_timeout_s"])
        await self.client.connect()
        self.logger.info("Connected!")

        # Negotiate MTU
        try:
            await self.client.write_gatt_char(
                "00002a00-0000-1000-8000-00805f9b34fb",  # Device name (dummy write to trigger MTU exchange)
                b"",
            )
        except Exception:
            pass

        # Step 3: Setup LSL, clock sync, acquisition controller
        self._create_lsl_outlets()
        self._setup_clock_sync()
        self._setup_acquisition_controller()

        # Step 4: Subscribe to sensor notifications
        await self._subscribe_characteristics()
        self.logger.info("Subscribed to all characteristics")

        # Step 5: Run validation loop
        loop_results = await self._run_validation_loop(duration_s)

        # Step 6: Cleanup
        self.running = False
        for name, char_uuid in bringup_cfg["ble"]["characteristics"].items():
            try:
                await self.client.stop_notify(char_uuid)
            except Exception:
                pass

        await self.client.disconnect()
        self.lsl_manager.close()

        # Step 7: Save XDF and verify
        xdf_proof = self._save_xdf_and_verify()

        # Step 8: Collect final results
        results = self._collect_final_results(loop_results, xdf_proof)
        results["success"] = True

        self.logger.info("=" * 60)
        self.logger.info("HARDWARE BRINGUP COMPLETE")
        self.logger.info(f"  Promotion: {'YES' if results['promotion_occurred'] else 'NO'}")
        self.logger.info(f"  Demotion: {'YES' if results['demotion_occurred'] else 'NO'}")
        self.logger.info(f"  Final Tier: {results['final_tier']}")
        self.logger.info(f"  Samples: {results['sample_counts']}")
        self.logger.info(f"  XDF Dropped: {xdf_proof.get('total_dropped', 'N/A')}")
        self.logger.info("=" * 60)

        return results


async def main():
    parser = argparse.ArgumentParser(description="SYNAPSE-24 Hardware Bringup")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/hardware_bringup.yaml"),
        help="Path to hardware bringup config",
    )
    parser.add_argument(
        "--flash",
        action="store_true",
        help="Flash firmware before connecting",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Validation duration in seconds (default: from config)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose logging",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    bringup = HardwareBringup(args.config)
    results = await bringup.run(flash=args.flash, duration_s=args.duration)

    # Print summary
    if results.get("success"):
        print("\n✅ Hardware bringup SUCCESS")
        sys.exit(0)
    else:
        print(f"\n❌ Hardware bringup FAILED: {results.get('error', 'Unknown error')}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
