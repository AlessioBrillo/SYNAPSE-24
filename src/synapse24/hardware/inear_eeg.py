"""In-Ear EEG Satellite Pod for SYNAPSE-24 Tier 0 Continuity.

Architecture.md §67-68: 1-2 channel EEG in-ear for continuous H24 coverage
when head pod is off (shower, sport, charging). Based on Guermandi et al.
"A Wireless System for EEG Acquisition and Processing in an Earbud Form Factor
with 600 Hours Battery Lifetime," IEEE EMBC 2022.

Target hardware: nRF5340 + ADS1299-4CH / AFE4400 (1-2ch, 24-bit, <10µA/ch)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt

try:
    from pylsl import StreamInfo, StreamOutlet, local_clock
except ImportError:  # pragma: no cover
    StreamInfo = None
    StreamOutlet = None
    local_clock = None

from .base import BoardAdapter, BoardConfig


@dataclass(frozen=True)
class InEarEEGConfig:
    """Configuration for In-Ear EEG satellite pod.

    Optimized for ultra-low-power continuous Tier 0 operation:
    - 1-2 EEG channels at 128-256 Hz
    - BLE 5.3 with 10-20% duty cycle
    - nRF5340 ULP co-processor for always-on acquisition
    - Target: <0.3mA avg @ 3.7V (600h on 150mAh)
    """

    # Tier classification (Architecture.md §37-40)
    tier: int = 0  # Tier 0: Continuous H24

    # EEG Front-end (ADS1299-4CH or AFE4400)
    eeg_channels: int = 2  # 1 or 2 channels (Fp1-Fp2 / T3-T4 in-ear)
    eeg_sampling_rate: int = 256  # Hz
    eeg_gain: int = 24  # 24x gain for dry electrodes
    eeg_input_type: str = "ADS1299"  # "ADS1299" or "AFE4400"
    eeg_spi_speed_mhz: int = 4

    # IMU (optional, for motion artifact tagging)
    imu_enabled: bool = True
    imu_i2c_addr: int = 0x68  # ICM-20948
    imu_sampling_rate: int = 50  # Hz (lower than hub for power)

    # BLE 5.3 LSL Bridge
    ble_device_name: str = "SYNAPSE-EAR"
    ble_service_uuid: str = "0000ffe0-0000-1000-8000-00805f9b34fb"
    ble_char_uuid: str = "0000ffe1-0000-1000-8000-00805f9b34fb"
    ble_connection_interval_min_ms: int = 20  # 20ms = 50Hz max throughput
    ble_connection_interval_max_ms: int = 40
    ble_slave_latency: int = 0  # No latency for continuous stream
    ble_supervision_timeout_ms: int = 4000
    ble_tx_power_dbm: int = 0  # 0 dBm for range/power balance

    # LSL Stream names (Tier 0)
    lsl_stream_eeg: str = "SYNAPSE_EEG_EAR_T0"
    lsl_stream_acc: str = "SYNAPSE_ACC_EAR_T0"

    # Power management
    battery_mah: float = 150.0  # 301020 LiPo pouch
    target_lifetime_h: float = 600.0  # Guermandi 2022 target
    ble_duty_cycle_pct: float = 15.0  # % time radio active
    mcu_active_duty_cycle_pct: float = 10.0  # % time MCU active (ULP handles rest)

    # Electrode configuration
    electrode_type: str = "dry_gold_plated"  # 3D-printed gold-plated pins
    electrode_impedance_target_kohm: float = 50.0  # Target <50kΩ for dry in-ear
    driven_right_leg: bool = True  # Active DRL for common-mode rejection


class InEarEEGFirmware:
    """In-Ear EEG firmware simulation for CI/testing.

    In production, this would be compiled Zephyr/Rust/C++ for nRF5340.
    This Python class simulates the firmware behavior for testing.
    """

    def __init__(self, config: InEarEEGConfig | None = None) -> None:
        self.config = config or InEarEEGConfig()
        self._eeg_outlet: StreamOutlet | None = None
        self._acc_outlet: StreamOutlet | None = None
        self._running = False
        self._sample_count = 0

    def setup_lsl_streams(self) -> None:
        """Initialize LSL outlets for in-ear EEG and IMU."""
        if StreamInfo is None or StreamOutlet is None:
            raise RuntimeError("pylsl not installed")

        # EEG stream (1-2 channels, Tier 0)
        eeg_info = StreamInfo(
            name=self.config.lsl_stream_eeg,
            type="EEG_T0",
            channel_count=self.config.eeg_channels,
            nominal_srate=self.config.eeg_sampling_rate,
            channel_format="float32",
            source_id=f"synapse24_ear_eeg_t0_{self.config.ble_device_name}",
        )
        eeg_info.desc().append_child_value("sensor", self.config.eeg_input_type)
        eeg_info.desc().append_child_value("placement", "in_ear")
        eeg_info.desc().append_child_value("electrode_type", self.config.electrode_type)
        eeg_info.desc().append_child_value("channels", str(self.config.eeg_channels))
        eeg_info.desc().append_child_value(
            "driven_right_leg", str(self.config.driven_right_leg).lower()
        )
        self._eeg_outlet = StreamOutlet(eeg_info, chunk_size=32, max_buffered=360)

        # IMU stream (optional, Tier 0)
        if self.config.imu_enabled:
            acc_info = StreamInfo(
                name=self.config.lsl_stream_acc,
                type="ACC_T0",
                channel_count=3,
                nominal_srate=self.config.imu_sampling_rate,
                channel_format="float32",
                source_id=f"synapse24_ear_acc_t0_{self.config.ble_device_name}",
            )
            acc_info.desc().append_child_value("sensor", "ICM-20948")
            acc_info.desc().append_child_value("placement", "in_ear")
            self._acc_outlet = StreamOutlet(acc_info, chunk_size=32, max_buffered=360)

    def start_streaming(self) -> None:
        """Start sensor acquisition and LSL streaming."""
        self._running = True
        self._sample_count = 0

    def stop_streaming(self) -> None:
        """Stop sensor acquisition."""
        self._running = False

    def push_eeg_sample(
        self, eeg_data: npt.NDArray[np.float64], timestamp: float | None = None
    ) -> None:
        """Push EEG sample(s) to LSL.

        Args:
            eeg_data: Shape (n_channels,) or (n_channels, n_samples)
            timestamp: LSL timestamp (auto-generated if None)
        """
        if self._eeg_outlet and self._running:
            if timestamp is None and local_clock:
                timestamp = local_clock()

            # Ensure 2D: (n_channels, n_samples)
            if eeg_data.ndim == 1:
                eeg_data = eeg_data.reshape(-1, 1)

            n_samples = eeg_data.shape[1]
            for i in range(n_samples):
                sample = eeg_data[:, i].astype(np.float32)
                ts = timestamp + i / self.config.eeg_sampling_rate if timestamp else None
                self._eeg_outlet.push_sample(sample.tolist(), ts)
            self._sample_count += n_samples

    def push_acc_sample(
        self,
        accel: npt.NDArray[np.float64],
        timestamp: float | None = None,
    ) -> None:
        """Push IMU accelerometer sample to LSL."""
        if self._acc_outlet and self._running and self.config.imu_enabled:
            if timestamp is None and local_clock:
                timestamp = local_clock()
            self._acc_outlet.push_sample(accel.astype(np.float32).tolist(), timestamp)

    def get_config_bytes(self) -> bytes:
        """Serialize config for OTA update or BLE config characteristic.

        Simple format: 16 bytes for key parameters
        """
        # Simplified: use 1 byte each for scaled values (sufficient for ranges)
        battery_scaled = min(255, int(self.config.battery_mah * 10))
        lifetime_scaled = min(255, int(self.config.target_lifetime_h * 10))
        ble_duty_scaled = min(255, int(self.config.ble_duty_cycle_pct * 100))
        mcu_duty_scaled = min(255, int(self.config.mcu_active_duty_cycle_pct * 100))

        return struct.pack(
            "<BBBBHHBBBBBBBBBB",
            self.config.eeg_channels,  # 1 byte
            self.config.eeg_sampling_rate & 0xFF,  # 1 byte (low)
            (self.config.eeg_sampling_rate >> 8) & 0xFF,  # 1 byte (high)
            self.config.eeg_gain,  # 1 byte
            self.config.ble_connection_interval_min_ms,  # 2 bytes
            self.config.ble_connection_interval_max_ms,  # 2 bytes
            self.config.ble_slave_latency,  # 1 byte
            self.config.ble_supervision_timeout_ms & 0xFF,  # 1 byte (low)
            (self.config.ble_supervision_timeout_ms >> 8) & 0xFF,  # 1 byte (high)
            self.config.ble_tx_power_dbm & 0xFF,  # 1 byte
            battery_scaled,  # 1 byte
            lifetime_scaled,  # 1 byte
            ble_duty_scaled,  # 1 byte
            mcu_duty_scaled,  # 1 byte
            0,  # reserved
            0,  # reserved
        )

    @property
    def sample_count(self) -> int:
        return self._sample_count


class InEarEEGAdapter(BoardAdapter):
    """BoardAdapter for In-Ear EEG satellite pod (Tier 0)."""

    @property
    def board_id(self) -> str:
        return "INEAR_EEG_BOARD"

    @property
    def default_sampling_rate(self) -> int:
        return 256  # EEG at 256 Hz

    @property
    def default_channels(self) -> dict[str, list[int]]:
        return {
            "eeg": list(range(2)),  # Max 2 EEG channels
            "acc": [2, 3, 4],
        }

    def create_config(self, **overrides: Any) -> BoardConfig:
        config = BoardConfig(
            board_id=self.board_id,
            mac_address=overrides.get("mac_address", ""),
            serial_port=overrides.get("serial_port", ""),
            sampling_rate=overrides.get("sampling_rate", self.default_sampling_rate),
        )
        for key, value in overrides.items():
            if hasattr(config, key) and key not in ("mac_address", "serial_port", "sampling_rate"):
                setattr(config, key, value)
        return config

    def get_stream_mapping(self) -> dict[str, dict[str, Any]]:
        """Get LSL stream mapping for In-Ear EEG (Tier 0)."""
        return {
            "EEG": {
                "channels": list(range(2)),
                "type": "EEG_T0",
                "unit": "µV",
                "tier": 0,
            },
            "ACC": {
                "channels": [2, 3, 4],
                "type": "ACC_T0",
                "unit": "g",
                "tier": 0,
            },
        }


def create_synthetic_inear_eeg_data(
    duration_s: float = 60.0,
    eeg_fs: int = 256,
    imu_fs: int = 50,
    eeg_channels: int = 2,
    alpha_power: float = 10.0,  # µV^2/Hz (eyes closed alpha)
    motion_level: float = 0.0,
    seed: int = 42,
) -> dict[str, npt.NDArray[np.float64]]:
    """Generate synthetic in-ear EEG data for testing.

    In-ear EEG characteristics (Guermandi 2022):
    - Lower amplitude than scalp EEG (~5-20 µV vs 50-100 µV)
    - Strong alpha in eyes-closed (ear canal near temporal lobe)
    - Susceptible to jaw/chewing artifacts (not modeled here)
    - 1-2 channels only (bipolar or referential)

    Args:
        duration_s: Duration in seconds
        eeg_fs: EEG sampling rate (Hz)
        imu_fs: IMU sampling rate (Hz)
        eeg_channels: Number of EEG channels (1 or 2)
        alpha_power: Alpha band power (µV^2/Hz)
        motion_level: Motion artifact level (0-1)
        seed: Random seed

    Returns:
        Dict with keys: eeg (n_ch, n_samples), acc_x, acc_y, acc_z, t_eeg, t_imu
    """
    rng = np.random.default_rng(seed)

    n_eeg = int(duration_s * eeg_fs)
    n_imu = int(duration_s * imu_fs)

    t_eeg = np.arange(n_eeg) / eeg_fs
    t_imu = np.arange(n_imu) / imu_fs

    # EEG: Alpha rhythm (8-12 Hz) + noise
    # In-ear EEG has lower amplitude, so scale accordingly
    eeg_data = np.zeros((eeg_channels, n_eeg), dtype=np.float64)

    for ch in range(eeg_channels):
        # Alpha rhythm ~10 Hz
        alpha_freq = 10.0 + rng.normal(0, 0.5)
        eeg_data[ch] = np.sqrt(alpha_power / 2) * np.sin(2 * np.pi * alpha_freq * t_eeg)

        # Add slower drift (delta/theta)
        eeg_data[ch] += 2.0 * np.sin(2 * np.pi * 2.0 * t_eeg)  # 2 Hz delta
        eeg_data[ch] += 1.5 * np.sin(2 * np.pi * 5.0 * t_eeg)  # 5 Hz theta

        # Add beta/gamma (low amplitude)
        eeg_data[ch] += rng.normal(0, 0.5, n_eeg)

        # Add motion artifact if specified
        if motion_level > 0:
            # Simulate jaw movement artifact (low freq, high amp)
            artifact = motion_level * 20.0 * np.sin(2 * np.pi * 1.5 * t_eeg)
            eeg_data[ch] += artifact

    # IMU: Low motion for in-ear (mostly static)
    acc_mag = 1.0 + motion_level * rng.normal(0, 0.5, n_imu)
    acc_x = acc_mag * np.sin(2 * np.pi * 0.05 * t_imu) + rng.normal(0, 0.01, n_imu)
    acc_y = acc_mag * np.cos(2 * np.pi * 0.05 * t_imu) + rng.normal(0, 0.01, n_imu)
    acc_z = 1.0 + rng.normal(0, 0.01, n_imu)

    return {
        "eeg": eeg_data,
        "acc_x": acc_x.astype(np.float64),
        "acc_y": acc_y.astype(np.float64),
        "acc_z": acc_z.astype(np.float64),
        "t_eeg": t_eeg,
        "t_imu": t_imu,
    }


BOARD_ADAPTERS = {
    "INEAR_EEG_BOARD": InEarEEGAdapter,
}


__all__ = [
    "InEarEEGConfig",
    "InEarEEGFirmware",
    "InEarEEGAdapter",
    "create_synthetic_inear_eeg_data",
    "BOARD_ADAPTERS",
]
