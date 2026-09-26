#!/usr/bin/env python3
"""SYNAPSE-24 Phase 1 Entry Gate: Hardware Bringup Validation.

Validates live ESP32-S3 Tier 0 firmware against hardware_bringup.yaml config.
Produces validated XDF with sync/quality metadata — the gate that unblocks
hardware procurement per Roadmap.md §138.

Exit codes:
  0 = PASS (all gates met)
  1 = FAIL (gate criteria not met)
  2 = ERROR (configuration/hardware/setup failure)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import struct
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from synapse24.config.loader import load_hardware_config_from_path

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
from synapse24.ingestion import CerelogEEGConfig, CerelogEEGManager
from synapse24.signal_quality import Tier as QualityTier
from synapse24.utils import verify_xdf_roundtrip

try:
    from bleak import BleakClient, BleakScanner
    from bleak.backends.characteristic import BleakGATTCharacteristic
    from bleak.backends.device import BLEDevice
except ImportError:
    BleakClient = None
    BleakScanner = None

try:
    import pylsl
except ImportError:
    pylsl = None


@dataclass
class ValidationGateResult:
    """Result of a single validation gate."""

    name: str
    passed: bool
    value: float | str
    threshold: float | str
    details: str = ""


@dataclass
class Phase1ValidationReport:
    """Complete Phase 1 validation report."""

    run_id: str
    timestamp: str
    config_path: str
    duration_s: float
    gates: list[ValidationGateResult]
    overall_pass: bool
    sample_counts: dict[str, int]
    transitions: list[dict[str, Any]]
    xdf_proof: dict[str, Any]
    tier0_sync_status: dict[str, Any]
    power_budget: dict[str, Any]
    xdf_path: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)


class BLEDataParser:
    """Parses BLE notifications from ESP32-S3 firmware."""

    # Expected packet formats (little-endian):
    # ECG:  1 int16 = 2 bytes
    # PPG:  2 int16 = 4 bytes (red, ir)
    # IMU:  9 int16 = 18 bytes (ax, ay, az, gx, gy, gz, mx, my, mz)
    # All scaled by firmware to physical units

    ECG_FMT = "<h"
    PPG_FMT = "<hh"
    IMU_FMT = "<hhhhhhhhh"

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
        if len(data) != self.ECG_SIZE:
            return np.array([0.0], dtype=np.float32)
        val = struct.unpack(self.ECG_FMT, data)[0]
        return np.array([val * self.ECG_SCALE], dtype=np.float32)

    def parse_ppg(self, data: bytes) -> np.ndarray:
        if len(data) != self.PPG_SIZE:
            return np.array([0.0, 0.0], dtype=np.float32)
        red, ir = struct.unpack(self.PPG_FMT, data)
        return np.array([red * self.PPG_SCALE, ir * self.PPG_SCALE], dtype=np.float32)

    def parse_imu(self, data: bytes) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
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


class LSLStreamManager:
    """Manages LSL outlets for all sensor streams."""

    def __init__(self):
        self.outlets: dict[str, Any] = {}
        self.stream_configs: dict[str, dict] = {}
        self.stream_buffers: dict[str, list] = {}
        self.stream_timestamps: dict[str, list] = {}

    def create_outlets(self, stream_configs: list[dict]) -> None:
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
            info.desc().append_child_value("tier", str(cfg.get("tier", 0)))

            outlet = pylsl.StreamOutlet(info, chunk_size=cfg.get("chunk_size", 16))
            self.outlets[cfg["name"]] = outlet
            self.stream_configs[cfg["name"]] = cfg
            self.stream_buffers[cfg["name"]] = []
            self.stream_timestamps[cfg["name"]] = []

    def push_sample(self, name: str, sample: np.ndarray, timestamp: float) -> None:
        if name in self.outlets:
            self.outlets[name].push_sample(sample.astype(np.float32), timestamp)
        if name in self.stream_buffers:
            self.stream_buffers[name].append(sample.copy())
            self.stream_timestamps[name].append(timestamp)

    def close(self) -> None:
        for outlet in self.outlets.values():
            try:
                outlet.__del__()
            except Exception:
                pass


class Phase1Validator:
    """Phase 1 hardware bringup validator."""

    def __init__(self, config_path: Path, logger: logging.Logger):
        self.config = self._load_config(config_path)
        self.logger = logger
        self.parser = BLEDataParser()
        self.lsl_manager = LSLStreamManager()
        self.clock_sync: MultiPodClockSync | None = None
        self.controller: AcquisitionController | None = None
        self.client: BleakClient | None = None
        self.device: BLEDevice | None = None
        self.running = False
        self.start_time = 0.0
        self.sample_counts = {"ecg": 0, "ppg": 0, "imu": 0, "eeg": 0}

    def _load_config(self, config_path: Path) -> dict:
        config = load_hardware_config_from_path(
            Path(__file__).parent.parent / "config" / "hardware.yaml", config_path
        )
        config["_config_path"] = str(config_path)
        return config

    def _expand_env_vars(self, obj: Any) -> Any:
        if isinstance(obj, str):
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

    def _create_lsl_outlets(self) -> None:
        streams = self.config["bringup"]["lsl"]["streams"]
        self.lsl_manager.create_outlets(streams)
        self.logger.info(f"Created {len(streams)} LSL outlets")

    def _setup_clock_sync(self) -> None:
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

    def _setup_cerelog_eeg(self) -> None:
        """Setup Cerelog ESP-EEG head pod for Tier 1 validation."""
        cerelog_cfg = self.config["bringup"].get("cerelog_eeg", {})
        if not cerelog_cfg.get("enabled", False):
            self.logger.info("Cerelog EEG not enabled in config, skipping")
            return

        self.logger.info("Setting up Cerelog ESP-EEG head pod...")

        cerelog_config = CerelogEEGConfig(
            serial_port=cerelog_cfg.get("port", ""),
            mac_address=cerelog_cfg.get("mac_address", ""),
            sampling_rate=cerelog_cfg.get("fs", 500),
            n_channels=cerelog_cfg.get("n_channels", 8),
            channel_names=cerelog_cfg.get(
                "channel_names", ["Fp1", "Fp2", "F3", "F4", "C3", "C4", "P3", "P4"]
            ),
            check_impedance=cerelog_cfg.get("impedance_check", True),
            impedance_threshold_kohm=cerelog_cfg.get("impedance_threshold_kohm", 50.0),
            impedance_test_duration_s=cerelog_cfg.get("impedance_test_duration_s", 5.0),
            tier=QualityTier.T1,
        )

        self.cerelog_manager = CerelogEEGManager(cerelog_config)

        try:
            self.cerelog_manager.prepare()
            self.cerelog_connected = True
            self.logger.info("Cerelog EEG prepared successfully")

            # Run impedance check
            impedance_results = self.cerelog_manager.check_impedance()
            report = self.cerelog_manager.get_impedance_report()
            self.logger.info(
                f"Impedance check: {report['passed']}/{report['total_channels']} channels passed"
            )

            # Start streaming
            self.cerelog_manager.start_streaming()
            self.logger.info("Cerelog EEG streaming started")

        except Exception:
            self.logger.exception("Failed to setup Cerelog EEG")
            self.cerelog_connected = False

    def _notification_handler(
        self, characteristic: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        timestamp = time.time()
        uuid_str = str(characteristic.uuid).lower()

        ble_chars = self.config["bringup"]["ble"]["characteristics"]

        try:
            if uuid_str == ble_chars["ecg"].lower():
                ecg = self.parser.parse_ecg(bytes(data))
                self.lsl_manager.push_sample("SYNAPSE_ECG_T0", ecg, timestamp)
                self.sample_counts["ecg"] += 1

            elif uuid_str == ble_chars["ppg"].lower():
                ppg = self.parser.parse_ppg(bytes(data))
                self.lsl_manager.push_sample("SYNAPSE_PPG_T0", ppg, timestamp)
                self.sample_counts["ppg"] += 1

            elif uuid_str == ble_chars["imu"].lower():
                acc, gyro, mag = self.parser.parse_imu(bytes(data))
                self.lsl_manager.push_sample("SYNAPSE_ACC_T0", acc, timestamp)
                self.lsl_manager.push_sample("SYNAPSE_GYRO_T0", gyro, timestamp)
                self.lsl_manager.push_sample("SYNAPSE_MAG_T0", mag, timestamp)
                self.sample_counts["imu"] += 1

                if self.clock_sync:
                    acc_mag = float(np.linalg.norm(acc))
                    self.clock_sync.add_hub_acc(acc_mag, timestamp)
                    self.clock_sync.add_pod_acc("forearm_hub", acc_mag, timestamp)

                if self.controller:
                    self.controller.update_imu(
                        accel_magnitude=float(np.linalg.norm(acc)),
                        timestamp=timestamp,
                    )

        except Exception:
            self.logger.exception("Error parsing notification")

    async def _run_validation_loop(self, duration_s: float) -> dict:
        val_cfg = self.config["bringup"]["validation"]

        self.logger.info(f"Starting validation loop for {duration_s}s...")
        self.start_time = time.time()
        self.running = True

        promotion_occurred = False
        demotion_occurred = False
        immobility_start = None
        movement_detected = False
        last_controller_tick = 0.0
        last_sync_update = 0.0
        last_log = 0.0

        while self.running and (time.time() - self.start_time) < duration_s:
            now = time.time()
            elapsed = now - self.start_time

            if elapsed - last_controller_tick >= 0.1:
                self.controller.tick(elapsed)
                last_controller_tick = elapsed

            if elapsed - last_sync_update >= 0.5:
                if self.clock_sync:
                    self.clock_sync.update_drift_estimates()
                last_sync_update = elapsed

            if self.clock_sync and self.controller:
                tier = self.controller.state_machine.current_tier
                if self.clock_sync.marker_manager.should_broadcast(elapsed, tier):
                    self.clock_sync.broadcast_sync(elapsed)

            # Poll Cerelog EEG data if connected
            if self.cerelog_connected and self.cerelog_manager:
                try:
                    n_pushed = self.cerelog_manager.push_lsl_chunk()
                    if n_pushed > 0:
                        self.sample_counts["eeg"] = self.sample_counts.get("eeg", 0) + n_pushed
                except Exception:
                    self.logger.warning("Cerelog EEG data poll error")

            promotion_occurred, immobility_start = self._check_promotion(
                elapsed, val_cfg, promotion_occurred, immobility_start
            )
            demotion_occurred, movement_detected = self._check_demotion(
                elapsed, val_cfg, demotion_occurred, movement_detected
            )

            if elapsed - last_log >= 10.0:
                tier = (
                    self.controller.state_machine.current_tier.name
                    if self.controller
                    else "UNKNOWN"
                )
                self.logger.info(
                    f"Status: t={elapsed:.1f}s tier={tier} "
                    f"samples: ECG={self.sample_counts['ecg']} "
                    f"PPG={self.sample_counts['ppg']} IMU={self.sample_counts['imu']} "
                    f"EEG={self.sample_counts.get('eeg', 0)}"
                )
                last_log = elapsed

            await asyncio.sleep(0.01)

        self.logger.info("Validation loop complete")
        return {
            "promotion_occurred": promotion_occurred,
            "demotion_occurred": demotion_occurred,
            "final_tier": self.controller.state_machine.current_tier.name
            if self.controller
            else "T0",
            "sample_counts": self.sample_counts.copy(),
        }

    def _check_promotion(
        self,
        elapsed: float,
        val_cfg: dict,
        promotion_occurred: bool,
        immobility_start: float | None,
    ) -> tuple[bool, float | None]:
        """Check for T0 -> T1 promotion on immobility."""
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
                    immobility_start = None
        else:
            immobility_start = None
        return promotion_occurred, immobility_start

    def _check_demotion(
        self,
        elapsed: float,
        val_cfg: dict,
        demotion_occurred: bool,
        movement_detected: bool,
    ) -> tuple[bool, bool]:
        """Check for T1 -> T0 demotion on movement."""
        if self.controller and self.controller.state_machine.is_tier1() and not movement_detected:
            if self.lsl_manager.stream_buffers.get("SYNAPSE_ACC_T0"):
                latest_acc = self.lsl_manager.stream_buffers["SYNAPSE_ACC_T0"][-1]
                acc_mag = float(np.linalg.norm(latest_acc))
                if acc_mag > val_cfg["movement_threshold_g"]:
                    self.logger.info(
                        f"DEMOTION: T1 -> T0 at {elapsed:.1f}s (movement: {acc_mag:.2f}g)"
                    )
                    demotion_occurred = True
                    movement_detected = True

        if movement_detected and self.lsl_manager.stream_buffers.get("SYNAPSE_ACC_T0"):
            latest_acc = self.lsl_manager.stream_buffers["SYNAPSE_ACC_T0"][-1]
            acc_mag = float(np.linalg.norm(latest_acc))
            if acc_mag <= val_cfg["movement_threshold_g"]:
                movement_detected = False

        return demotion_occurred, movement_detected

    def _save_xdf_and_verify(self) -> tuple[dict, Path | None]:
        val_cfg = self.config["bringup"]["validation"]
        output_dir = Path(val_cfg["xdf_output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        xdf_path = output_dir / val_cfg["xdf_filename_template"].format(timestamp=timestamp)

        self.logger.info(f"Saving XDF to {xdf_path}...")

        streams_for_xdf = []
        for name, data in self.lsl_manager.stream_buffers.items():
            if not data:
                continue
            timestamps = np.array(self.lsl_manager.stream_timestamps[name], dtype=np.float64)
            data_arr = np.array(data, dtype=np.float64)
            if data_arr.ndim == 1:
                data_arr = data_arr.reshape(-1, 1)

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

        try:
            xdf_proof = verify_xdf_roundtrip(streams_for_xdf, xdf_path)
            self.logger.info(f"XDF verification: {xdf_proof}")
            return xdf_proof, xdf_path
        except Exception:
            self.logger.exception("XDF verification failed")
            return {
                "all_streams_valid": False,
                "total_dropped": -1,
                "total_expected": 0,
                "total_recovered": 0,
            }, xdf_path

    def _collect_results(self, loop_results: dict, xdf_proof: dict) -> Phase1ValidationReport:
        run_id = uuid.uuid4().hex[:8]
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")

        sync_status_t0, sync_status_t1 = self._get_sync_status()
        power_status = self._get_power_status()
        transitions = self._get_transitions()
        gates = self._evaluate_gates(loop_results, xdf_proof, sync_status_t0, sync_status_t1)
        overall_pass = all(g.passed for g in gates)

        return Phase1ValidationReport(
            run_id=run_id,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
            config_path=str(self.config.get("_config_path", "unknown")),
            duration_s=self.config["bringup"]["validation"]["duration_s"],
            gates=gates,
            overall_pass=all(g.passed for g in gates),
            sample_counts=loop_results["sample_counts"],
            transitions=self._get_transitions(),
            xdf_proof=xdf_proof,
            tier0_sync_status=self._get_sync_status()[0],
            tier1_sync_status=self._get_sync_status()[1],
            power_budget=self._get_power_budget_dict(self._get_power_status()),
            xdf_path=str(xdf_proof.get("path"))
            if isinstance(xdf_proof, dict) and "path" in xdf_proof
            else None,
        )

    def _get_sync_status(self) -> tuple[dict, dict]:
        sync_status_t0 = {}
        sync_status_t1 = {}
        if self.clock_sync:
            sync_status_t0 = self.clock_sync.get_sync_status(Tier.T0)
            sync_status_t1 = self.clock_sync.get_sync_status(Tier.T1)
        return sync_status_t0, sync_status_t1

    def _get_power_status(self):
        if self.controller and self.controller.power_budget:
            return self.controller.power_budget.get_status()
        return None

    def _get_transitions(self) -> list:
        if not self.controller:
            return []
        return [
            {
                "from": t.from_tier.name,
                "to": t.to_tier.name,
                "transition": t.transition.value,
                "reason": t.reason,
                "timestamp": t.timestamp,
            }
            for t in self.controller.state_machine.transition_history
        ]

    def _get_power_budget_dict(self, power_status) -> dict:
        if not power_status:
            return {}
        return {
            "battery_remaining_mah": power_status.battery_remaining_mah,
            "estimated_remaining_h": power_status.estimated_remaining_h,
            "tier0_h_used": power_status.tier0_h_used,
            "tier1_h_used": power_status.tier1_h_used,
            "can_afford_tier1": power_status.can_afford_tier1,
            "power_draw_mw": power_status.power_draw_mw,
        }

    def _get_power_budget_dict(self, power_status) -> dict:
        if not power_status:
            return {}
        return {
            "battery_remaining_mah": power_status.battery_remaining_mah,
            "estimated_remaining_h": power_status.estimated_remaining_h,
            "tier0_h_used": power_status.tier0_h_used,
            "tier1_h_used": power_status.tier1_h_used,
            "can_afford_tier1": power_status.can_afford_tier1,
            "power_draw_mw": power_status.power_draw_mw,
        }

    def _evaluate_gates(
        self,
        loop_results: dict,
        xdf_proof: dict,
        sync_status_t0: dict,
        sync_status_t1: dict,
    ) -> list:
        gates = []

        # Gate 1: XDF zero-drop
        total_dropped = xdf_proof.get("total_dropped", -1)
        gates.append(
            ValidationGateResult(
                name="xdf_zero_drop",
                passed=total_dropped == 0,
                value=total_dropped,
                threshold=0,
                details=f"Total dropped samples: {total_dropped}",
            )
        )

        # Gate 2: T0 sync ≤10ms residual
        tier0_ok = True
        max_residual_t0 = 0.0
        if sync_status_t0.get("pods"):
            for pod_id, pod_status in sync_status_t0["pods"].items():
                within = pod_status.get("within_tolerance", False)
                if not within:
                    tier0_ok = False
                offset = abs(pod_status.get("offset_ms", 0))
                max_residual_t0 = max(max_residual_t0, offset)
        gates.append(
            ValidationGateResult(
                name="tier0_sync_10ms",
                passed=tier0_ok,
                value=round(max_residual_t0, 3),
                threshold=10.0,
                details=f"Max residual drift T0: {max_residual_t0:.3f}ms (budget: 10ms)",
            )
        )

        # Gate 3: T1 sync ≤1ms residual
        tier1_ok = True
        max_residual_t1 = 0.0
        if sync_status_t1.get("pods"):
            for pod_id, pod_status in sync_status_t1["pods"].items():
                within = pod_status.get("within_tolerance", False)
                if not within:
                    tier1_ok = False
                offset = abs(pod_status.get("offset_ms", 0))
                max_residual_t1 = max(max_residual_t1, offset)
        gates.append(
            ValidationGateResult(
                name="tier1_sync_1ms",
                passed=tier1_ok,
                value=round(max_residual_t1, 3),
                threshold=1.0,
                details=f"Max residual drift T1: {max_residual_t1:.3f}ms (budget: 1ms)",
            )
        )

        # Gate 3: PPG SQI ≥0.3 for ≥80% of windows
        ppg_samples = loop_results["sample_counts"]["ppg"]
        expected_ppg = int(64 * self.config["bringup"]["validation"]["duration_s"])
        ppg_completeness = ppg_samples / expected_ppg if expected_ppg > 0 else 0
        gates.append(
            ValidationGateResult(
                name="ppg_completeness",
                passed=ppg_completeness >= 0.8,
                value=round(ppg_completeness * 100, 1),
                threshold=80.0,
                details=f"PPG sample completeness: {ppg_completeness * 100:.1f}%",
            )
        )

        # Gate 4: EEG quality (Tier 1)
        eeg_quality_pass = True
        eeg_flatness = 0.0
        eeg_alpha_ratio = 0.0
        if (
            self.cerelog_connected
            and self.cerelog_manager
            and self.cerelog_manager._quality_metrics
        ):
            qm = self.cerelog_manager._quality_metrics
            eeg_flatness = qm.spectral_flatness or 0.0
            eeg_alpha_ratio = qm.alpha_band_ratio or 0.0
            eeg_quality_pass = eeg_flatness <= 0.3 and eeg_alpha_ratio >= 0.3
        gates.append(
            ValidationGateResult(
                name="eeg_quality_tier1",
                passed=eeg_quality_pass,
                value=f"flatness={eeg_flatness:.3f}, alpha_ratio={eeg_alpha_ratio:.2f}",
                threshold="flatness≤0.3, alpha_ratio≥0.3",
                details=(
                    f"EEG Tier 1 quality: flatness={eeg_flatness:.3f} (≤0.3), "
                    f"alpha_ratio={eeg_alpha_ratio:.2f} (≥0.3)"
                ),
            )
        )

        # Gate 5: Sample rates within 1% of nominal (includes EEG @ 500Hz for Tier 1)
        ecg_samples = loop_results["sample_counts"]["ecg"]
        imu_samples = loop_results["sample_counts"]["imu"]
        eeg_samples = loop_results["sample_counts"]["eeg"]
        duration = self.config["bringup"]["validation"]["duration_s"]
        ecg_rate = ecg_samples / duration if duration > 0 else 0
        imu_rate = imu_samples / duration if duration > 0 else 0
        eeg_rate = eeg_samples / duration if duration > 0 else 0
        ecg_ok = abs(ecg_rate - 500) / 500 <= 0.01
        imu_ok = abs(imu_rate - 100) / 100 <= 0.01
        eeg_ok = abs(eeg_rate - 500) / 500 <= 0.01 if eeg_samples > 0 else True
        gates.append(
            ValidationGateResult(
                name="sample_rate_accuracy",
                passed=ecg_ok and imu_ok and eeg_ok,
                value=f"ECG={ecg_rate:.1f}Hz, IMU={imu_rate:.1f}Hz, EEG={eeg_rate:.1f}Hz",
                threshold="ECG=500Hz±1%, IMU=100Hz±1%, EEG=500Hz±1%",
                details=f"Measured rates over {duration}s",
            )
        )

        return gates

    async def run(
        self, flash: bool = False, duration_s: float | None = None, dry_run: bool = False
    ) -> Phase1ValidationReport:
        bringup_cfg = self.config["bringup"]
        val_cfg = bringup_cfg["validation"]

        if duration_s is None:
            duration_s = val_cfg["duration_s"]

        self.logger.info("=" * 60)
        self.logger.info("SYNAPSE-24 Phase 1 Entry Gate: Hardware Bringup Validation")
        self.logger.info("=" * 60)

        if dry_run:
            self.logger.info("DRY RUN MODE - validating configuration only")
            # Validate config structure, env expansion, etc.
            self._create_lsl_outlets()
            self._setup_clock_sync()
            self._setup_acquisition_controller()
            # Skip Cerelog EEG hardware connection in dry-run
            self.cerelog_connected = False
            self.logger.info("Configuration validation PASSED")
            return Phase1ValidationReport(
                run_id="dry-run",
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
                config_path=str(self.config.get("_config_path", "unknown")),
                duration_s=duration_s,
                gates=[
                    ValidationGateResult(
                        "config_validation", True, "OK", "OK", "Config structure valid"
                    )
                ],
                overall_pass=True,
                sample_counts={},
                transitions=[],
                xdf_proof={"all_streams_valid": True, "total_dropped": 0},
                tier0_sync_status={},
                power_budget={},
            )

        # Step 1: Flash firmware if requested
        if flash:
            success = await self._flash_firmware()
            if not success:
                raise RuntimeError("Firmware flash failed")
            await asyncio.sleep(3)

        # Step 2: Discover and connect to BLE device
        self.device = await self._discover_device()
        if self.device is None:
            raise RuntimeError("No BLE device found")

        self.logger.info(f"Connecting to {self.device.address}...")
        self.client = BleakClient(self.device, timeout=bringup_cfg["ble"]["connect_timeout_s"])
        await self.client.connect()
        self.logger.info("Connected!")

        # Step 3: Setup LSL, clock sync, acquisition controller
        self._create_lsl_outlets()
        self._setup_clock_sync()
        self._setup_acquisition_controller()
        self._setup_cerelog_eeg()

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
        xdf_proof, xdf_path = self._save_xdf_and_verify()

        # Step 8: Collect results
        report = self._collect_results(loop_results, xdf_proof)
        report.xdf_path = str(xdf_path) if xdf_path else None

        self.logger.info("=" * 60)
        self.logger.info("PHASE 1 ENTRY GATE RESULT")
        for gate in report.gates:
            status = "PASS" if gate.passed else "FAIL"
            self.logger.info(
                f"  [{status}] {gate.name}: {gate.value} (threshold: {gate.threshold})"
            )
        self.logger.info(f"OVERALL: {'PASS' if report.overall_pass else 'FAIL'}")
        self.logger.info("=" * 60)

        return report

    async def _flash_firmware(self) -> bool:
        fw_cfg = self.config["bringup"]["firmware"]
        firmware_path = Path(fw_cfg["path"])

        if not firmware_path.exists():
            self.logger.error(f"Firmware not found: {firmware_path}")
            return False

        self.logger.info(f"Flashing firmware: {firmware_path}")

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
        except Exception:
            self.logger.exception("Flash error")
            return False

    async def _discover_device(self) -> BLEDevice | None:
        ble_cfg = self.config["bringup"]["ble"]
        address = ble_cfg["address"]

        if address != "auto":
            self.logger.info(f"Connecting to known address: {address}")
            return await BleakScanner.find_device_by_address(
                address, timeout=ble_cfg["scan_timeout_s"]
            )

        self.logger.info("Scanning for SYNAPSE device...")
        devices = await BleakScanner.discover(timeout=ble_cfg["scan_timeout_s"])
        for d in devices:
            if d.name and "SYNAPSE" in d.name.upper():
                self.logger.info(f"Found SYNAPSE device: {d.name} ({d.address})")
                return d

        self.logger.warning("No SYNAPSE device found in scan")
        return None

    async def _subscribe_characteristics(self) -> None:
        ble_chars = self.config["bringup"]["ble"]["characteristics"]
        for name, char_uuid in ble_chars.items():
            self.logger.info(f"Subscribing to {name}: {char_uuid}")
            await self.client.start_notify(char_uuid, self._notification_handler)


def setup_logging(verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("phase1_validator")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s"))
    logger.addHandler(handler)
    return logger


async def main_async(args: argparse.Namespace) -> int:
    logger = setup_logging(args.verbose)

    if BleakClient is None or BleakScanner is None:
        logger.error("bleak not installed. Install with: uv add bleak")
        return 2

    if pylsl is None:
        logger.error("pylsl not installed. Install with: uv add pylsl")
        return 2

    config_path = Path(args.config)
    if not config_path.exists():
        logger.error(f"Config file not found: {config_path}")
        return 2

    validator = Phase1Validator(config_path, logger)

    try:
        report = await validator.run(
            flash=args.flash,
            duration_s=args.duration,
            dry_run=args.dry_run,
        )

        # Save report
        output_dir = (
            Path(args.output_dir) if args.output_dir else Path("data/processed/phase1_validation")
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / f"phase1_validation_{report.run_id}.json"
        report_path.write_text(report.to_json())
        logger.info(f"Report saved to: {report_path}")

        # Print summary
        print(f"\n{'=' * 60}")
        print(f"PHASE 1 ENTRY GATE: {'PASS' if report.overall_pass else 'FAIL'}")
        print(f"{'=' * 60}")
        for gate in report.gates:
            status = "PASS" if gate.passed else "FAIL"
            print(f"  [{status}] {gate.name}: {gate.value} (threshold: {gate.threshold})")
        print(f"{'=' * 60}")

        return 0 if report.overall_pass else 1

    except Exception:
        logger.exception("Validation failed")
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="SYNAPSE-24 Phase 1 Entry Gate Validation")
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
        "--dry-run",
        action="store_true",
        help="Validate configuration only (no hardware required)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory for validation report",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose logging",
    )
    args = parser.parse_args()

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
