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
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from synapse24.config.loader import load_hardware_config_from_path
from synapse24.contracts_generated import UUIDS as CONTRACT_UUIDS

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from synapse24.acquisition.clock_sync import MultiPodClockSync, SyncConfig, TierSyncBudget
from synapse24.acquisition.coordinator import SensorPodCoordinator
from synapse24.acquisition.immobility import ImmobilityDetector
from synapse24.acquisition.night_window import NightWindowScheduler, SleepWindowConfig
from synapse24.acquisition.pod_clock_model import ClockExchange, PodClockModel
from synapse24.acquisition.power_budget import PowerBudgetManager
from synapse24.acquisition.state_machine import (
    AcquisitionController,
    MotionGateConfig,
    Tier,
)
from synapse24.acquisition.stream_spool import DEFAULT_TAIL_SAMPLES, SpoolManager
from synapse24.hardware.raw_codec import RawStream
from synapse24.hardware.raw_ingest import RawFrameIngestor
from synapse24.ingestion import CerelogEEGConfig, CerelogEEGManager
from synapse24.signal_quality import Tier as QualityTier
from synapse24.signal_quality.ppg import compute_ppg_quality
from synapse24.utils import validate_xdf, verify_xdf_roundtrip

try:
    from bleak import BleakClient, BleakError, BleakScanner
    from bleak.backends.characteristic import BleakGATTCharacteristic
    from bleak.backends.device import BLEDevice
except ImportError:
    BleakClient = None
    BleakScanner = None
    BleakError = None

try:
    import pylsl
except ImportError:
    pylsl = None

# Device Information Service, serial number (2A25). Read after connect so the
# XDF and the gate report name the unit that actually produced the samples.
DIS_SERIAL_UUID = "00002a25-0000-1000-8000-00805f9b34fb"
# The device-config characteristic is already in the GATT contract (read-only
# in practice: writes are refused by band-core). Its value is the pod's own
# account of what it booted with, generated from hardware.yaml by the
# firmware, so the host can prove the flashed configuration on the bench.
DEVICE_CONFIG_UUID = CONTRACT_UUIDS["CONSUMER_DEVICE_CONFIG"]
ROOT = Path(__file__).resolve().parents[1]

# Sync-exchange failures that are EXPECTED noise (malformed reply, radio
# hiccup). Everything else inside an exchange must surface: a blanket except
# could hide a firmware regression for a whole multi-hour session.
_SYNC_EXCHANGE_ERRORS: tuple[type[BaseException], ...] = (struct.error, OSError)
if BleakError is not None:
    _SYNC_EXCHANGE_ERRORS += (BleakError,)


@dataclass
class ValidationGateResult:
    """Result of a single validation gate.

    A gate that could not be evaluated is ``N/A`` — never a free PASS. The
    ``required`` flag comes from the run profile in hardware_bringup.yaml:
    ``overall_pass`` is computed only over required gates, so a G1 live run
    cannot pass while the cardiac and POWER measurements are missing.
    """

    name: str
    passed: bool
    value: float | str
    threshold: float | str
    details: str = ""
    status: str = ""
    required: bool = True

    def __post_init__(self) -> None:
        if not self.status:
            self.status = "PASS" if self.passed else "FAIL"
        elif self.status == "N/A":
            self.passed = False
        else:
            self.passed = self.status == "PASS"

    @classmethod
    def not_applicable(
        cls, name: str, reason: str, threshold: float | str = "n/a"
    ) -> ValidationGateResult:
        """A gate with no evidence: reported, but never counted as a pass."""
        return cls(
            name=name,
            passed=False,
            value="N/A",
            threshold=threshold,
            details=reason,
            status="N/A",
        )


def _overall_pass(gates: list[ValidationGateResult]) -> bool:
    """Only required gates decide the verdict; N/A is never a pass."""
    required = [gate for gate in gates if gate.required]
    return bool(required) and all(gate.passed for gate in required)


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
    tier1_sync_status: dict[str, Any]
    power_budget: dict[str, Any]
    xdf_path: str | None = None
    clock_model_status: dict[str, Any] = field(default_factory=dict)

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
    """Manages LSL outlets for all sensor streams.

    The samples are spooled to disk (see
    :class:`synapse24.acquisition.stream_spool.SpoolManager`): a 6-hour
    session at 500 Hz is ~11M samples, which does not fit in Python lists.
    Only a bounded tail stays in memory, and the full session is read back
    once, when the XDF is written.
    """

    def __init__(self):
        self.outlets: dict[str, Any] = {}
        self.stream_configs: dict[str, dict] = {}
        self.spool = SpoolManager()
        # Bounded in-memory view of every stream (the analysis tail).
        self.stream_buffers: dict[str, list] = {}
        self.stream_timestamps: dict[str, list] = {}
        # Samples pushed per stream (the gates judge these counts).
        self._sample_counts: dict[str, int] = {}

    @property
    def sample_counts(self) -> dict[str, int]:
        """Samples pushed per stream."""
        return dict(self._sample_counts)

    @staticmethod
    def render_source_id(cfg: dict, serial_hint: str = "") -> str:
        """The definitive LSL source_id for one stream of this session."""
        source_id = str(cfg["source_id"])
        if "{serial}" in source_id:
            return source_id.format(serial=serial_hint or "synthetic")
        if serial_hint and source_id.endswith("_001"):
            # Legacy configs without a template still get a unique UID.
            return f"{source_id[:-4]}_{serial_hint}"
        return source_id

    def create_outlets(self, stream_configs: list[dict], serial_hint: str = "") -> None:
        if pylsl is None:
            raise RuntimeError("pylsl not available")

        # A new session starts with an empty spool: the gates judge the
        # samples of *this* run, never the ones of the previous one.
        self.spool.reset()
        self._sample_counts.clear()

        for cfg in stream_configs:
            # A fixed `_001` source id would make two bands' recordings
            # indistinguishable in XDF. When the expected serial is known,
            # bind the LSL identity to it at outlet creation (source_id is
            # immutable once the outlet exists).
            source_id = self.render_source_id(cfg, serial_hint)
            info = pylsl.StreamInfo(
                name=cfg["name"],
                type=cfg["type"],
                channel_count=cfg["channel_count"],
                nominal_srate=cfg["sampling_rate"],
                channel_format=cfg["channel_format"],
                source_id=source_id,
            )
            info.desc().append_child_value("tier", str(cfg.get("tier", 0)))

            outlet = pylsl.StreamOutlet(info, chunk_size=cfg.get("chunk_size", 16))
            self.outlets[cfg["name"]] = outlet
            self.stream_configs[cfg["name"]] = cfg
            self.stream_buffers[cfg["name"]] = []
            self.stream_timestamps[cfg["name"]] = []
            self.spool.spool(cfg["name"])

    def push_sample(self, name: str, sample: np.ndarray, timestamp: float) -> None:
        if name in self.outlets:
            # The raw ingestor forwards plain Python lists; LSL accepts
            # anything array-like, and crashing here would kill a recording
            # on its very first sample.
            self.outlets[name].push_sample(np.asarray(sample, dtype=np.float32), timestamp)
        if name in self.stream_configs:
            row = np.asarray(sample, dtype=np.float64).ravel()
            self.spool.append(name, row, timestamp)
            self._sample_counts[name] = self._sample_counts.get(name, 0) + 1
            buffers = self.stream_buffers.setdefault(name, [])
            stamps = self.stream_timestamps.setdefault(name, [])
            buffers.append(row.copy())
            stamps.append(float(timestamp))
            if len(buffers) > DEFAULT_TAIL_SAMPLES:
                del buffers[: len(buffers) - DEFAULT_TAIL_SAMPLES]
                del stamps[: len(stamps) - DEFAULT_TAIL_SAMPLES]

    def stream_array(self, name: str) -> np.ndarray:
        """Every sample of a stream, read back from the spool."""
        rows, _stamps = self.spool.read_all(name)
        return rows

    def close(self) -> None:
        # pylsl's StreamOutlet frees its native handle in __del__; calling it
        # explicitly and then dropping the reference double-frees (the
        # "access violation reading 0xFFFF..." teardown noise). Clearing the
        # references is enough — and the stream metadata and the spooled
        # samples must survive, because the XDF gate proof is written *after*
        # the session closes.
        self.outlets.clear()


class Phase1Validator:
    """Phase 1 hardware bringup validator."""

    def __init__(
        self,
        config_path: Path,
        logger: logging.Logger,
        legacy_format: bool = False,
        transport: Any = None,
    ):
        self.config = self._load_config(config_path)
        self.logger = logger
        self.legacy_format = legacy_format
        # Injected BLE transport (CI / synthetic pod). When present, bleak
        # discovery and connect are bypassed entirely — that is what lets the
        # whole SOP run in CI with no hardware on the desk.
        self.transport = transport
        self.parser = BLEDataParser()
        self.lsl_manager = LSLStreamManager()
        self.clock_sync: MultiPodClockSync | None = None
        self.controller: AcquisitionController | None = None
        self.client: Any = None
        self.device: BLEDevice | None = None
        self.device_serial: str | None = None
        self.running = False
        self.start_time = 0.0
        self.sample_counts = {"ecg": 0, "ppg": 0, "imu": 0, "eeg": 0}
        self.cerelog_connected = False
        self.cerelog_manager: CerelogEEGManager | None = None
        # Pod→hub clock model, fed by the SYNC characteristic exchanges.
        # Guardian Principle 3: everything that reaches LSL lives in the
        # local_clock() domain, and the pod's own stamps are mapped through
        # this model rather than pushed raw.
        self.clock_model = PodClockModel(pod_id="forearm_hub")
        self._sync_sequence = 0
        # Throttle downstream feeds: immobility and PPG quality work on
        # 10-30 s windows, so recomputing them on every BLE notification
        # (tens per second) is wasted work and fills the detector window
        # faster than real time. One feed per second is ample.
        self._last_downstream_feed: float = 0.0
        # Duration of the run actually being executed (set by run()); the
        # gates judge this, never the config's nominal window.
        self._duration_s = float(self.config["bringup"]["validation"]["duration_s"])
        self.lsl_clock_fn: Callable[[], float] | None = None
        # Decodes the Synapse Research Service frame format (the canonical one)
        # and pushes straight into the LSL outlets. See raw_ingest.py.
        # The decoder keeps a bounded window (the session is written by the
        # spool, on disk): an unbounded history would put the whole session
        # in RAM again.
        self.raw_ingestor = RawFrameIngestor(
            push_callback=self._push_raw_to_lsl,
            clock_model=self.clock_model,
            history_limit=self._decoder_history_limit(),
        )
        # Provenance of the recording the report reads back from: WHO wrote
        # the artifact and its SHA-256. The soak's single writer is
        # LabRecorderCLI; everything else (the in-memory spool) is recovery
        # memory, and the report must name the difference instead of letting
        # a synthetic rehearsal masquerade as a 6-hour recording truth.
        self.artifact: dict[str, Any] = {
            "source": "unknown",
            "path": None,
            "sha256": None,
        }
        self.recorder_cli: str | None = None
        # True only for a real --live-hardware run: it selects the G1 gate
        # profile and the fail-closed preconditions. CI/synthetic runs keep the
        # reduced "synthetic" profile and must say so in the report.
        self.live_hardware = False
        # One measurement per session: both the gates and the SOP gate report
        # read these numbers, so they must not be recomputed twice.
        self._quality_cache: dict[str, Any] | None = None
        # What the pod says it actually booted with, read from its device
        # config characteristic before the recording starts.
        self.device_config_readback: dict[str, Any] = {}

    def _decoder_history_limit(self) -> int:
        """Values kept in memory per stream (30 s at the highest rate)."""
        rates = self._nominal_rates()
        highest = max(rates.values()) if rates else 500.0
        return int(max(highest, 1.0) * 30)

    def _push_raw_to_lsl(self, outlet: str, values: list[float], ts: float) -> None:
        """Forward one decoded raw sample row into its LSL outlet."""
        self.lsl_manager.push_sample(outlet, values, ts)

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

    def _lsl_serial(self) -> str:
        """The serial that goes into every LSL source_id of this session.

        The DIS value read from the connected pod wins; the configured serial
        is only a fallback for setup steps that run before connect. A live run
        with neither is a hard error: an untraceable recording is not G1
        evidence. Synthetic/CI runs are explicitly tagged ``synthetic``.
        """
        if self.device_serial:
            return self.device_serial
        expected = str(self.config["bringup"]["ble"].get("serial") or "").strip()
        if expected:
            return expected
        if self.live_hardware and self.transport is None:
            raise RuntimeError(
                "DIS serial unavailable: live recordings must bind LSL source_id "
                "to the device serial (set ble.serial or check the DIS characteristic)"
            )
        return "synthetic"

    def _create_lsl_outlets(self) -> None:
        streams = self.config["bringup"]["lsl"]["streams"]
        self.lsl_manager.create_outlets(streams, serial_hint=self._lsl_serial())
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
            acc_sampling_rates={
                "forearm_hub": int(self._nominal_rates().get("SYNAPSE_ACC_T0", 0) or 0)
            },
        )
        self.clock_sync = MultiPodClockSync(config)
        self.clock_sync.register_pod(
            "forearm_hub", int(self._nominal_rates().get("SYNAPSE_ACC_T0", 0) or 0)
        )

        marker_info = pylsl.StreamInfo(
            name="SYNAPSE_MARKERS",
            type="Markers",
            channel_count=1,
            nominal_srate=0,
            channel_format="string",
            source_id=f"synapse_markers_{self._lsl_serial()}",
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
            accel_sampling_rate=int(self._nominal_rates().get("SYNAPSE_ACC_T0", 0) or 0),
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
            self.cerelog_manager.check_impedance()
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
        # Guardian Principle 3: the LSL clock domain is the only timeline in
        # the signal path; wall time appears nowhere but the log line.
        lsl_now = self.lsl_clock_fn or (pylsl.local_clock if pylsl is not None else time.time)
        timestamp = float(lsl_now())
        uuid_str = str(characteristic.uuid).lower()

        ble_chars = self.config["bringup"]["ble"]["characteristics"]

        # The canonical firmware streams the Synapse Research Service frame
        # format. Decode that first; only if the payload is not one of those
        # frames do we fall back to the legacy sensor_sample_t parser, so an
        # unexpected firmware build is reported rather than mis-parsed.
        if self.raw_ingestor is not None:
            pushed = self.raw_ingestor.handle_notification(uuid_str, bytes(data))
            if pushed:
                now = time.monotonic()
                if now - self._last_downstream_feed >= 1.0:
                    self._feed_downstream_from_raw(timestamp)
                    self._last_downstream_feed = now
                return

        if not self.legacy_format:
            # The legacy Nordic-UART parser mis-parses the research service
            # format (wrong ECG scale, wrong rates), so an unrecognised
            # payload from a conforming pod must surface as a decoder
            # rejection — never as silently mangled samples.
            self.logger.warning(
                "Unrecognised notification on %s (%d bytes) — not a research "
                "service frame. Re-run with --legacy-format for the old "
                "sensor_sample_t wire format.",
                uuid_str,
                len(data),
            )
            return

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
        last_sync_exchange = 0.0
        last_log = 0.0

        while self.running and (time.time() - self.start_time) < duration_s:
            now = time.time()
            elapsed = now - self.start_time

            if elapsed - last_controller_tick >= 0.1:
                # Night window needs wall-clock (Unix epoch) to evaluate
                # the sleep window; session seconds (elapsed) would land in
                # 1970 inside datetime.fromtimestamp.
                self.controller.tick(time.time())
                last_controller_tick = elapsed

            if elapsed - last_sync_update >= 0.5:
                if self.clock_sync:
                    self.clock_sync.update_drift_estimates()
                last_sync_update = elapsed

            # Clock-sync exchanges over the SYNC characteristic feed the
            # pod→hub clock model (the Tier-0 interval from hardware.yaml).
            if elapsed - last_sync_exchange >= self._sync_interval_s():
                await self._run_sync_exchange()
                last_sync_exchange = elapsed

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
            "sample_counts": self._report_sample_counts(),
        }

    def _report_sample_counts(self) -> dict[str, int]:
        """Sample counts per modality, derived from the LSL outlets.

        The research-service path never touched the legacy ``sample_counts``
        dict, so a conforming pod used to report zero samples while the
        gates (which read the outlets) saw thousands. One source of truth.
        """
        counts = self._outlet_sample_counts()
        return {
            "ecg": counts.get("SYNAPSE_ECG_T0", self.sample_counts["ecg"]),
            "ppg": counts.get("SYNAPSE_PPG_T0", self.sample_counts["ppg"]),
            "imu": counts.get("SYNAPSE_ACC_T0", self.sample_counts["imu"]),
            "eeg": self.sample_counts.get("eeg", 0),
        }

    def _check_promotion(
        self,
        elapsed: float,
        val_cfg: dict,
        promotion_occurred: bool,
        immobility_start: float | None,
    ) -> tuple[bool, float | None]:
        """Check for T0 -> T1 promotion on immobility.

        The promotion is *performed* on the state machine, not just flagged:
        a validator that only sets a boolean leaves the pod in Tier 0 while
        the report claims the transition happened.
        """
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
                    promoted = self.controller.state_machine.promote_to_tier1(
                        "immobility_held",
                        metadata={"elapsed_s": elapsed},
                    )
                    promotion_occurred = promotion_occurred or promoted
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
        """Check for T1 -> T0 demotion on movement.

        As for the promotion, the demotion is performed on the state machine.
        """
        if self.controller and self.controller.state_machine.is_tier1() and not movement_detected:
            if self.lsl_manager.stream_buffers.get("SYNAPSE_ACC_T0"):
                latest_acc = self.lsl_manager.stream_buffers["SYNAPSE_ACC_T0"][-1]
                acc_mag = float(np.linalg.norm(latest_acc))
                if acc_mag > val_cfg["movement_threshold_g"]:
                    self.logger.info(
                        f"DEMOTION: T1 -> T0 at {elapsed:.1f}s (movement: {acc_mag:.2f}g)"
                    )
                    demoted = self.controller.state_machine.demote_to_tier0(
                        "movement_detected",
                        metadata={"acc_magnitude_g": acc_mag, "elapsed_s": elapsed},
                    )
                    demotion_occurred = demotion_occurred or demoted
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
        for name in sorted(self.lsl_manager.spool.counts()):
            data_arr, timestamps = self.lsl_manager.spool.read_all(name)
            if data_arr.size == 0:
                continue
            if data_arr.ndim == 1:
                data_arr = data_arr.reshape(-1, 1)

            cfg = self.lsl_manager.stream_configs.get(name, {})
            stream_type = cfg.get("type", "Other")
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

        sync_status_t0, sync_status_t1 = self._get_sync_status()
        gates = self._evaluate_gates(loop_results, xdf_proof, sync_status_t0, sync_status_t1)

        return Phase1ValidationReport(
            run_id=run_id,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
            config_path=str(self.config.get("_config_path", "unknown")),
            duration_s=self._duration_s,
            gates=gates,
            overall_pass=_overall_pass(gates),
            sample_counts=loop_results["sample_counts"],
            transitions=self._get_transitions(),
            xdf_proof=xdf_proof,
            tier0_sync_status=self._get_sync_status()[0],
            tier1_sync_status=self._get_sync_status()[1],
            power_budget=self._get_power_budget_dict(self._get_power_status()),
            clock_model_status=self.clock_model.status() if self.clock_model else {},
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

    # ------------------------------------------------------------------
    # Contract-derived expectations (config/hardware.yaml is the single
    # source of truth; these gates never hardcode a rate).
    # ------------------------------------------------------------------

    def _nominal_rates(self) -> dict[str, float]:
        """Nominal sampling rate per LSL outlet, from the stream declarations.

        The LSL declarations in hardware_bringup.yaml are pinned to
        hardware.yaml by tools/check_contract.py, so deriving the gate
        expectations here means a rate cannot change in the manifest and
        leave the validator asserting a stale value.
        """
        rates: dict[str, float] = {}
        for cfg in self.config["bringup"]["lsl"]["streams"]:
            rates[cfg["name"]] = float(cfg.get("sampling_rate", 0.0))
        return rates

    def _outlet_sample_counts(self) -> dict[str, int]:
        """Samples actually pushed per LSL outlet.

        Counting from the outlets (not from per-path counters) makes the
        gate correct whichever wire format the pod speaks: the legacy
        parser path used to increment ``sample_counts`` inline while the
        canonical research-service path did not, so the gates measured
        zero samples against a conforming pod.
        """
        return dict(self.lsl_manager.sample_counts)

    def _tier0_sync_gate(self, sync_status_t0: dict) -> ValidationGateResult:
        """T0 sync gate: the clock model's evidence, or an honest failure.

        The residual bounds the *stability* of the pod clock over the fit
        window; the exchange RTT bounds how well any single measurement
        could possibly know the offset (RTT/2, the documented NTP floor).
        Both must hold, and an uncalibrated model FAILS: "synchronized" must
        be measured, and a vacuous PASS is worse than a failure.
        """
        calibrated = self.clock_model is not None and self.clock_model.is_calibrated
        if calibrated:
            assert self.clock_model is not None  # narrowed by `calibrated`
            status = self.clock_model.status()
            residual_ms = float(status.get("residual_ms", 0.0))
            # Half the round trip is the uncertainty floor of one exchange;
            # a radio this slow cannot prove a 10 ms claim either.
            uncertainty_ms = float(status.get("median_rtt_ms", 0.0)) / 2.0
            worst_ms = max(residual_ms, uncertainty_ms)
            passed = worst_ms <= 10.0
            details = (
                f"Clock model evidence T0: {worst_ms:.3f}ms "
                f"(budget 10ms; fit residual {residual_ms:.3f}ms, "
                f"exchange floor RTT/2 {uncertainty_ms:.3f}ms, "
                f"offset {status.get('offset_ms', 0)}ms, "
                f"drift {status.get('drift_ppm', 0)}ppm, "
                f"{status.get('n_exchanges', 0)} exchanges)"
            )
        else:
            worst_ms = 0.0
            within = bool(sync_status_t0.get("pods"))
            if sync_status_t0.get("pods"):
                for pod_status in sync_status_t0["pods"].values():
                    if not pod_status.get("within_tolerance", False):
                        within = False
                    worst_ms = max(worst_ms, abs(pod_status.get("offset_ms", 0)))
            passed = within
            details = (
                f"ACC cross-correlation residual T0: {worst_ms:.3f}ms "
                f"(budget 10ms; no pod clock model calibrated)"
                if within
                else "No calibrated pod clock model: the pod→host clock was never "
                "measured, so synchronization is unproven"
            )
        return ValidationGateResult(
            name="tier0_sync_10ms",
            passed=passed,
            value=round(worst_ms, 3),
            threshold=10.0,
            details=details,
            required="tier0_sync_10ms" in self._required_gates(),
        )

    def _required_gates(self) -> set[str]:
        """Names of the gates that decide this run's verdict."""
        validation = self.config["bringup"]["validation"]
        profile = "live" if self.live_hardware else "synthetic"
        return set(validation.get("required_gates", {}).get(profile, []))

    def _gate(
        self,
        name: str,
        passed: bool,
        value: float | str,
        threshold: float | str,
        details: str,
    ) -> ValidationGateResult:
        return ValidationGateResult(
            name=name,
            passed=passed,
            value=value,
            threshold=threshold,
            details=details,
            required=name in self._required_gates(),
        )

    def _na(self, name: str, reason: str, threshold: float | str = "n/a") -> ValidationGateResult:
        gate = ValidationGateResult.not_applicable(name, reason, threshold)
        gate.required = name in self._required_gates()
        return gate

    def _ppg_quality_sustained(
        self, ppg_red: Any, ppg_rate: int, sustained: float
    ) -> tuple[float | None, float | None]:
        """Worst quality over evenly sampled ``sustained``-second windows.

        A single session average can hide a bad 30 s stretch, but evaluating
        every window of a 6 h soak is quadratic. Twelve evenly spaced windows
        bound the work while keeping the claim honest.
        """
        rows, stamps = self.lsl_manager.spool.read_all("SYNAPSE_PPG_T0")
        if not stamps.size or self._duration_s < sustained:
            ppg = compute_ppg_quality(ppg_red, ppg_rate)
            return ppg.get("ppg_sqi"), ppg.get("motion_artifact_prob")

        first = float(stamps[0])
        span = float(stamps[-1]) - first
        count = min(12, max(1, int(span // sustained) or 1))
        worst_sqi, worst_map = 1.0, 0.0
        for index in range(count):
            start = first + index * span / count
            window = (stamps >= start) & (stamps < start + sustained)
            segment = np.asarray(rows[window], dtype=np.float64)
            if segment.ndim == 1:
                segment = segment.reshape(-1, 1)
            if segment.shape[0] < 50:
                continue
            quality = compute_ppg_quality(segment[:, 0], ppg_rate)
            sqi = quality.get("ppg_sqi")
            map_value = quality.get("motion_artifact_prob")
            if sqi is not None:
                worst_sqi = min(worst_sqi, float(sqi))
            worst_map = max(worst_map, float(map_value) if map_value is not None else 1.0)
        return worst_sqi, worst_map

    def _quality_metrics(self) -> dict[str, Any]:
        """The session's HR/RMSSD/PPG metrics, computed once per run."""
        if self._quality_cache is not None:
            return self._quality_cache
        val_cfg = self.config["bringup"]["validation"]
        ecg_rate = int(self._nominal_rates().get("SYNAPSE_ECG_T0", 500) or 500)
        ppg_rate = int(self._nominal_rates().get("SYNAPSE_PPG_T0", 50) or 50)
        result: dict[str, Any] = {
            "hr_bpm": None,
            "rmssd_ms": None,
            "ppg_sqi": None,
            "ppg_map": None,
            "rmssd_mae_ms": None,
            "detected_peaks": None,
        }

        ecg = self._outlet_array("SYNAPSE_ECG_T0")
        if ecg.size > 50:
            try:
                from synapse24.signal_quality import compute_ecg_quality, detect_r_peaks_neurokit

                quality = compute_ecg_quality(ecg[:, 0], ecg_rate)
                hrv = getattr(quality, "hrv_metrics", {}) or {}
                result["hr_bpm"] = hrv.get("hr_mean_bpm")
                result["rmssd_ms"] = hrv.get("rmssd_ms")
                peaks = detect_r_peaks_neurokit(ecg[:, 0], ecg_rate)
                result["detected_peaks"] = peaks
            except Exception:  # noqa: BLE001 - a metric failure must not hide the rest
                self.logger.warning("ECG quality computation failed", exc_info=True)

        reference = val_cfg.get("ground_truth_ecg_path")
        if reference and result["detected_peaks"] is not None:
            try:
                from synapse24.signal_quality import rmssd_mae

                path = Path(str(reference))
                if not path.is_absolute():
                    path = ROOT / path
                with np.load(path) as data:
                    peaks = data["r_peaks"] if "r_peaks" in data else data[data.files[0]]
                result["rmssd_mae_ms"] = float(
                    rmssd_mae(
                        np.asarray(result["detected_peaks"], dtype=np.int64),
                        np.asarray(peaks, dtype=np.int64),
                        ecg_rate,
                    )
                )
            except Exception:  # noqa: BLE001 - an unusable reference is a gate failure
                self.logger.warning("Reference ECG could not be read", exc_info=True)

        ppg_red = self._outlet_array("SYNAPSE_PPG_T0", channel=0)
        sustained = float(val_cfg.get("ppg_sustained_s", 30.0))
        if ppg_red.size > 50:
            try:
                result["ppg_sqi"], result["ppg_map"] = self._ppg_quality_sustained(
                    ppg_red, ppg_rate, sustained
                )
            except Exception:  # noqa: BLE001 - as above
                self.logger.warning("PPG quality computation failed", exc_info=True)

        self._quality_cache = result
        return result

    def _expected_flashed_config(self) -> dict[str, float | int]:
        """The configuration the contract says the connected band must run."""
        bring = self.config["bringup"]
        rates = self._nominal_rates()
        pod = next(p for p in bring.get("pods", []) if p.get("pod_id") == "forearm_hub")
        meta = pod["metadata"]
        ecg_cfg = meta["ecg_config"]
        ppg_cfg = meta["ppg_config"]
        result: dict[str, float | int] = {
            "ecg_hz": rates["SYNAPSE_ECG_T0"],
            "ppg_hz": rates["SYNAPSE_PPG_T0"],
            "imu_hz": rates["SYNAPSE_ACC_T0"],
            "ecg_gain_x100": int(round(float(ecg_cfg["gain"]) * 100)),
            "ppg_led_red": int(ppg_cfg["led_current_red"]),
            "ppg_led_ir": int(ppg_cfg["led_current_ir"]),
            "battery_mah": int(bring["power_budget"]["hub_battery_mah"]),
        }
        # A calibrated unit is authoritative over the nominal manifest gain.
        cal_source = self._calibration_source()
        if cal_source:
            try:
                record = json.loads((ROOT / cal_source).read_text(encoding="utf-8"))
                for key in ("ecg_gain_x100", "ppg_led_red", "ppg_led_ir"):
                    if record.get(key):
                        result[key] = int(record[key])
            except Exception:  # noqa: BLE001 - a bad record is reported by the gate below
                self.logger.warning("Calibration record unreadable", exc_info=True)
        return result

    def _flashed_config_gate(self) -> ValidationGateResult:
        readback = self.device_config_readback
        if not readback or readback.get("error"):
            if self.live_hardware:
                return self._gate(
                    "flashed_config_matches_contract",
                    False,
                    "missing",
                    "device config == config/hardware.yaml",
                    "the pod did not report its live configuration; a stale sdkconfig "
                    "cannot be ruled out",
                )
            return self._na(
                "flashed_config_matches_contract",
                "no device-config readback on a synthetic transport",
            )
        mismatches = []
        for key, expected in self._expected_flashed_config().items():
            actual = readback.get(key)
            if actual is None or float(actual) != float(expected):
                mismatches.append(f"{key}: pod={actual!r}, contract={expected!r}")
        return self._gate(
            "flashed_config_matches_contract",
            not mismatches,
            "OK" if not mismatches else "; ".join(mismatches),
            "device config == config/hardware.yaml",
            "pod-reported rates/gain/LEDs/capacity compared with the manifest",
        )

    def _cardiac_gates(self) -> list[ValidationGateResult]:
        """HR, RMSSD and PPG quality: the rows the SOP says gate G1."""
        val_cfg = self.config["bringup"]["validation"]
        motion = self.config["bringup"]["motion_gate"]
        q = self._quality_metrics()
        gates: list[ValidationGateResult] = []

        manual_hr = val_cfg.get("manual_hr_bpm")
        tol_hr = float(val_cfg.get("hr_mae_max_bpm", 2.0))
        if manual_hr is None or q["hr_bpm"] is None:
            gates.append(
                self._na(
                    "hr_accuracy",
                    "no manual pulse count or no ECG R-peaks in this recording "
                    "(set validation.manual_hr_bpm for a live run)",
                    f"MAE <= {tol_hr:g} bpm",
                )
            )
        else:
            mae = abs(float(q["hr_bpm"]) - float(manual_hr))
            gates.append(
                self._gate(
                    "hr_accuracy",
                    mae <= tol_hr,
                    round(mae, 2),
                    f"MAE <= {tol_hr:g} bpm",
                    f"ECG HR {q['hr_bpm']:.1f} bpm vs manual {float(manual_hr):g} bpm",
                )
            )

        rmssd_mae = q.get("rmssd_mae_ms")
        tol_rmssd = float(val_cfg.get("rmssd_mae_max_ms", 5.0))
        if rmssd_mae is None:
            gates.append(
                self._na(
                    "rmssd_accuracy",
                    "no NeuroKit2 reference R-peaks (set validation.ground_truth_ecg_path "
                    "to an .npz with key `r_peaks`)",
                    f"MAE < {tol_rmssd:g} ms",
                )
            )
        else:
            gates.append(
                self._gate(
                    "rmssd_accuracy",
                    rmssd_mae < tol_rmssd,
                    round(rmssd_mae, 2),
                    f"MAE < {tol_rmssd:g} ms",
                    "RMSSD of this recording vs NeuroKit2 reference on the same signal",
                )
            )

        sustained = float(val_cfg.get("ppg_sustained_s", 30.0))
        enough = self._duration_s >= sustained
        sqi_min = float(motion.get("sqi_min", 0.3))
        map_max = float(motion.get("map_max", 0.5))
        for name, value, ok, threshold in (
            (
                "ppg_sqi",
                q["ppg_sqi"],
                q["ppg_sqi"] is not None and q["ppg_sqi"] >= sqi_min,
                f">= {sqi_min:g} for {sustained:g}s",
            ),
            (
                "ppg_map",
                q["ppg_map"],
                q["ppg_map"] is not None and q["ppg_map"] <= map_max,
                f"<= {map_max:g} for {sustained:g}s",
            ),
        ):
            if value is None:
                gates.append(self._na(name, "no PPG signal quality in this recording", threshold))
            elif not enough:
                gates.append(
                    self._na(
                        name,
                        f"recording is {self._duration_s:g}s, shorter than the "
                        f"{sustained:g}s sustained window",
                        threshold,
                    )
                )
            else:
                gates.append(
                    self._gate(
                        name,
                        ok,
                        round(float(value), 3),
                        threshold,
                        (
                            f"worst of up to 12 {sustained:g}s windows over "
                            f"{self._duration_s:g}s, seated and still"
                        ),
                    )
                )
        return gates

    def _power_gate(self) -> ValidationGateResult:
        """The G1 power row must rest on a measurement, not on a tier model."""
        model_frames = (
            int(self.raw_ingestor.decoder.power_model_frames)
            if self.raw_ingestor is not None
            else 0
        )
        power_seen = bool(
            self.raw_ingestor is not None
            and self.raw_ingestor.decoder.tail(RawStream.POWER, 1).size > 0
        )
        if model_frames:
            ok, value, details = (
                False,
                model_frames,
                (
                    f"{model_frames} POWER frame(s) flagged as tier-model: the "
                    "24 h claim cannot rest on a model"
                ),
            )
        elif self.live_hardware and not power_seen:
            ok, value, details = (
                False,
                0,
                (
                    "no POWER stream in this live recording: measured INA219 current "
                    "is a G1 requirement (the kit carries the monitor)"
                ),
            )
        else:
            ok, value, details = (
                True,
                0,
                "POWER frames carry measured INA219 current"
                if power_seen
                else "no POWER stream on a synthetic run",
            )
        return self._gate("power_telemetry_measured", ok, value, 0, details)

    def _evaluate_gates(
        self,
        loop_results: dict,
        xdf_proof: dict,
        sync_status_t0: dict,
        sync_status_t1: dict,
    ) -> list:
        gates = []

        # Gate 1: XDF zero-drop (write/read fidelity of this file)
        total_dropped = xdf_proof.get("total_dropped", -1)
        gates.append(
            self._gate(
                "xdf_zero_drop",
                total_dropped == 0,
                total_dropped,
                0,
                f"Total dropped samples: {total_dropped}",
            )
        )

        # Gate 2: research stream zero-drop — frames lost in transit. The
        # header stamps a 16-bit sequence per stream, so a hole is *seen*;
        # this gate is the difference between "the file is consistent" and
        # "the data is complete". A recording with a hole is worse than one
        # that stopped, because the hole is silent.
        drops = self.raw_ingestor.drop_summary() if self.raw_ingestor else {}
        rejected = int(self.raw_ingestor.decoder.frames_rejected) if self.raw_ingestor else 0
        dropped_samples = (
            self.raw_ingestor.decoder.dropped_sample_summary() if self.raw_ingestor else {}
        )
        lost_samples = int(sum(dropped_samples.values()))
        lost_frames = int(sum(drops.values()))
        gates.append(
            self._gate(
                "research_stream_zero_loss",
                lost_frames == 0 and lost_samples == 0 and rejected == 0,
                lost_frames,
                0,
                (
                    f"Frames lost in transit: {lost_frames} "
                    f"(per stream: {drops or 'none'}), rejected frames: {rejected}, "
                    f"samples the pod reports as lost: {lost_samples} "
                    f"(per stream: {dropped_samples or 'none'})"
                ),
            )
        )
        # Gate 2: T0 sync (clock model evidence; unproven fails)
        gates.append(self._tier0_sync_gate(sync_status_t0))

        # Gate 3: T1 sync <=1ms residual. No T1 pod means NO EVIDENCE, not a
        # pass: G2's Cerelog precondition must be measured on the bench.
        if not sync_status_t1.get("pods"):
            gates.append(
                self._na("tier1_sync_1ms", "no Tier 1 pod connected: residual unmeasured", 1.0)
            )
        else:
            tier1_ok = True
            max_residual_t1 = 0.0
            for pod_status in sync_status_t1["pods"].values():
                if not pod_status.get("within_tolerance", False):
                    tier1_ok = False
                offset = abs(pod_status.get("offset_ms", 0))
                max_residual_t1 = max(max_residual_t1, offset)
            gates.append(
                self._gate(
                    "tier1_sync_1ms",
                    tier1_ok,
                    round(max_residual_t1, 3),
                    1.0,
                    f"Max residual drift T1: {max_residual_t1:.3f}ms (budget: 1ms)",
                )
            )

        # Gate 4: PPG completeness — expected rate from the LSL declaration.
        nominal = self._nominal_rates()
        counts = self._outlet_sample_counts()
        duration = self._duration_s
        ppg_rate = nominal.get("SYNAPSE_PPG_T0", 0.0)
        expected_ppg = int(ppg_rate * duration)
        ppg_samples = counts.get("SYNAPSE_PPG_T0", 0)
        ppg_completeness = ppg_samples / expected_ppg if expected_ppg > 0 else 0.0
        gates.append(
            self._gate(
                "ppg_completeness",
                ppg_completeness >= 0.8,
                round(ppg_completeness * 100, 1),
                80.0,
                (
                    f"PPG sample completeness: {ppg_completeness * 100:.1f}% "
                    f"({ppg_samples}/{expected_ppg} at nominal {ppg_rate:g} Hz)"
                ),
            )
        )

        # Gate 5: EEG quality (Tier 1). A disabled Cerelog is N/A, not a pass.
        if not (
            self.cerelog_connected
            and self.cerelog_manager
            and self.cerelog_manager._quality_metrics
        ):
            gates.append(
                self._na("eeg_quality_tier1", "Cerelog EEG not connected: quality unmeasured")
            )
        else:
            qm = self.cerelog_manager._quality_metrics
            eeg_flatness = qm.spectral_flatness or 0.0
            eeg_alpha_ratio = qm.alpha_band_ratio or 0.0
            gates.append(
                self._gate(
                    "eeg_quality_tier1",
                    eeg_flatness <= 0.3 and eeg_alpha_ratio >= 0.3,
                    f"flatness={eeg_flatness:.3f}, alpha_ratio={eeg_alpha_ratio:.2f}",
                    "flatness<=0.3, alpha_ratio>=0.3",
                    (
                        f"EEG Tier 1 quality: flatness={eeg_flatness:.3f}, "
                        f"alpha_ratio={eeg_alpha_ratio:.2f}"
                    ),
                )
            )

        # Gate 6: Sample rates within 1% of the declared nominal values.
        outlet_of = {
            "ECG": "SYNAPSE_ECG_T0",
            "IMU": "SYNAPSE_ACC_T0",
            "EEG": "SYNAPSE_EEG_T1",
        }
        measured: dict[str, float] = {}
        for label, outlet in outlet_of.items():
            n = counts.get(outlet, loop_results.get("sample_counts", {}).get(label.lower(), 0))
            measured[label] = n / duration if duration > 0 else 0.0
        rate_checks = {
            label: abs(measured[label] - nominal.get(outlet, 0.0))
            <= 0.01 * nominal.get(outlet, 0.0)
            for label, outlet in outlet_of.items()
            if nominal.get(outlet, 0.0) > 0
        }
        measured_str = ", ".join(f"{label}={measured[label]:.1f}Hz" for label in sorted(measured))
        expected_str = ", ".join(
            f"{label}={nominal.get(outlet, 0.0):g}Hz" for label, outlet in sorted(outlet_of.items())
        )
        if not rate_checks:
            gates.append(
                self._na(
                    "sample_rate_accuracy",
                    "no declared outlet rates to compare against",
                    f"{expected_str} ±1%",
                )
            )
        else:
            gates.append(
                self._gate(
                    "sample_rate_accuracy",
                    all(rate_checks.values()),
                    measured_str,
                    f"{expected_str} ±1% (declared in hardware_bringup.yaml)",
                    f"Measured rates over {duration}s vs declared nominal rates",
                )
            )

        # Gate: the G1 power row must rest on a measurement. A tier model on
        # the POWER stream is a target (5 mW system, 425 mW kit envelope), not
        # evidence for the 24 h claim, so it fails here instead of being
        # averaged into a plausible-looking current.
        gates.append(self._power_gate())

        # The pod's own boot-time configuration and the four cardiac rows the
        # SOP says gate G1. On a synthetic run these are N/A, never free passes.
        gates.append(self._flashed_config_gate())
        gates.extend(self._cardiac_gates())

        return gates

    def _dry_run_report(
        self, duration_s: float, problems: list[str] | None = None
    ) -> Phase1ValidationReport:
        """Build the dry-run report after validating configuration only."""
        problems = problems or []
        gates = [
            ValidationGateResult(
                "config_validation",
                not problems,
                "OK" if not problems else "FAILED",
                "OK",
                "Config structure valid" if not problems else "; ".join(problems),
            )
        ]
        return Phase1ValidationReport(
            run_id="dry-run",
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
            config_path=str(self.config.get("_config_path", "unknown")),
            duration_s=duration_s,
            gates=gates,
            overall_pass=_overall_pass(gates),
            sample_counts={},
            transitions=[],
            xdf_proof={"all_streams_valid": True, "total_dropped": 0},
            tier0_sync_status={},
            tier1_sync_status={},
            power_budget={},
        )

    async def _connect_live_pod(self, bringup_cfg: dict) -> None:
        """Discover, connect and subscribe to the live pod.

        The LSL outlets, clock-sync model and acquisition controller are set
        up for *both* transports: the injected path is the same recording as
        the bleak one, minus the radio.
        """

        def setup_session_state() -> None:
            # Outlets are created only after the serial is known: source_id is
            # immutable once an outlet exists, so creating them any earlier
            # would leave every recording tagged `_synthetic` on a real pod.
            self._create_lsl_outlets()
            self._setup_clock_sync()
            self._setup_acquisition_controller()
            # A fresh session: the pod restarts its sequence numbers, and a
            # reconnect must not be read as ~65533 lost frames per stream.
            self.raw_ingestor.reset_session()

        if self.transport is not None:
            # Injected transport: no bleak, no radio, the same code path.
            self.client = self.transport
            setup_session_state()
            for _name, char_uuid in bringup_cfg["ble"]["characteristics"].items():
                await self.client.start_notify(char_uuid, self._notification_handler)
            self.logger.info("Subscribed to all characteristics (injected transport)")
            await self._calibrate_clock_model()
            return

        # Step 1: Discover and connect to the pod before naming any stream.
        self.device = await self._discover_device()
        if self.device is None:
            raise RuntimeError("No BLE device found")

        self.logger.info(f"Connecting to {self.device.address}...")
        self.client = BleakClient(self.device, timeout=bringup_cfg["ble"]["connect_timeout_s"])
        await self.client.connect()
        self.logger.info("Connected!")
        await self._read_device_serial()
        await self._read_device_config()

        # Step 2: LSL, clock sync, acquisition controller and the decoder.
        setup_session_state()

        # Step 3: Subscribe to sensor notifications
        await self._subscribe_characteristics()
        self.logger.info("Subscribed to all characteristics")

        # Step 5: Calibrate the pod clock before the recording starts, so the
        # very first pushed sample already carries a mapped LSL timestamp
        # instead of hours of host-arrival stamps.
        await self._calibrate_clock_model()

    async def _disconnect_live_pod(self, bringup_cfg: dict) -> None:
        """Stop notifications and disconnect cleanly."""
        self.running = False
        if self.client is None:
            return
        for _name, char_uuid in bringup_cfg["ble"]["characteristics"].items():
            try:
                await self.client.stop_notify(char_uuid)
            except Exception:
                pass

        if self.transport is None:
            await self.client.disconnect()
        self.lsl_manager.close()

    async def _calibrate_clock_model(self, exchanges: int = 6, spacing_s: float = 0.0) -> None:
        """Burst of sync exchanges to pin the pod clock offset at connect.

        The periodic tier-interval exchanges refine the model over the
        session; this burst makes the first minutes usable and lets the sync
        gate report a measured number rather than "unproven". The default
        spacing is zero so CI runs it in milliseconds; live hardware still
        sees real round trips between exchanges.
        """
        for _ in range(exchanges):
            await self._run_sync_exchange()
            if spacing_s > 0:
                await asyncio.sleep(spacing_s)
        status = self.clock_model.status()
        if status.get("calibrated"):
            self.logger.info(
                "Pod clock calibrated: offset %.3f ms, drift %.2f ppm, %s exchanges",
                float(status.get("offset_ms", 0.0)),
                float(status.get("drift_ppm", 0.0)),
                status.get("n_exchanges", 0),
            )
        else:
            self.logger.warning("Pod clock NOT calibrated: sync exchanges were not usable")

    def _sync_interval_s(self) -> float:
        """Tier-0 sync exchange interval, from the manifest's tier budgets."""
        sync_cfg = self.config["bringup"]["sync"]
        return float(sync_cfg["tier_budgets"]["T0"]["sync_interval_s"])

    async def _run_sync_exchange(self) -> None:
        """One BLE SYNC exchange, recorded in the LSL clock domain.

        This is what makes the sync gate measurable: without exchanges the
        pod→hub clock model never calibrates and the recording's
        synchronization is an assumption rather than a measurement. A failed
        exchange is logged and skipped — it must never abort a recording.
        """
        if self.client is None or self.clock_model is None:
            return
        sync_uuid = self.config["bringup"]["ble"]["characteristics"].get("sync")
        if not sync_uuid:
            self.logger.warning("No SYNC characteristic configured; skipping exchange")
            return

        try:
            import struct

            self._sync_sequence += 1
            seq = self._sync_sequence
            lsl_now = self.lsl_clock_fn or (pylsl.local_clock if pylsl is not None else time.time)
            lsl_send_us = int(float(lsl_now()) * 1_000_000)
            request = struct.pack("<Iq", seq, time.monotonic_ns() // 1000)
            await self.client.write_gatt_char(sync_uuid, request)
            response = await self.client.read_gatt_char(sync_uuid)
            lsl_recv_us = int(float(lsl_now()) * 1_000_000)
            received_seq, pod_recv_us, pod_send_us = struct.unpack("<Iqq", response[:20])
            if received_seq == seq:
                self.clock_model.record_exchange(
                    ClockExchange(
                        sequence=received_seq,
                        pod_recv_us=int(pod_recv_us),
                        pod_send_us=int(pod_send_us),
                        host_send_us=lsl_send_us,
                        host_recv_us=lsl_recv_us,
                    )
                )
                # Anchor the per-stream DeviceClocks to the pod's full 64-bit
                # time so frame timestamps stay in the right epoch after a
                # 2^32 µs (~71.6 min) counter wrap.
                if self.raw_ingestor is not None:
                    self.raw_ingestor.decoder.seed_from_sync(int(pod_send_us))
        except _SYNC_EXCHANGE_ERRORS:
            self.logger.debug("Sync exchange failed (non-fatal)", exc_info=True)

    def _build_gate_report(self, report: Phase1ValidationReport) -> dict[str, Any]:
        """Build the SOP gate report: the 10 measurements of PROCUREMENT.md.

        Software-computable measurements are filled from this run; bench
        measurements (current, HR-vs-manual, triage latency) stay ``None``
        with an explicit ``source`` so a half-filled report is visibly a
        half-filled report rather than a green lie.
        """
        counts = self._outlet_sample_counts()
        duration = self._duration_s
        val_cfg = self.config["bringup"]["validation"]
        drops = self.raw_ingestor.drop_summary() if self.raw_ingestor else {}
        rejected = int(self.raw_ingestor.decoder.frames_rejected) if self.raw_ingestor else 0
        dropped_samples = (
            self.raw_ingestor.decoder.dropped_sample_summary() if self.raw_ingestor else {}
        )
        restarts = self.raw_ingestor.decoder.restart_summary() if self.raw_ingestor else {}
        throughput = int(self.raw_ingestor.decoder.estimated_bytes()) if self.raw_ingestor else 0
        mtu_value = getattr(self.client, "mtu", None) if self.client is not None else None

        # One measurement per session, shared with the gates: the report and
        # the verdict must never quote different numbers for the same run.
        quality = self._quality_metrics()
        hr_bpm = quality["hr_bpm"]
        rmssd_ms = quality["rmssd_ms"]
        sqi = quality["ppg_sqi"]
        map_ = quality["ppg_map"]

        # 11: measured Tier-0 power, carried inside the recording itself by
        # the INA219 POWER stream (Architecture.md §55-62: the 24-hour claim
        # rests on per-session measurement, not on a tier model). When the
        # stream is absent the row says so instead of inventing a number.
        power_stats = self._power_stream_stats()

        return {
            "run_id": report.run_id,
            "timestamp": report.timestamp,
            "device_serial": self.device_serial
            or str(self.config["bringup"]["ble"].get("serial") or "").strip()
            or None,
            "calibration_source": self._calibration_source(),
            "duration_s": duration,
            # v2 truth rows (Q3/Q5): who wrote the artifact the report reads
            # from, what the MTU actually negotiated to, and every drop broken
            # out by reason. A rehearsal marks itself, so its numbers can
            # never be presented as a real soak's G1 evidence.
            "artifact": {
                "source": self.artifact.get("source", "unknown"),
                "path": self.artifact.get("path"),
                "sha256": self.artifact.get("sha256"),
                "exit_code": self.artifact.get("exit_code"),
                "labrecorder_cli": self.recorder_cli,
                "labrecorder_version": {
                    "value": None,
                    "source": "LabRecorderCLI offers no --version; the executable is recorded above",
                },
            },
            "artifact_sha256": self.artifact.get("sha256"),
            "rehearsal": bool(self.artifact.get("rehearsal")),
            "mtu": {
                "value": mtu_value if mtu_value is None or int(mtu_value) > 0 else None,
                "source": "ResearchBleClient.mtu (the link's negotiated value; None = not negotiated)",
            },
            "conn_interval_ms": {
                "value": None,
                "source": "bleak does not expose the link interval; measured on the bench",
            },
            "samples_by_stream": counts,
            "frames_rejected": rejected,
            "drops_by_reason": {
                "sequence_gap_frames": drops,
                "pod_reported_samples": dropped_samples,
                "decode_rejected": rejected,
                "reconnect_restarts": restarts,
            },
            "measurements": {
                # 1: bench PSU / power meter, recorded by hand.
                "idle_current_ma": {"value": None, "source": "bench"},
                # 2: bench average over the same session as the soak, by hand.
                # The automatic equivalent below is the POWER-stream row: the
                # recording carries its own consumption measurement.
                "tier0_avg_current_ma": {"value": None, "source": "bench"},
                # 11: measured Tier-0 power from the INA219 POWER stream of
                # THIS recording (empty when the stream is not subscribed).
                "measured_tier0_current_ma": {
                    "value": (power_stats.get("current_ma") or {}).get("mean"),
                    "min": (power_stats.get("current_ma") or {}).get("min"),
                    "max": (power_stats.get("current_ma") or {}).get("max"),
                    "n_samples": power_stats.get("n_samples", 0),
                    "source": (
                        "POWER stream (INA219), decoded from this recording"
                        if power_stats
                        else "POWER stream absent from this recording"
                    ),
                },
                "measured_tier0_power_mw": {
                    "value": (power_stats.get("power_mw") or {}).get("mean"),
                    "min": (power_stats.get("power_mw") or {}).get("min"),
                    "max": (power_stats.get("power_mw") or {}).get("max"),
                    "source": "POWER stream (INA219), decoded from this recording"
                    if power_stats
                    else "POWER stream absent from this recording",
                },
                "measured_battery_pct": {
                    "value": (power_stats.get("battery_pct") or {}).get("mean"),
                    "source": "POWER stream (INA219), decoded from this recording"
                    if power_stats
                    else "POWER stream absent from this recording",
                },
                "power_source": {
                    "value": (
                        "model_kit"
                        if power_stats and power_stats.get("model_frames")
                        else "measured"
                    ),
                    "model_frames": int(power_stats.get("model_frames", 0)),
                    "source": "POWER frame flag bit1 (power_is_model)",
                },
                # 3: BLE throughput of the raw research service.
                "ble_throughput_bytes_per_s": {
                    "value": round(throughput / duration, 1) if duration > 0 else 0.0,
                    "source": "decoder.estimated_bytes",
                },
                # 4: dropped frames — the silent-hole measurement.
                "dropped_frames": {
                    "total": int(sum(drops.values())),
                    "per_stream": drops,
                    "rejected": rejected,
                    "samples_lost": int(sum(dropped_samples.values())),
                    "samples_lost_per_stream": dropped_samples,
                    "source": "ResearchStreamDecoder",
                },
                # 5-6: HR and RMSSD against references, from this recording.
                "hr_mean_bpm": {
                    "value": self._finite(hr_bpm),
                    "source": "synapse24.signal_quality.ecg",
                },
                "rmssd_ms": {
                    "value": self._finite(rmssd_ms),
                    "source": "synapse24.signal_quality.ecg",
                },
                "hr_mae_bpm": {
                    "value": self._finite(
                        None
                        if hr_bpm is None or not val_cfg.get("manual_hr_bpm")
                        else abs(float(hr_bpm) - float(val_cfg["manual_hr_bpm"]))
                    ),
                    "reference": val_cfg.get("manual_hr_bpm"),
                    "source": "manual pulse count + this recording",
                },
                "rmssd_mae_ms": {
                    "value": self._finite(quality.get("rmssd_mae_ms")),
                    "reference": val_cfg.get("ground_truth_ecg_path"),
                    "source": "NeuroKit2 reference R-peaks + this recording",
                },
                "flashed_config": {
                    "value": self.device_config_readback or None,
                    "source": "device config characteristic (bf68d647-...)",
                },
                # 7: PPG quality, seated and still.
                "ppg_sqi": {
                    "value": self._finite(sqi),
                    "source": "synapse24.signal_quality.ppg",
                },
                "ppg_map": {
                    "value": self._finite(map_),
                    "source": "synapse24.signal_quality.ppg",
                },
                # 8: clock residual drift, pod↔host via the SYNC exchange.
                "clock_residual_drift_ms": {
                    "value": report.clock_model_status.get("residual_ms"),
                    "tier": "T0",
                    "budget_ms": 10.0,
                    "source": "PodClockModel",
                },
                # 9: XDF integrity of the recorded file.
                "xdf_integrity": {
                    "all_streams_valid": bool(report.xdf_proof.get("all_streams_valid")),
                    "total_dropped": report.xdf_proof.get("total_dropped"),
                    "path": report.xdf_path,
                    "source": report.xdf_proof.get("source", "synapse24.utils.validate_xdf"),
                },
                # 10: on-device triage latency, from the serial log.
                "triage_latency_ms": {"value": None, "source": "serial log"},
            },
            "sample_counts": counts,
            "gates": [
                {
                    "name": g.name,
                    "passed": g.passed,
                    "value": g.value,
                    "threshold": g.threshold,
                    "details": g.details,
                }
                for g in report.gates
            ],
            "overall_pass": report.overall_pass,
        }

    def _outlet_array(self, outlet: str, channel: int | None = None) -> Any:
        """Buffered samples of one LSL outlet, as a 2-D array.

        The samples come from the on-disk spool, so the array covers the whole
        session and not only the in-memory tail.
        """
        import numpy as np

        arr = self.lsl_manager.stream_array(outlet)
        if arr.size == 0:
            return np.zeros(0)
        if arr.ndim == 1:
            arr = arr.reshape(-1, 1)
        return arr if channel is None else arr[:, channel]

    def _calibration_source(self) -> str | None:
        """The per-unit calibration file used for this session, if any."""
        serial = (
            self.device_serial or str(self.config["bringup"]["ble"].get("serial") or "").strip()
        )
        if not serial:
            return None
        path = ROOT / "data" / "calibration" / f"{serial}.json"
        return str(path.relative_to(ROOT)) if path.is_file() else None

    def _power_stream_stats(self) -> dict[str, Any]:
        """Aggregate the decoded INA219 POWER stream of this recording.

        The firmware packs four channels per frame in the raw stream
        (voltage_mv, current_ma, power_mw, battery_pct); the decoder's unit
        divisor is 1 so these are already engineering units. An empty result
        means the stream was not subscribed — reported, never invented.
        """
        import numpy as np

        if self.raw_ingestor is None:
            return {}
        samples, _timestamps = self.raw_ingestor.decoder.arrays(RawStream.POWER)
        if samples.size == 0:
            return {}
        block = samples.reshape(-1, 4)
        names = ("voltage_mv", "current_ma", "power_mw", "battery_pct")
        stats: dict[str, Any] = {}
        for idx, name in enumerate(names):
            column = block[:, idx]
            stats[name] = {
                "mean": float(np.mean(column)),
                "min": float(np.min(column)),
                "max": float(np.max(column)),
            }
        stats["n_samples"] = int(block.shape[0])
        stats["model_frames"] = int(self.raw_ingestor.decoder.power_model_frames)
        return stats

    @staticmethod
    def _finite(value: Any) -> float | None:
        """JSON-safe float: NaN/Inf (a short window has no HRV) becomes None.

        json.dumps happily writes ``NaN``, which is not valid JSON and breaks
        every strict parser downstream, including the ones reading the SOP
        report after a short run.
        """
        if value is None:
            return None
        try:
            import math

            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    def _log_gate_summary(self, report: Phase1ValidationReport) -> None:
        self.logger.info("=" * 60)
        self.logger.info("PHASE 1 ENTRY GATE RESULT")
        for gate in report.gates:
            status = gate.status or ("PASS" if gate.passed else "FAIL")
            self.logger.info(
                f"  [{status}] {gate.name}: {gate.value} (threshold: {gate.threshold})"
            )
        self.logger.info(f"OVERALL: {'PASS' if report.overall_pass else 'FAIL'}")
        self.logger.info("=" * 60)

    async def run(
        self,
        duration_s: float | None = None,
        dry_run: bool = False,
        legacy_format: bool | None = None,
        record: bool = False,
        lsl_clock_fn: Callable[[], float] | None = None,
        rehearsal: bool = False,
        live_hardware: bool = False,
    ) -> Phase1ValidationReport:
        self.live_hardware = bool(live_hardware) and self.transport is None
        if legacy_format is not None:
            self.legacy_format = legacy_format
        if lsl_clock_fn is not None:
            self.lsl_clock_fn = lsl_clock_fn
        bringup_cfg = self.config["bringup"]
        val_cfg = bringup_cfg["validation"]

        if duration_s is None:
            duration_s = val_cfg["duration_s"]
        # The gate must judge THIS run, not the config's nominal window: a
        # 2-second CI run compared against a 60-second expectation fails
        # every completeness gate there is.
        self._duration_s = float(duration_s)

        self.logger.info("=" * 60)
        self.logger.info("SYNAPSE-24 Phase 1 Entry Gate: Hardware Bringup Validation")
        self.logger.info("=" * 60)

        if dry_run:
            self.logger.info("DRY RUN MODE - validating configuration only")
            # Validate config structure, env expansion, etc. What the setup raises is the
            # verdict: the report used to say PASS whatever happened (it was a constant).
            problems: list[str] = []
            for step in (
                self._create_lsl_outlets,
                self._setup_clock_sync,
                self._setup_acquisition_controller,
            ):
                try:
                    step()
                except Exception as exc:  # noqa: BLE001 - any failure of the setup is the finding
                    problems.append(f"{step.__name__}: {exc}")
            # Skip Cerelog EEG hardware connection in dry-run
            self.cerelog_connected = False
            if problems:
                self.logger.error("Configuration validation FAILED: %s", "; ".join(problems))
            else:
                self.logger.info("Configuration validation PASSED")
            return self._dry_run_report(duration_s, problems)

        if record:
            return await self._run_with_recording(bringup_cfg, duration_s, rehearsal=rehearsal)

        await self._connect_live_pod(bringup_cfg)
        loop_results = await self._run_validation_loop(duration_s)
        await self._disconnect_live_pod(bringup_cfg)

        # Save XDF and verify, then collect results
        xdf_proof, xdf_path = self._save_xdf_and_verify()
        self._record_artifact(xdf_path, source="spool")
        # The XDF is written: the spool files are not needed any more, but
        # the gate report still reads the samples back, so only the handles
        # are released here (the files go with the cleanup at the end of the
        # run).
        self.lsl_manager.spool.close()
        report = self._collect_results(loop_results, xdf_proof)
        report.xdf_path = str(xdf_path) if xdf_path else None
        self._log_gate_summary(report)
        return report

    def _record_artifact(self, path: Path | None, source: str, **extra: Any) -> None:
        """Fingerprint the file this session's report reads back from.

        ``sha256`` is computed for whatever path exists: the report carries a
        fingerprint of a REAL file, never a path that may vanish under
        someone's feet (the rehearsal and the soak both need it, but the
        recovery path needs it just as much).
        """
        from synapse24.utils.labrecorder import sha256_of

        self.artifact = {
            "source": source,
            "path": str(path) if path else None,
            "sha256": sha256_of(Path(path)) if path and Path(path).exists() else None,
            **extra,
        }

    def _verify_recorded_xdf(self, path: Path) -> dict[str, Any]:
        """Validate the file LabRecorder wrote — the one the report keeps.

        The soak used to fingerprint the recorder's file but run the zero-drop
        gate on a second XDF rebuilt from the spool. Two writers, two files,
        and the gated one was not the artifact. Integrity now belongs to the
        artifact itself; the spool stays recovery memory for live metrics.
        """
        try:
            summary = validate_xdf(path)
            valid = bool(summary["validation"]["all_streams_valid"])
            recovered = sum(int(stream["n_samples"]) for stream in summary["streams"])
            return {
                "file": str(path),
                "n_streams": len(summary["streams"]),
                "all_streams_valid": valid,
                "total_expected": recovered,
                "total_recovered": recovered,
                "total_dropped": 0 if valid else -1,
                "per_stream": summary["streams"],
                "source": "LabRecorderCLI file, validated by synapse24.utils.validate_xdf",
            }
        except Exception:  # noqa: BLE001 - a corrupt artifact must fail a gate
            self.logger.exception("The recorded XDF could not be validated")
            return {
                "file": str(path),
                "n_streams": 0,
                "all_streams_valid": False,
                "total_expected": 0,
                "total_recovered": 0,
                "total_dropped": -1,
                "per_stream": [],
                "source": "LabRecorderCLI file, validation failed",
            }

    async def _run_with_recording(
        self,
        bringup_cfg: dict,
        duration_s: float,
        rehearsal: bool = False,
    ) -> Phase1ValidationReport:
        """Run the gate with LabRecorderCLI writing the continuous XDF.

        The in-memory buffer path is fine for a one-minute gate; a 6-hour
        soak is not (10.8M ECG samples in Python lists, and nothing on disk
        if the process dies). The recorder is trusted to write, never to
        prove: integrity stays with validate_xdf on the file it produced.

        In rehearsal mode (``--soak-rehearsal``) the same code path runs
        without the recorder: the artifact is the deterministic spool XDF and
        the report marks itself as a rehearsal, so it can never be confused
        with the G1 evidence the real soak produces.
        """
        val_cfg = bringup_cfg["validation"]
        record_dir = Path(val_cfg["xdf_output_dir"]) / "recordings"

        if rehearsal:
            self.recorder_cli = None
            await self._connect_live_pod(bringup_cfg)
            loop_results = await self._run_validation_loop(duration_s)
            await self._disconnect_live_pod(bringup_cfg)
            xdf_proof, xdf_path = self._save_xdf_and_verify()
            self._record_artifact(xdf_path, source="spool (synthetic rehearsal)", rehearsal=True)
            self.lsl_manager.spool.close()
            report = self._collect_results(loop_results, xdf_proof)
            report.xdf_path = str(xdf_path) if xdf_path else None
            self._log_gate_summary(report)
            return report

        from synapse24.utils.labrecorder import (
            artifacts_of,
            find_labrecorder_cli,
            record_session,
        )

        cli = find_labrecorder_cli()
        self.recorder_cli = str(cli) if cli else None
        # The exceptions carry their own recovery_hint (error contract);
        # logging here would only duplicate it. Raise, don't log-and-throw.
        with record_session(
            record_dir, stream_specs=val_cfg.get("recorder_streams", [])
        ) as session:
            # Fail fast when the recorder cannot actually run: a CLI that
            # dies during startup used to leave the gate reading the
            # recovery spool for six hours while pretending to soak.
            session.wait_ready()
            await self._connect_live_pod(bringup_cfg)
            loop_results = await self._run_validation_loop(duration_s)
            await self._disconnect_live_pod(bringup_cfg)

        # Primary artifact: the continuous recording, fingerprinted AFTER the
        # recorder stopped (a recorder that flushes on close must have its
        # bytes counted too). A completed session with no file never gets
        # here: record_session raises in that case.
        recorded = artifacts_of(record_dir)
        self._record_artifact(
            recorded[0],
            source="LabRecorderCLI",
            exit_code=session.process.returncode,
        )
        xdf_proof = self._verify_recorded_xdf(recorded[0])
        report = self._collect_results(loop_results, xdf_proof)
        report.xdf_path = str(recorded[0])
        self._log_gate_summary(report)
        return report

    async def _discover_device(self) -> BLEDevice | None:
        """Bind to ONE band.

        Taking the first 'SYNAPSE' name is a data bug: two pods on the bench
        would produce an XDF attributed to the wrong unit.
        """
        ble_cfg = self.config["bringup"]["ble"]
        address = ble_cfg["address"]

        if address not in ("", "auto"):
            self.logger.info(f"Connecting to known address: {address}")
            return await BleakScanner.find_device_by_address(
                address, timeout=ble_cfg["scan_timeout_s"]
            )

        self.logger.info("Scanning for SYNAPSE device...")
        devices = await BleakScanner.discover(timeout=ble_cfg["scan_timeout_s"])
        candidates = [d for d in devices if d.name and "SYNAPSE" in d.name.upper()]
        for d in candidates:
            self.logger.info(f"Candidate SYNAPSE device: {d.name} ({d.address})")

        expected = str(ble_cfg.get("serial") or "").strip()
        if expected and candidates:
            matched: list[BLEDevice] = []
            for d in candidates:
                try:
                    async with BleakClient(d, timeout=ble_cfg["connect_timeout_s"]) as client:
                        raw = await client.read_gatt_char(DIS_SERIAL_UUID)
                        serial = bytes(raw).decode("ascii", errors="replace").strip("\x00 ")
                    self.logger.info(f"  {d.address}: serial {serial}")
                    if serial == expected:
                        matched.append(d)
                        self.device_serial = serial
                except BleakError as exc:
                    self.logger.warning(f"  {d.address}: cannot read serial: {exc}")
            candidates = matched

        if not candidates:
            self.logger.warning("No SYNAPSE device found in scan (or no serial match)")
            return None
        if len(candidates) > 1:
            details = ", ".join(f"{d.name} ({d.address})" for d in candidates)
            self.logger.error(
                f"Ambiguous BLE scan: {len(candidates)} SYNAPSE devices: {details}. "
                "Set bringup.ble.serial or SYNAPSE_FOREARM_BLE."
            )
            return None
        chosen = candidates[0]
        self.logger.info(f"Bound to {chosen.name} ({chosen.address})")
        return chosen

    async def _read_device_serial(self) -> None:
        """Read DIS 2A25 after connect; the record travels into the XDF metadata."""
        if self.client is None or self.transport is not None:
            return
        serial = ""
        try:
            raw = await self.client.read_gatt_char(DIS_SERIAL_UUID)
            serial = bytes(raw).decode("ascii", errors="replace").strip("\x00 ")
        except Exception as exc:  # noqa: BLE001 - a missing DIS must not kill the session
            self.logger.warning(f"DIS serial number unavailable: {exc}")
        if not serial:
            return
        expected = str(self.config["bringup"]["ble"].get("serial") or "").strip()
        if expected and serial != expected:
            raise RuntimeError(f"connected to serial {serial}, expected {expected}")
        self.device_serial = serial
        self.logger.info(f"Device serial: {serial}")

    async def _read_device_config(self) -> None:
        """Read the pod's own boot-time configuration snapshot.

        A pod flashed from a stale ``sdkconfig`` used to report nominal rates
        and gain while actually running different ones. The firmware fills
        this characteristic from its live configuration; the gate compares it
        with the manifest, so a mismatch fails the run instead of producing a
        beautifully scaled but wrong dataset.
        """
        if self.client is None or self.transport is not None:
            return
        try:
            raw = await self.client.read_gatt_char(DEVICE_CONFIG_UUID)
            payload = bytes(raw).decode("ascii", errors="replace").strip("\x00 ")
            parsed = json.loads(payload) if payload else {}
            if isinstance(parsed, dict):
                self.device_config_readback = parsed
                self.logger.info(f"Device config readback: {parsed}")
        except Exception as exc:  # noqa: BLE001 - the gate reports the missing proof
            self.logger.warning(f"Device config readback unavailable: {exc}")

    def _feed_downstream_from_raw(self, timestamp: float) -> None:
        """Feed clock sync and the acquisition controller from raw frames.

        The ingestor already pushed samples into LSL; this mirrors the side
        effects the legacy parser performed inline (accelerometer magnitude into
        the sync model and the motion gate), so the rest of the gate behaves
        identically whichever wire format the pod speaks.
        """
        if self.clock_sync is None and self.controller is None:
            return

        acc = self.raw_ingestor.decoder.tail(RawStream.ACC, 3)
        if acc.size < 3:
            return

        acc_mag = float(np.linalg.norm(acc[-3:]))
        if self.clock_sync:
            self.clock_sync.add_hub_acc(acc_mag, timestamp)
            self.clock_sync.add_pod_acc("forearm_hub", acc_mag, timestamp)
        if self.controller:
            self.controller.update_imu(accel_magnitude=acc_mag, timestamp=timestamp)

        self._feed_motion_quality(timestamp)

    def _feed_motion_quality(self, timestamp: float) -> None:
        """Feed the measured PPG quality into the promotion gate.

        The motion gate is armed by *measured* signal quality
        (Architecture.md §74). Without this feed the gate never arms, the
        T0 -> T1 promotion is unreachable, and the tiered acquisition is
        decorative: the state machine stays in Tier 0 for the whole session.

        The decoder returns the samples *interleaved* (PPG: red, IR, red,
        IR...), so the window is de-interleaved before the quality metrics
        see it: a mixed red/IR trace puts a strong component at fs/2, outside
        the cardiac band, and the SQI of a perfectly clean signal collapses.
        """
        if self.controller is None:
            return
        rate_hz = float(self._nominal_rates().get("SYNAPSE_PPG_T0", 50.0) or 50.0)
        n_samples = int(rate_hz * 10)  # ten seconds of samples
        # Two values per sample (red + IR).
        values = self.raw_ingestor.decoder.tail(RawStream.PPG, 2 * n_samples)
        if values.size < 64:
            return
        window = values[0::2]  # the red channel
        acc_values = self.raw_ingestor.decoder.tail(RawStream.ACC, 3 * window.shape[0])
        acc_mag = None
        if acc_values.size >= 3 * window.shape[0]:
            # The decoder returns the samples flat: the accelerometer is
            # 3-axis, so the tail is reshaped before the magnitude.
            acc_tail = np.asarray(acc_values).reshape(-1, 3)
            acc_mag = np.linalg.norm(acc_tail, axis=1)
        try:
            quality = compute_ppg_quality(
                np.asarray(window, dtype=np.float64),
                int(rate_hz),
                accel_magnitude=acc_mag,
            )
        except Exception:  # pragma: no cover - depends on the signal
            self.logger.debug("PPG quality computation failed", exc_info=True)
            return
        self.controller.update_motion_quality(
            ppg_sqi=quality.get("ppg_sqi"),
            motion_artifact_prob=quality.get("motion_artifact_prob"),
            timestamp=timestamp,
        )

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

    mode_errors: list[str] = []
    if args.soak and args.soak_rehearsal:
        mode_errors.append(
            "--soak and --soak-rehearsal are mutually exclusive: a rehearsal "
            "is, by definition, not a soak"
        )
    if args.soak and not args.live_hardware:
        mode_errors.append(
            "--soak records continuously and is meant for live hardware; add --live-hardware"
        )
    if args.soak_rehearsal and args.live_hardware:
        mode_errors.append(
            "--soak-rehearsal is a synthetic rehearsal and cannot be a live-hardware run"
        )
    if mode_errors:
        for message in mode_errors:
            logger.error(message)
        return 2

    validator = Phase1Validator(config_path, logger)

    try:
        report = await validator.run(
            duration_s=args.duration,
            dry_run=args.dry_run,
            legacy_format=args.legacy_format,
            record=args.soak,
            rehearsal=args.soak_rehearsal,
            live_hardware=args.live_hardware,
        )

        # Save report
        output_dir = (
            Path(args.output_dir) if args.output_dir else Path("data/processed/phase1_validation")
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / f"phase1_validation_{report.run_id}.json"
        report_path.write_text(report.to_json())
        logger.info(f"Report saved to: {report_path}")

        # SOP evidence: the ten measurements PROCUREMENT.md gates G1 on.
        gate_report = validator._build_gate_report(report)
        args.gate_report.parent.mkdir(parents=True, exist_ok=True)
        args.gate_report.write_text(json.dumps(gate_report, indent=2, default=str))
        logger.info(f"SOP gate report saved to: {args.gate_report}")

        # Print summary
        print(f"\n{'=' * 60}")
        print(f"PHASE 1 ENTRY GATE: {'PASS' if report.overall_pass else 'FAIL'}")
        print(f"{'=' * 60}")
        for gate in report.gates:
            status = gate.status or ("PASS" if gate.passed else "FAIL")
            print(f"  [{status}] {gate.name}: {gate.value} (threshold: {gate.threshold})")
        print(f"{'=' * 60}")

        return 0 if report.overall_pass else 1

    except Exception:
        logger.exception("Validation failed")
        return 2
    finally:
        # The samples are on disk: release them (a 6-hour session leaves
        # hundreds of megabytes behind).
        validator.lsl_manager.spool.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description="SYNAPSE-24 Phase 1 Entry Gate Validation")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/hardware_bringup.yaml"),
        help="Path to hardware bringup config",
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
        "--live-hardware",
        action="store_true",
        help=(
            "Run the entry gate against live hardware. This is the mode the "
            "bringup SOP documents; without it the script still streams, but "
            "the flag makes the intent (and the prerequisite checks) explicit."
        ),
    )
    parser.add_argument(
        "--soak",
        action="store_true",
        help=(
            "Soak mode: record continuously via LabRecorderCLI and write "
            "data/live_validation/gate_report.json at the end. Requires "
            "--live-hardware semantics; use with a multi-hour --duration."
        ),
    )
    parser.add_argument(
        "--soak-rehearsal",
        action="store_true",
        help=(
            "Rehearse the soak path without the recorder: same validation "
            "loop and v2 gate report, artifact = the deterministic spool XDF "
            "marked as a synthetic rehearsal. Proves the machinery in CI; it "
            "is never the G1 evidence (that requires --soak)."
        ),
    )
    parser.add_argument(
        "--legacy-format",
        action="store_true",
        help=(
            "DEPRECATED: accept the legacy Nordic-UART sensor_sample_t wire "
            "format (default: research service only; unrecognised payloads are "
            "reported, never silently mis-parsed). Kept for pods that still "
            "run the old firmware; it is removed once the research service is "
            "validated on the bench."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory for validation report",
    )
    parser.add_argument(
        "--gate-report",
        type=Path,
        default=Path("data/live_validation/gate_report.json"),
        help="Where to write the SOP gate report (default: data/live_validation/gate_report.json)",
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
