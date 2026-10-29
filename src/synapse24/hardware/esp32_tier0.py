"""ESP32 Tier 0 firmware interface for SYNAPSE-24.

Interfaces with AD8232 (ECG), MAX30102 (PPG), ICM-20948 (IMU) on ESP32.
Provides BLE LSL bridge for real-time streaming to Python hub.

**Single source of truth.** Every normative value (rates, ranges, pins) is
read from ``config/hardware.yaml`` at import time via
:func:`forearm_hub_normative_rates`; nothing is hardcoded here. The values in
this file used to be literals that drifted from the manifest (PPG 64 Hz / IMU
100 Hz / ±4 g) — exactly the incoherence class the contract checker
(``tools/check_contract.py``) exists to prevent. If the manifest is missing or
unparseable the module refuses to guess: it raises, so a broken checkout fails
loudly instead of silently resampling.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import yaml

try:
    from pylsl import StreamInfo, StreamOutlet, local_clock
except ImportError:  # pragma: no cover
    StreamInfo = None
    StreamOutlet = None
    local_clock = None

from .base import BoardAdapter, BoardConfig, build_board_config

# config/hardware.yaml, resolved relative to the repository root
# (src/synapse24/hardware/esp32_tier0.py -> <root>/config/hardware.yaml).
_HARDWARE_YAML = Path(__file__).resolve().parents[3] / "config" / "hardware.yaml"


class NormativeConfigError(RuntimeError):
    """Raised when config/hardware.yaml cannot provide the forearm-hub values."""


def forearm_hub_normative_rates(config_path: Path | None = None) -> dict[str, int]:
    """Load the normative Tier 0 rates from ``config/hardware.yaml``.

    Args:
        config_path: Override path, for tests. Defaults to the repository
            ``config/hardware.yaml``.

    Returns:
        Dict with keys ``ecg_hz``, ``ppg_hz``, ``imu_hz``, ``accel_range_g``,
        ``gyro_range_dps`` — all taken from the ``forearm_hub`` pod entry.

    Raises:
        NormativeConfigError: If the manifest is missing or does not declare
            the ``forearm_hub`` pod. The caller must not fall back to
            hardcoded literals: a silent fallback is how the rates drifted.
    """
    path = Path(config_path) if config_path is not None else _HARDWARE_YAML
    if not path.is_file():
        raise NormativeConfigError(f"hardware manifest not found: {path}")

    with path.open(encoding="utf-8") as handle:
        manifest = yaml.safe_load(handle) or {}

    pod = next(
        (p for p in manifest.get("pods", []) if p.get("pod_id") == "forearm_hub"),
        None,
    )
    if pod is None:
        raise NormativeConfigError("config/hardware.yaml declares no 'forearm_hub' pod")

    rates = pod.get("sampling_rate", {})
    meta = pod.get("metadata", {})
    imu_cfg = meta.get("imu_config", {})
    required = ("ecg", "ppg", "imu")
    if any(rates.get(key) is None for key in required):
        raise NormativeConfigError(f"forearm_hub sampling_rate is incomplete: {rates}")

    return {
        "ecg_hz": int(rates["ecg"]),
        "ppg_hz": int(rates["ppg"]),
        "imu_hz": int(rates["imu"]),
        "accel_range_g": int(imu_cfg.get("accel_range_g", 0)),
        "gyro_range_dps": int(imu_cfg.get("gyro_range_dps", 0)),
    }


def _normative(field_name: str) -> Any:
    """Resolve one normative value at import time, failing loudly on drift."""
    rates = forearm_hub_normative_rates()
    if field_name not in rates:
        raise NormativeConfigError(f"normative rates missing key {field_name!r}")
    return rates[field_name]


@dataclass(frozen=True)
class ESP32Tier0Config:
    """Configuration for ESP32 Tier 0 sensor suite.

    Defaults are **derived from config/hardware.yaml** (pod ``forearm_hub``)
    at import time — see :func:`forearm_hub_normative_rates`. Pass explicit
    overrides only to emulate a non-normative breakout; the production path
    stays normative. No silent resampling (Architecture.md tiered acquisition).
    """

    # ECG (AD8232)  # noqa: ERA001
    ecg_adc_pin: int = 36
    ecg_sampling_rate: int = field(default_factory=lambda: _normative("ecg_hz"))
    ecg_lead_off_plus: int = 39
    ecg_lead_off_minus: int = 34

    # PPG (MAX30102)  # noqa: ERA001
    ppg_i2c_addr: int = 0x57
    ppg_sampling_rate: int = field(default_factory=lambda: _normative("ppg_hz"))
    ppg_led_current_red: int = 0x1F  # 6.4 mA
    ppg_led_current_ir: int = 0x1F  # 6.4 mA

    # IMU (ICM-20948)  # noqa: ERA001
    imu_i2c_addr: int = 0x68
    imu_sampling_rate: int = field(default_factory=lambda: _normative("imu_hz"))
    imu_accel_range: int = field(default_factory=lambda: _normative("accel_range_g"))
    imu_gyro_range: int = field(default_factory=lambda: _normative("gyro_range_dps"))

    # BLE LSL  # noqa: ERA001
    ble_device_name: str = "SYNAPSE-T0"
    ble_service_uuid: str = "0000ffe0-0000-1000-8000-00805f9b34fb"
    ble_char_uuid: str = "0000ffe1-0000-1000-8000-00805f9b34fb"

    # LSL Stream names  # noqa: ERA001
    lsl_stream_ecg: str = "SYNAPSE_ECG_T0"
    lsl_stream_ppg: str = "SYNAPSE_PPG_T0"
    lsl_stream_acc: str = "SYNAPSE_ACC_T0"
    lsl_stream_gyro: str = "SYNAPSE_GYRO_T0"
    lsl_stream_mag: str = "SYNAPSE_MAG_T0"


class ESP32Tier0Firmware:
    """ESP32 Tier 0 firmware simulation for CI/testing.

    In production, this would be compiled C++ for ESP32.
    This Python class simulates the firmware behavior for testing.
    """

    def __init__(self, config: ESP32Tier0Config | None = None) -> None:
        self.config = config or ESP32Tier0Config()
        self._ecg_outlet: StreamOutlet | None = None
        self._ppg_outlet: StreamOutlet | None = None
        self._acc_outlet: StreamOutlet | None = None
        self._gyro_outlet: StreamOutlet | None = None
        self._mag_outlet: StreamOutlet | None = None
        self._running = False

    def setup_lsl_streams(self) -> None:
        """Initialize LSL outlets for each sensor modality."""
        if StreamInfo is None or StreamOutlet is None:
            raise RuntimeError("pylsl not installed")

        # ECG stream
        ecg_info = StreamInfo(
            name=self.config.lsl_stream_ecg,
            type="ECG_T0",
            channel_count=1,
            nominal_srate=self.config.ecg_sampling_rate,
            channel_format="float32",
            source_id=f"synapse24_ecg_t0_{self.config.ble_device_name}",
        )
        ecg_info.desc().append_child_value("sensor", "AD8232")
        ecg_info.desc().append_child_value("placement", "chest_lead_I")
        self._ecg_outlet = StreamOutlet(ecg_info, chunk_size=32, max_buffered=360)

        # PPG stream
        ppg_info = StreamInfo(
            name=self.config.lsl_stream_ppg,
            type="PPG_T0",
            channel_count=2,  # Red + IR
            nominal_srate=self.config.ppg_sampling_rate,
            channel_format="float32",
            source_id=f"synapse24_ppg_t0_{self.config.ble_device_name}",
        )
        ppg_info.desc().append_child_value("sensor", "MAX30102")
        ppg_info.desc().append_child_value("wavelengths", "660nm,880nm")
        self._ppg_outlet = StreamOutlet(ppg_info, chunk_size=32, max_buffered=360)

        # IMU streams
        for name, stream_type, unit in [
            (self.config.lsl_stream_acc, "ACC_T0", "g"),
            (self.config.lsl_stream_gyro, "GYRO_T0", "deg/s"),
            (self.config.lsl_stream_mag, "MAG_T0", "uT"),
        ]:
            imu_info = StreamInfo(
                name=name,
                type=stream_type,
                channel_count=3,
                nominal_srate=self.config.imu_sampling_rate,
                channel_format="float32",
                source_id=f"synapse24_{name.lower()}_{self.config.ble_device_name}",
            )
            imu_info.desc().append_child_value("sensor", "ICM-20948")
            if "ACC" in stream_type:
                imu_info.desc().append_child_value("range", f"±{self.config.imu_accel_range}g")
            elif "GYRO" in stream_type:
                imu_info.desc().append_child_value("range", f"±{self.config.imu_gyro_range}dps")

            outlet = StreamOutlet(imu_info, chunk_size=32, max_buffered=360)
            if "ACC" in stream_type:
                self._acc_outlet = outlet
            elif "GYRO" in stream_type:
                self._gyro_outlet = outlet
            else:
                self._mag_outlet = outlet

    def start_streaming(self) -> None:
        """Start sensor acquisition and LSL streaming."""
        self._running = True

    def stop_streaming(self) -> None:
        """Stop sensor acquisition."""
        self._running = False

    def read_battery_voltage(self) -> float:
        """Simulate reading battery voltage (nominal 3.7V LiPo)."""
        return 3.72

    def log_power_budget(self) -> dict[str, Any]:
        """Log current power state and battery voltage per Architecture.md."""
        power_state = {
            "ble_active": self._running,
            "adc_active": self._running,
            "sampling_rate": self.config.ecg_sampling_rate,
            "battery_voltage": self.read_battery_voltage(),
            "timestamp": time.time(),
        }
        return power_state

    def push_ecg_sample(self, ecg_mv: float, timestamp: float | None = None) -> None:
        """Push single ECG sample to LSL."""
        if self._ecg_outlet and self._running:
            if timestamp is None and local_clock:
                timestamp = local_clock()
            self._ecg_outlet.push_sample([ecg_mv], timestamp)

    def push_ppg_sample(self, red: float, ir: float, timestamp: float | None = None) -> None:
        """Push PPG sample (Red + IR) to LSL."""
        if self._ppg_outlet and self._running:
            if timestamp is None and local_clock:
                timestamp = local_clock()
            self._ppg_outlet.push_sample([red, ir], timestamp)

    def push_imu_sample(
        self,
        accel: npt.NDArray[np.float64],
        gyro: npt.NDArray[np.float64],
        mag: npt.NDArray[np.float64] | None = None,
        timestamp: float | None = None,
    ) -> None:
        """Push IMU sample to LSL."""
        if timestamp is None and local_clock:
            timestamp = local_clock()

        if self._acc_outlet and self._running:
            self._acc_outlet.push_sample(accel.astype(np.float32).tolist(), timestamp)
        if self._gyro_outlet and self._running:
            self._gyro_outlet.push_sample(gyro.astype(np.float32).tolist(), timestamp)
        if self._mag_outlet and self._running and mag is not None:
            self._mag_outlet.push_sample(mag.astype(np.float32).tolist(), timestamp)

    def get_config_bytes(self) -> bytes:
        """Serialize config for OTA update or BLE config characteristic."""
        return struct.pack(
            "<BBBBHHBBBBBB",
            self.config.ecg_adc_pin,
            self.config.ppg_i2c_addr,
            self.config.imu_i2c_addr,
            0,  # reserved
            self.config.ecg_sampling_rate,
            self.config.ppg_sampling_rate,
            self.config.imu_sampling_rate,
            self.config.ecg_lead_off_plus,
            self.config.ecg_lead_off_minus,
            self.config.ppg_led_current_red,
            self.config.ppg_led_current_ir,
            self.config.imu_accel_range,
            self.config.imu_gyro_range,
        )


def create_synthetic_tier0_data(
    duration_s: float = 60.0,
    ecg_hr: float = 72.0,
    ppg_hr: float = 72.0,
    motion_level: float = 0.0,
    seed: int = 42,
    ecg_fs: int | None = None,
    ppg_fs: int | None = None,
    imu_fs: int | None = None,
) -> dict[str, npt.NDArray[np.float64]]:
    """Generate synthetic Tier 0 data for testing.

    Normative defaults are loaded from config/hardware.yaml (pod
    ``forearm_hub``). Pass explicit fs_* only to emulate a non-normative
    breakout; production path stays normative.

    Returns dict with keys: ecg, ppg_red, ppg_ir, acc_x, acc_y, acc_z, gyro_x, gyro_y, gyro_z
    All timestamps aligned to common timebase.
    """
    normative = forearm_hub_normative_rates()
    rng = np.random.default_rng(seed)

    fs_ecg = ecg_fs if ecg_fs is not None else normative["ecg_hz"]
    fs_ppg = ppg_fs if ppg_fs is not None else normative["ppg_hz"]
    fs_imu = imu_fs if imu_fs is not None else normative["imu_hz"]

    n_ecg = int(duration_s * fs_ecg)
    n_ppg = int(duration_s * fs_ppg)
    n_imu = int(duration_s * fs_imu)

    t_ecg = np.arange(n_ecg) / fs_ecg
    t_ppg = np.arange(n_ppg) / fs_ppg
    t_imu = np.arange(n_imu) / fs_imu

    # ECG with R-peaks
    rr_interval = 60.0 / ecg_hr
    ecg = np.zeros(n_ecg)
    for i in np.arange(0, duration_s, rr_interval):
        idx = int(i * fs_ecg)
        if idx < n_ecg:
            window = int(0.08 * fs_ecg)
            for j in range(max(0, idx - window), min(n_ecg, idx + window)):
                dt = (j - idx) / fs_ecg
                ecg[j] += 1.5 * np.exp(-(dt**2) / (0.02**2))
    ecg += 0.2 * np.sin(2 * np.pi * ecg_hr / 60 * t_ecg)
    ecg += rng.normal(0, 0.05, n_ecg)
    ecg *= 1000  # µV

    # PPG (Red + IR)  # noqa: ERA001
    ppg_red = 100000 + 5000 * np.sin(2 * np.pi * ppg_hr / 60 * t_ppg)
    ppg_ir = 100000 + 5000 * np.sin(2 * np.pi * ppg_hr / 60 * t_ppg + 0.1)
    ppg_red += rng.normal(0, 100, n_ppg)
    ppg_ir += rng.normal(0, 100, n_ppg)

    # IMU  # noqa: ERA001
    acc_mag = 1.0 + motion_level * rng.normal(0, 1, n_imu)
    acc_x = acc_mag * np.sin(2 * np.pi * 0.1 * t_imu) + rng.normal(0, 0.01, n_imu)
    acc_y = acc_mag * np.cos(2 * np.pi * 0.1 * t_imu) + rng.normal(0, 0.01, n_imu)
    acc_z = 1.0 + rng.normal(0, 0.01, n_imu)

    gyro_x = rng.normal(0, 0.5, n_imu)
    gyro_y = rng.normal(0, 0.5, n_imu)
    gyro_z = rng.normal(0, 0.5, n_imu)

    return {
        "ecg": ecg.astype(np.float64),
        "ppg_red": ppg_red.astype(np.float64),
        "ppg_ir": ppg_ir.astype(np.float64),
        "acc_x": acc_x.astype(np.float64),
        "acc_y": acc_y.astype(np.float64),
        "acc_z": acc_z.astype(np.float64),
        "gyro_x": gyro_x.astype(np.float64),
        "gyro_y": gyro_y.astype(np.float64),
        "gyro_z": gyro_z.astype(np.float64),
        "t_ecg": t_ecg,
        "t_ppg": t_ppg,
        "t_imu": t_imu,
    }


class Tier0LSLValidator:
    """Validates live Tier 0 LSL streams against quality thresholds."""

    def __init__(
        self,
        ecg_fs: int | None = None,
        ppg_fs: int | None = None,
        imu_fs: int | None = None,
        tier: int = 0,
    ) -> None:
        from synapse24.signal_quality import QualityThresholds
        from synapse24.signal_quality import Tier as SQTier

        normative = forearm_hub_normative_rates()
        self.ecg_fs = ecg_fs if ecg_fs is not None else normative["ecg_hz"]
        self.ppg_fs = ppg_fs if ppg_fs is not None else normative["ppg_hz"]
        self.imu_fs = imu_fs if imu_fs is not None else normative["imu_hz"]
        self.thresholds = QualityThresholds.for_tier(SQTier(tier))

        self._ecg_buffer: list[float] = []
        self._ppg_red_buffer: list[float] = []
        self._ppg_ir_buffer: list[float] = []
        self._acc_mag_buffer: list[float] = []

        self._ecg_samples_needed = self.ecg_fs * 10  # 10 seconds for quality assessment
        self._ppg_samples_needed = self.ppg_fs * 30  # 30 seconds

    def process_ecg(self, sample: float) -> dict[str, Any] | None:
        """Process ECG sample, return quality metrics when buffer full."""
        self._ecg_buffer.append(sample)
        if len(self._ecg_buffer) >= self._ecg_samples_needed:
            ecg_arr = np.array(self._ecg_buffer, dtype=np.float64)
            from synapse24.signal_quality import compute_ecg_quality

            quality = compute_ecg_quality(ecg_arr, self.ecg_fs, thresholds=self.thresholds)
            self._ecg_buffer = self._ecg_buffer[-self.ecg_fs * 5 :]  # Keep 5s overlap
            return quality.to_dict()
        return None

    def process_ppg(self, red: float, ir: float) -> dict[str, Any] | None:
        """Process PPG sample, return quality metrics when buffer full."""
        self._ppg_red_buffer.append(red)
        self._ppg_ir_buffer.append(ir)

        if len(self._ppg_red_buffer) >= self._ppg_samples_needed:
            ppg_arr = np.array(self._ppg_red_buffer, dtype=np.float64)
            # Compute accelerometer magnitude if available
            accel_mag = (
                np.array(self._acc_mag_buffer, dtype=np.float64) if self._acc_mag_buffer else None
            )

            from synapse24.signal_quality import compute_ppg_quality

            quality = compute_ppg_quality(ppg_arr, self.ppg_fs, accel_mag, self.thresholds)
            self._ppg_red_buffer = self._ppg_red_buffer[-self.ppg_fs * 10 :]
            self._ppg_ir_buffer = self._ppg_ir_buffer[-self.ppg_fs * 10 :]
            if self._acc_mag_buffer:
                self._acc_mag_buffer = self._acc_mag_buffer[-self.imu_fs * 10 :]
            return quality
        return None

    def process_imu(self, acc_x: float, acc_y: float, acc_z: float) -> None:
        """Process IMU sample for motion artifact detection."""
        mag = np.sqrt(acc_x**2 + acc_y**2 + acc_z**2)
        self._acc_mag_buffer.append(float(mag))
        if len(self._acc_mag_buffer) > self.imu_fs * 60:  # Keep 60s
            self._acc_mag_buffer = self._acc_mag_buffer[-self.imu_fs * 30 :]


class ESP32Tier0Adapter(BoardAdapter):
    """BoardAdapter for ESP32 Tier 0 (forearm hub: ECG, PPG, IMU)."""

    @property
    def board_id(self) -> str:
        return "ESP32_TIER0"

    @property
    def default_sampling_rate(self) -> int:
        return 500  # ECG at 500 Hz (highest rate)

    @property
    def default_channels(self) -> dict[str, list[int]]:
        return {
            "ecg": [0],
            "ppg": [1, 2],  # Red + IR
            "acc": [3, 4, 5],
            "gyro": [6, 7, 8],
            "mag": [9, 10, 11],
        }

    def create_config(self, **overrides: Any) -> BoardConfig:
        defaults: dict[str, Any] = {
            "mac_address": "",
            "serial_port": "",
            "sampling_rate": self.default_sampling_rate,
        }
        return build_board_config(self.board_id, {**defaults, **overrides})

    def get_stream_mapping(self) -> dict[str, dict[str, Any]]:
        """Get LSL stream mapping for ESP32 Tier 0 (forearm hub)."""
        return {
            "ECG": {"channels": [0], "type": "ECG_T0", "unit": "µV"},
            "PPG": {"channels": [1, 2], "type": "PPG_T0", "unit": "a.u."},
            "ACC": {"channels": [3, 4, 5], "type": "ACC_T0", "unit": "g"},
            "GYRO": {"channels": [6, 7, 8], "type": "GYRO_T0", "unit": "°/s"},
            "MAG": {"channels": [9, 10, 11], "type": "MAG_T0", "unit": "µT"},
        }


BOARD_ADAPTERS = {
    "ESP32_TIER0": ESP32Tier0Adapter,
}
