"""Deterministic synthetic 3-pod data generator for SYNAPSE-24 Phase 0 exit gate.

Generates physiologically plausible signals for:
- Forearm Hub (Tier 0): ECG 500Hz, PPG 64Hz (red+IR), IMU 100Hz (9-axis)
- Head Pod (Tier 1): EEG 8ch 500Hz, fNIRS 10Hz (HbO/HbR), ACC 100Hz (3-axis)
- In-Ear Satellite (Tier 0): EEG 2ch 250Hz, IMU 100Hz (3-axis)

All signals share a common LSL clock domain with configurable per-pod clock drift
(±50 ppm default) to validate MultiPodClockSync correction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt


@dataclass(frozen=True)
class PodSignalSpec:
    """Specification for a single pod's signal generation."""

    pod_id: str
    tier: int
    sampling_rates: dict[str, int]
    channel_counts: dict[str, int]
    clock_drift_ppm: float = 0.0  # Parts per million relative to hub clock


# Default specifications matching config/hardware.yaml
DEFAULT_POD_SPECS = [
    PodSignalSpec(
        pod_id="forearm_hub",
        tier=0,
        sampling_rates={"ecg": 500, "ppg": 64, "imu": 100},
        channel_counts={"ecg": 1, "ppg": 2, "imu": 9},
        clock_drift_ppm=0.0,  # Hub is reference clock
    ),
    PodSignalSpec(
        pod_id="head_pod",
        tier=1,
        sampling_rates={"eeg": 500, "fnirs": 10, "acc": 100},
        channel_counts={"eeg": 8, "fnirs": 2, "acc": 3},
        clock_drift_ppm=35.0,  # +35 ppm drift
    ),
    PodSignalSpec(
        pod_id="in_ear_satellite",
        tier=0,
        sampling_rates={"eeg": 250, "imu": 100},
        channel_counts={"eeg": 2, "imu": 3},
        clock_drift_ppm=-22.0,  # -22 ppm drift
    ),
]


class SyntheticPodGenerator:
    """Generates deterministic multi-pod physiological signals.

    Uses a fixed seed for reproducibility. All signals are derived from
    a common time base with per-pod clock drift applied.
    """

    def __init__(
        self,
        seed: int = 42,
        pod_specs: list[PodSignalSpec] | None = None,
        duration_s: float = 300.0,
    ) -> None:
        self.seed = seed
        self.pod_specs = pod_specs or DEFAULT_POD_SPECS
        self.duration_s = duration_s
        self._rng = np.random.default_rng(seed)
        self._hub_clock = np.arange(0, duration_s, 1.0 / 500.0)  # 500Hz reference

    def generate_all(self) -> dict[str, dict[str, npt.NDArray[np.float64]]]:
        """Generate signals for all pods.

        Returns:
            Dict[pod_id, Dict[modality, (n_samples, n_channels)]]
        """
        return {spec.pod_id: self._generate_pod(spec) for spec in self.pod_specs}

    def generate_timestamps(self, pod_id: str, modality: str) -> npt.NDArray[np.float64]:
        """Generate LSL timestamps for a specific pod/modality with clock drift."""
        spec = next(s for s in self.pod_specs if s.pod_id == pod_id)
        fs = spec.sampling_rates[modality]
        n_samples = int(self.duration_s * fs)

        # Ideal timestamps in hub clock domain
        ideal_ts = np.arange(n_samples, dtype=np.float64) / fs

        # Apply clock drift: t_pod = t_hub * (1 + drift_ppm * 1e-6)
        drift_factor = 1.0 + spec.clock_drift_ppm * 1e-6
        return ideal_ts * drift_factor

    def _generate_pod(self, spec: PodSignalSpec) -> dict[str, npt.NDArray[np.float64]]:
        """Generate all signals for one pod."""
        signals = {}

        # Shared time base for this pod (with drift)
        pod_time = self._get_pod_time_base(spec)

        # Generate each modality
        for modality, fs in spec.sampling_rates.items():
            n_channels = spec.channel_counts[modality]
            n_samples = int(self.duration_s * fs)
            t = np.arange(n_samples, dtype=np.float64) / fs

            if modality == "ecg":
                signals["ecg"] = self._generate_ecg(t, n_channels, spec.pod_id)
            elif modality == "ppg":
                signals["ppg_red"], signals["ppg_ir"] = self._generate_ppg_dual(t, spec.pod_id)
            elif modality == "imu":
                signals["acc_x"], signals["acc_y"], signals["acc_z"] = self._generate_imu_acc(
                    t, spec.pod_id
                )
                signals["gyro_x"], signals["gyro_y"], signals["gyro_z"] = self._generate_imu_gyro(
                    t, spec.pod_id
                )
                signals["mag_x"], signals["mag_y"], signals["mag_z"] = self._generate_imu_mag(
                    t, spec.pod_id
                )
            elif modality == "eeg":
                signals["eeg"] = self._generate_eeg(t, n_channels, spec.pod_id, spec.tier)
            elif modality == "fnirs":
                signals["hbo"], signals["hbr"] = self._generate_fnirs(t, spec.pod_id)
            elif modality == "acc":
                signals["acc_x"], signals["acc_y"], signals["acc_z"] = self._generate_imu_acc(
                    t, spec.pod_id
                )

        return signals

    def _get_pod_time_base(self, spec: PodSignalSpec) -> npt.NDArray[np.float64]:
        """Get time base for pod at highest sampling rate."""
        max_fs = max(spec.sampling_rates.values())
        n_samples = int(self.duration_s * max_fs)
        t = np.arange(n_samples, dtype=np.float64) / max_fs
        drift_factor = 1.0 + spec.clock_drift_ppm * 1e-6
        return t * drift_factor

    def _generate_ecg(
        self, t: npt.NDArray[np.float64], n_channels: int, pod_id: str
    ) -> npt.NDArray[np.float64]:
        """Generate realistic ECG with HRV."""
        # Base heart rate ~70 BPM with RSA (respiratory sinus arrhythmia)
        hr_base = 70.0
        rsa_amp = 5.0  # BPM variation from breathing
        resp_rate = 0.25  # Hz (15 breaths/min)

        # Instantaneous heart rate with RSA
        instantaneous_hr = hr_base + rsa_amp * np.sin(2 * np.pi * resp_rate * t)

        # Convert to R-peak times
        rr_intervals = 60.0 / instantaneous_hr  # seconds
        r_peaks = np.cumsum(rr_intervals)
        r_peaks = r_peaks[r_peaks < t[-1]]

        # Generate ECG waveform (simplified McSharry model)
        ecg = np.zeros_like(t)
        for rp in r_peaks:
            idx = np.argmin(np.abs(t - rp))
            if 0 < idx < len(t) - 30:
                # P wave
                ecg[idx - 15 : idx - 5] += 0.15 * np.sin(np.linspace(0, np.pi, 10))
                # QRS complex
                qrs_template = np.array([0, -0.3, 1.5, -0.5, 0.2, 0, 0])
                ecg[idx - 3 : idx + 4] += qrs_template
                # T wave
                ecg[idx + 10 : idx + 30] += 0.3 * np.sin(np.linspace(0, np.pi, 20))

        # Add baseline wander and noise
        ecg += 0.05 * np.sin(2 * np.pi * 0.05 * t)  # 0.05 Hz wander
        ecg += self._rng.normal(0, 0.02, size=len(t))  # Measurement noise

        if n_channels == 1:
            return ecg.reshape(-1, 1)
        return np.tile(ecg.reshape(-1, 1), (1, n_channels))

    def _generate_ppg_dual(
        self, t: npt.NDArray[np.float64], pod_id: str
    ) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
        """Generate dual-wavelength PPG (red + IR) with motion artifact."""
        # Heart rate from ECG (shared physiological basis)
        hr = 70.0 + 5.0 * np.sin(2 * np.pi * 0.25 * t)
        pulse_freq = hr / 60.0

        # PPG waveform (approximate arterial pulse)
        ppg_clean = np.zeros_like(t)
        for i in range(len(t)):
            phase = (t[i] * pulse_freq[i]) % 1.0
            if phase < 0.3:
                ppg_clean[i] = 1.0 - np.cos(phase * np.pi / 0.3)
            else:
                ppg_clean[i] = 0.3 * np.exp(-(phase - 0.3) * 8)

        # Add respiratory modulation
        ppg_clean *= 1 + 0.05 * np.sin(2 * np.pi * 0.25 * t)

        # Motion artifact (correlated with IMU)
        motion_artifact = 0.15 * self._rng.normal(0, 1, size=len(t))
        motion_artifact = self._lowpass(motion_artifact, 5.0, 1.0 / (t[1] - t[0]))

        # Red and IR channels (different AC/DC ratios)
        dc_red, dc_ir = 1.0, 1.2
        ac_red, ac_ir = 0.02, 0.025

        ppg_red = dc_red + ac_red * ppg_clean + motion_artifact
        ppg_ir = dc_ir + ac_ir * ppg_clean + motion_artifact * 0.8

        # Add noise
        ppg_red += self._rng.normal(0, 0.005, size=len(t))
        ppg_ir += self._rng.normal(0, 0.005, size=len(t))

        return ppg_red.reshape(-1, 1), ppg_ir.reshape(-1, 1)

    def _generate_imu_acc(
        self, t: npt.NDArray[np.float64], pod_id: str
    ) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64], npt.NDArray[np.float64]]:
        """Generate 3-axis accelerometer with gravity + motion."""
        # Gravity component (1g downward, varies with orientation)
        g = 9.81
        acc_x = g * 0.1 * np.sin(2 * np.pi * 0.01 * t)  # Slow postural sway
        acc_y = g * 0.1 * np.cos(2 * np.pi * 0.01 * t)
        acc_z = g + g * 0.05 * np.sin(2 * np.pi * 0.02 * t)

        # Voluntary movement (bursts)
        n_bursts = int(self.duration_s / 60)  # ~1 per minute
        for _ in range(n_bursts):
            burst_start = self._rng.uniform(0, self.duration_s - 5)
            burst_mask = (t >= burst_start) & (t < burst_start + 3)
            acc_x[burst_mask] += self._rng.normal(0, 0.5, size=burst_mask.sum())
            acc_y[burst_mask] += self._rng.normal(0, 0.5, size=burst_mask.sum())
            acc_z[burst_mask] += self._rng.normal(0, 0.3, size=burst_mask.sum())

        # Sensor noise
        noise_std = 0.01
        acc_x += self._rng.normal(0, noise_std, size=len(t))
        acc_y += self._rng.normal(0, noise_std, size=len(t))
        acc_z += self._rng.normal(0, noise_std, size=len(t))

        return acc_x, acc_y, acc_z

    def _generate_imu_gyro(
        self, t: npt.NDArray[np.float64], pod_id: str
    ) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64], npt.NDArray[np.float64]]:
        """Generate 3-axis gyroscope (deg/s)."""
        # Slow drift + movement bursts
        gyro_x = self._rng.normal(0, 0.5, size=len(t))
        gyro_y = self._rng.normal(0, 0.5, size=len(t))
        gyro_z = self._rng.normal(0, 0.5, size=len(t))

        # Add movement-correlated bursts
        n_bursts = int(self.duration_s / 60)
        for _ in range(n_bursts):
            burst_start = self._rng.uniform(0, self.duration_s - 3)
            burst_mask = (t >= burst_start) & (t < burst_start + 2)
            gyro_x[burst_mask] += self._rng.normal(0, 10, size=burst_mask.sum())
            gyro_y[burst_mask] += self._rng.normal(0, 10, size=burst_mask.sum())
            gyro_z[burst_mask] += self._rng.normal(0, 5, size=burst_mask.sum())

        return gyro_x, gyro_y, gyro_z

    def _generate_imu_mag(
        self, t: npt.NDArray[np.float64], pod_id: str
    ) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64], npt.NDArray[np.float64]]:
        """Generate 3-axis magnetometer (µT)."""
        # Earth field ~50 µT with small perturbations
        mag_x = 25.0 + self._rng.normal(0, 0.5, size=len(t))
        mag_y = 5.0 + self._rng.normal(0, 0.5, size=len(t))
        mag_z = 40.0 + self._rng.normal(0, 0.5, size=len(t))
        return mag_x, mag_y, mag_z

    def _generate_eeg(
        self, t: npt.NDArray[np.float64], n_channels: int, pod_id: str, tier: int
    ) -> npt.NDArray[np.float64]:
        """Generate multi-channel EEG with alpha rhythm (eyes closed) and sleep features."""
        eeg = np.zeros((len(t), n_channels))

        # Alpha rhythm (8-12 Hz) - dominant in eyes-closed rest
        alpha_freq = 10.0
        for ch in range(n_channels):
            # Spatial variation in alpha power
            alpha_power = 20.0 + ch * 2.0 + self._rng.normal(0, 3.0)
            eeg[:, ch] += alpha_power * np.sin(
                2 * np.pi * alpha_freq * t + self._rng.uniform(0, 2 * np.pi)
            )

            # Add sleep spindles (Tier 1: 12-16 Hz, 0.5-1.5s bursts)
            if tier == 1:
                n_spindles = int(self.duration_s / 30)
                for _ in range(n_spindles):
                    start = self._rng.uniform(0, self.duration_s - 1)
                    dur = self._rng.uniform(0.5, 1.5)
                    spindle_freq = self._rng.uniform(12, 16)
                    mask = (t >= start) & (t < start + dur)
                    envelope = np.sin(np.pi * (t[mask] - start) / dur) ** 2
                    eeg[mask, ch] += 30.0 * envelope * np.sin(2 * np.pi * spindle_freq * t[mask])

            # Add slow waves (delta, 0.5-4 Hz) for sleep
            if tier == 1:
                eeg[:, ch] += 15.0 * np.sin(2 * np.pi * 1.5 * t + self._rng.uniform(0, 2 * np.pi))

            # Beta/gamma background
            eeg[:, ch] += self._rng.normal(0, 3.0, size=len(t))

        # Add common mode (reference electrode noise)
        common_mode = self._rng.normal(0, 2.0, size=len(t))
        eeg += common_mode.reshape(-1, 1)

        return eeg

    def _generate_fnirs(
        self, t: npt.NDArray[np.float64], pod_id: str
    ) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
        """Generate fNIRS HbO/HbR at 10 Hz (slow hemodynamic response)."""
        fs = 10.0
        t_fnirs = np.arange(int(self.duration_s * fs)) / fs

        # Slow hemodynamic oscillations (Mayer waves ~0.1 Hz)
        hbo = 0.5 * np.sin(2 * np.pi * 0.1 * t_fnirs)
        hbr = -0.3 * np.sin(2 * np.pi * 0.1 * t_fnirs)  # Anti-correlated

        # Task-related activation (simulated)
        n_events = int(self.duration_s / 60)
        for _ in range(n_events):
            start = self._rng.uniform(10, self.duration_s - 20)
            mask = (t_fnirs >= start) & (t_fnirs < start + 15)
            hrf = self._hrf(t_fnirs[mask] - start)
            hbo[mask] += 0.2 * hrf
            hbr[mask] -= 0.1 * hrf

        # Noise
        hbo += self._rng.normal(0, 0.02, size=len(t_fnirs))
        hbr += self._rng.normal(0, 0.02, size=len(t_fnirs))

        return hbo.reshape(-1, 1), hbr.reshape(-1, 1)

    def _hrf(self, t: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        """Canonical hemodynamic response function."""
        return (t**5 * np.exp(-t / 1.5)) / (1.5**6 * 120)

    def _lowpass(
        self, signal: npt.NDArray[np.float64], fc: float, fs: float
    ) -> npt.NDArray[np.float64]:
        """Simple first-order lowpass filter."""
        rc = 1.0 / (2 * np.pi * fc)
        dt = 1.0 / fs
        alpha = dt / (rc + dt)
        filtered = np.zeros_like(signal)
        filtered[0] = signal[0]
        for i in range(1, len(signal)):
            filtered[i] = filtered[i - 1] + alpha * (signal[i] - filtered[i - 1])
        return filtered


def generate_synthetic_recording(
    seed: int = 42,
    duration_s: float = 300.0,
    pod_specs: list[PodSignalSpec] | None = None,
) -> dict[str, Any]:
    """Generate complete synthetic recording for all pods.

    Returns:
        Dict with:
        - 'pods': Dict[pod_id, Dict[modality, signal_array]]
        - 'timestamps': Dict[pod_id, Dict[modality, timestamp_array]]
        - 'specs': PodSignalSpec list
        - 'metadata': generation parameters
    """
    generator = SyntheticPodGenerator(seed=seed, pod_specs=pod_specs, duration_s=duration_s)
    pods_data = generator.generate_all()
    timestamps = {}

    for spec in generator.pod_specs:
        timestamps[spec.pod_id] = {}
        for modality in spec.sampling_rates:
            timestamps[spec.pod_id][modality] = generator.generate_timestamps(spec.pod_id, modality)

    return {
        "pods": pods_data,
        "timestamps": timestamps,
        "specs": generator.pod_specs,
        "metadata": {
            "seed": seed,
            "duration_s": duration_s,
            "generator_version": "1.0",
            "hub_clock_drift_ppm": 0.0,
        },
    }


def create_lsl_streams_from_synthetic(
    synthetic_data: dict[str, Any],
) -> list[dict[str, Any]]:
    """Convert synthetic data to LSL stream format for XDF writing.

    Returns list of stream dicts compatible with synapse24.utils.write_xdf
    """
    from synapse24.utils import create_stream_info_from_dict

    streams = []
    pods = synthetic_data["pods"]
    timestamps = synthetic_data["timestamps"]
    specs = synthetic_data["specs"]

    stream_name_map = {
        "forearm_hub": {
            "ecg": ("SYNAPSE_ECG_T0", "ECG", ["ECG"], ["µV"]),
            "ppg_red": ("SYNAPSE_PPG_T0", "PPG", ["PPG_RED", "PPG_IR"], ["a.u.", "a.u."]),
            "imu": ("SYNAPSE_ACC_T0", "ACC", ["ACC_X", "ACC_Y", "ACC_Z"], ["g", "g", "g"]),
        },
        "head_pod": {
            "eeg": ("SYNAPSE_EEG_T1", "EEG", [f"EEG_{i + 1}" for i in range(8)], ["µV"] * 8),
            "fnirs": ("SYNAPSE_FNIRS_T1", "fNIRS", ["HbO", "HbR"], ["µM", "µM"]),
            "acc": ("SYNAPSE_ACC_T1", "ACC", ["ACC_X", "ACC_Y", "ACC_Z"], ["g", "g", "g"]),
        },
        "in_ear_satellite": {
            "eeg": ("SYNAPSE_EEG_T0_EAR", "EEG", ["EEG_L", "EEG_R"], ["µV", "µV"]),
            "imu": ("SYNAPSE_IMU_T0_EAR", "IMU", ["ACC_X", "ACC_Y", "ACC_Z"], ["g", "g", "g"]),
        },
    }

    for spec in specs:
        pod_id = spec.pod_id
        pod_data = pods[pod_id]
        pod_timestamps = timestamps[pod_id]
        name_map = stream_name_map[pod_id]

        if pod_id == "forearm_hub":
            # ECG
            ecg_data = pod_data["ecg"]
            ecg_ts = pod_timestamps["ecg"]
            streams.append(
                {
                    "info": create_stream_info_from_dict(
                        {
                            "name": name_map["ecg"][0],
                            "type": name_map["ecg"][1],
                            "channel_count": 1,
                            "sampling_rate": spec.sampling_rates["ecg"],
                            "channel_names": name_map["ecg"][2],
                            "channel_units": name_map["ecg"][3],
                            "tier": spec.tier,
                        }
                    ),
                    "data": ecg_data.astype(np.float32),
                    "timestamps": ecg_ts.astype(np.float64),
                }
            )

            # PPG (red + IR combined)
            ppg_data = np.column_stack([pod_data["ppg_red"], pod_data["ppg_ir"]])
            ppg_ts = pod_timestamps["ppg"]
            streams.append(
                {
                    "info": create_stream_info_from_dict(
                        {
                            "name": name_map["ppg_red"][0],
                            "type": name_map["ppg_red"][1],
                            "channel_count": 2,
                            "sampling_rate": spec.sampling_rates["ppg"],
                            "channel_names": name_map["ppg_red"][2],
                            "channel_units": name_map["ppg_red"][3],
                            "tier": spec.tier,
                        }
                    ),
                    "data": ppg_data.astype(np.float32),
                    "timestamps": ppg_ts.astype(np.float64),
                }
            )

            # IMU (9-axis: acc + gyro + mag)
            imu_data = np.column_stack(
                [
                    pod_data["acc_x"],
                    pod_data["acc_y"],
                    pod_data["acc_z"],
                    pod_data["gyro_x"],
                    pod_data["gyro_y"],
                    pod_data["gyro_z"],
                    pod_data["mag_x"],
                    pod_data["mag_y"],
                    pod_data["mag_z"],
                ]
            )
            imu_ts = pod_timestamps["imu"]
            streams.append(
                {
                    "info": create_stream_info_from_dict(
                        {
                            "name": name_map["imu"][0],
                            "type": name_map["imu"][1],
                            "channel_count": 9,
                            "sampling_rate": spec.sampling_rates["imu"],
                            "channel_names": [
                                "ACC_X",
                                "ACC_Y",
                                "ACC_Z",
                                "GYRO_X",
                                "GYRO_Y",
                                "GYRO_Z",
                                "MAG_X",
                                "MAG_Y",
                                "MAG_Z",
                            ],
                            "channel_units": ["g"] * 3 + ["deg/s"] * 3 + ["µT"] * 3,
                            "tier": spec.tier,
                        }
                    ),
                    "data": imu_data.astype(np.float32),
                    "timestamps": imu_ts.astype(np.float64),
                }
            )

        elif pod_id == "head_pod":
            # EEG
            eeg_data = pod_data["eeg"]
            eeg_ts = pod_timestamps["eeg"]
            streams.append(
                {
                    "info": create_stream_info_from_dict(
                        {
                            "name": name_map["eeg"][0],
                            "type": name_map["eeg"][1],
                            "channel_count": 8,
                            "sampling_rate": spec.sampling_rates["eeg"],
                            "channel_names": name_map["eeg"][2],
                            "channel_units": name_map["eeg"][3],
                            "tier": spec.tier,
                        }
                    ),
                    "data": eeg_data.astype(np.float32),
                    "timestamps": eeg_ts.astype(np.float64),
                }
            )

            # fNIRS
            fnirs_data = np.column_stack([pod_data["hbo"], pod_data["hbr"]])
            fnirs_ts = pod_timestamps["fnirs"]
            streams.append(
                {
                    "info": create_stream_info_from_dict(
                        {
                            "name": name_map["fnirs"][0],
                            "type": name_map["fnirs"][1],
                            "channel_count": 2,
                            "sampling_rate": spec.sampling_rates["fnirs"],
                            "channel_names": name_map["fnirs"][2],
                            "channel_units": name_map["fnirs"][3],
                            "tier": spec.tier,
                        }
                    ),
                    "data": fnirs_data.astype(np.float32),
                    "timestamps": fnirs_ts.astype(np.float64),
                }
            )

            # ACC
            acc_data = np.column_stack([pod_data["acc_x"], pod_data["acc_y"], pod_data["acc_z"]])
            acc_ts = pod_timestamps["acc"]
            streams.append(
                {
                    "info": create_stream_info_from_dict(
                        {
                            "name": name_map["acc"][0],
                            "type": name_map["acc"][1],
                            "channel_count": 3,
                            "sampling_rate": spec.sampling_rates["acc"],
                            "channel_names": name_map["acc"][2],
                            "channel_units": name_map["acc"][3],
                            "tier": spec.tier,
                        }
                    ),
                    "data": acc_data.astype(np.float32),
                    "timestamps": acc_ts.astype(np.float64),
                }
            )

        elif pod_id == "in_ear_satellite":
            # EEG
            eeg_data = pod_data["eeg"]
            eeg_ts = pod_timestamps["eeg"]
            streams.append(
                {
                    "info": create_stream_info_from_dict(
                        {
                            "name": name_map["eeg"][0],
                            "type": name_map["eeg"][1],
                            "channel_count": 2,
                            "sampling_rate": spec.sampling_rates["eeg"],
                            "channel_names": name_map["eeg"][2],
                            "channel_units": name_map["eeg"][3],
                            "tier": spec.tier,
                        }
                    ),
                    "data": eeg_data.astype(np.float32),
                    "timestamps": eeg_ts.astype(np.float64),
                }
            )

            # IMU (3-axis acc only for in-ear)
            imu_data = np.column_stack([pod_data["acc_x"], pod_data["acc_y"], pod_data["acc_z"]])
            imu_ts = pod_timestamps["imu"]
            streams.append(
                {
                    "info": create_stream_info_from_dict(
                        {
                            "name": name_map["imu"][0],
                            "type": name_map["imu"][1],
                            "channel_count": 3,
                            "sampling_rate": spec.sampling_rates["imu"],
                            "channel_names": name_map["imu"][2],
                            "channel_units": name_map["imu"][3],
                            "tier": spec.tier,
                        }
                    ),
                    "data": imu_data.astype(np.float32),
                    "timestamps": imu_ts.astype(np.float64),
                }
            )

    # Add marker stream for tier transitions and sync
    marker_timestamps = np.array([0.0, 60.0, 180.0, 300.0], dtype=np.float64)
    marker_labels = np.array(
        [
            ["recording_start"],
            ["t0_to_t1_night_window"],
            ["t1_to_t0_window_end"],
            ["recording_end"],
        ],
        dtype=object,
    )
    streams.append(
        {
            "info": create_stream_info_from_dict(
                {
                    "name": "SYNAPSE_Markers",
                    "type": "Markers",
                    "channel_count": 1,
                    "sampling_rate": 0,
                    "channel_format": "string",
                    "channel_names": ["marker"],
                    "channel_units": [""],
                    "tier": 1,
                }
            ),
            "data": marker_labels,
            "timestamps": marker_timestamps,
        }
    )

    return streams


if __name__ == "__main__":
    # Quick test generation
    data = generate_synthetic_recording(seed=42, duration_s=60.0)
    print("Generated synthetic recording:")
    for pod_id, pod_data in data["pods"].items():
        print(f"  {pod_id}:")
        for modality, signal in pod_data.items():
            print(f"    {modality}: {signal.shape}")
    print(f"\nTotal modalities: {sum(len(v) for v in data['pods'].values())}")

    # Test LSL stream conversion
    streams = create_lsl_streams_from_synthetic(data)
    print(f"\nGenerated {len(streams)} LSL streams:")
    for s in streams:
        info = s["info"]
        print(
            f"  {info.name()}: {info.channel_count()}ch @ {info.nominal_srate()}Hz, {s['data'].shape[0]} samples"
        )
