"""Sleep-EDF Expanded dataset ingestion and validation pipeline with LSL/XDF export.

Sleep-EDF Expanded contains 197 whole-night polysomnograms (PSGs) with:
- EEG: Fpz-Cz, Pz-Oz at 100 Hz
- Hypnogram: 30-second epochs, sleep stages W/N1/N2/N3/REM (irregular/marker stream)
- Additional channels: EOG, EMG, airflow, etc.

Architecture.md Tier 1: High-density rest/sleep EEG (6-16ch) during sleep windows.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import requests
from tqdm import tqdm

logger = logging.getLogger(__name__)

from synapse24.signal_quality import (
    QualityThresholds,
    SignalQualityMetrics,
    Tier,
    compute_eeg_quality,
)
from synapse24.utils import (
    StreamConfig,
    create_marker_stream,
    create_quality_metadata_stream,
    create_stream_info,
    generate_synthetic_timestamps,
    write_xdf,
)

# Sleep-EDF Expanded on PhysioNet (EDF files live under per-cohort subfolders).
SLEEP_EDF_BASE_URL = "https://physionet.org/files/sleep-edfx/1.0.0/"
SLEEP_EDF_SUBDIR = {"SC": "sleep-cassette", "ST": "sleep-telemetry"}

# Minimal valid EDF sizes (bytes): PSG carries hours of signals, hypnograms
# only annotations. Anything smaller is a poison cache (cf. WESAD zip guard).
_MIN_PSG_BYTES = 1_000_000
_MIN_HYPNOGRAM_BYTES = 500

# Closure subjects for the Phase 0 exit gate (healthy SC cohort, Fpz-Cz @100 Hz).
CLOSURE_SUBJECTS = ("SC4001", "SC4101")

EPOCH_DURATION_S = 30.0
SLEEP_EDF_RECORDS = [
    # Sleep Cassette (SC) - healthy subjects
    "SC4001E0-PSG.edf",
    "SC4001EC-Hypnogram.edf",
    "SC4002E0-PSG.edf",
    "SC4002EC-Hypnogram.edf",
    "SC4011E0-PSG.edf",
    "SC4011EC-Hypnogram.edf",
    "SC4012E0-PSG.edf",
    "SC4012EC-Hypnogram.edf",
    "SC4021E0-PSG.edf",
    "SC4021EC-Hypnogram.edf",
    "SC4022E0-PSG.edf",
    "SC4022EC-Hypnogram.edf",
    "SC4031E0-PSG.edf",
    "SC4031EC-Hypnogram.edf",
    "SC4032E0-PSG.edf",
    "SC4032EC-Hypnogram.edf",
    "SC4041E0-PSG.edf",
    "SC4041EC-Hypnogram.edf",
    "SC4042E0-PSG.edf",
    "SC4042EC-Hypnogram.edf",
    "SC4051E0-PSG.edf",
    "SC4051EC-Hypnogram.edf",
    "SC4052E0-PSG.edf",
    "SC4052EC-Hypnogram.edf",
    "SC4061E0-PSG.edf",
    "SC4061EC-Hypnogram.edf",
    "SC4062E0-PSG.edf",
    "SC4062EC-Hypnogram.edf",
    "SC4071E0-PSG.edf",
    "SC4071EC-Hypnogram.edf",
    "SC4072E0-PSG.edf",
    "SC4072EC-Hypnogram.edf",
    "SC4081E0-PSG.edf",
    "SC4081EC-Hypnogram.edf",
    "SC4082E0-PSG.edf",
    "SC4082EC-Hypnogram.edf",
    "SC4091E0-PSG.edf",
    "SC4091EC-Hypnogram.edf",
    "SC4092E0-PSG.edf",
    "SC4092EC-Hypnogram.edf",
    "SC4101E0-PSG.edf",
    "SC4101EC-Hypnogram.edf",
    "SC4102E0-PSG.edf",
    "SC4102EC-Hypnogram.edf",
    "SC4111E0-PSG.edf",
    "SC4111EC-Hypnogram.edf",
    "SC4112E0-PSG.edf",
    "SC4112EC-Hypnogram.edf",
    "SC4121E0-PSG.edf",
    "SC4121EC-Hypnogram.edf",
    "SC4122E0-PSG.edf",
    "SC4122EC-Hypnogram.edf",
    "SC4131E0-PSG.edf",
    "SC4131EC-Hypnogram.edf",
    "SC4132E0-PSG.edf",
    "SC4132EC-Hypnogram.edf",
    "SC4141E0-PSG.edf",
    "SC4141EC-Hypnogram.edf",
    "SC4142E0-PSG.edf",
    "SC4142EC-Hypnogram.edf",
    "SC4151E0-PSG.edf",
    "SC4151EC-Hypnogram.edf",
    "SC4152E0-PSG.edf",
    "SC4152EC-Hypnogram.edf",
    "SC4161E0-PSG.edf",
    "SC4161EC-Hypnogram.edf",
    "SC4162E0-PSG.edf",
    "SC4162EC-Hypnogram.edf",
    "SC4171E0-PSG.edf",
    "SC4171EC-Hypnogram.edf",
    "SC4172E0-PSG.edf",
    "SC4172EC-Hypnogram.edf",
    "SC4181E0-PSG.edf",
    "SC4181EC-Hypnogram.edf",
    "SC4182E0-PSG.edf",
    "SC4182EC-Hypnogram.edf",
    "SC4191E0-PSG.edf",
    "SC4191EC-Hypnogram.edf",
    "SC4192E0-PSG.edf",
    "SC4192EC-Hypnogram.edf",
    # Sleep Telemetry (ST) - subjects with sleep disorders
    "ST7011E0-PSG.edf",
    "ST7011EC-Hypnogram.edf",
    "ST7012E0-PSG.edf",
    "ST7012EC-Hypnogram.edf",
    "ST7021E0-PSG.edf",
    "ST7021EC-Hypnogram.edf",
    "ST7022E0-PSG.edf",
    "ST7022EC-Hypnogram.edf",
    "ST7031E0-PSG.edf",
    "ST7031EC-Hypnogram.edf",
    "ST7032E0-PSG.edf",
    "ST7032EC-Hypnogram.edf",
    "ST7041E0-PSG.edf",
    "ST7041EC-Hypnogram.edf",
    "ST7042E0-PSG.edf",
    "ST7042EC-Hypnogram.edf",
    "ST7051E0-PSG.edf",
    "ST7051EC-Hypnogram.edf",
    "ST7052E0-PSG.edf",
    "ST7052EC-Hypnogram.edf",
    "ST7061E0-PSG.edf",
    "ST7061EC-Hypnogram.edf",
    "ST7062E0-PSG.edf",
    "ST7062EC-Hypnogram.edf",
    "ST7071E0-PSG.edf",
    "ST7071EC-Hypnogram.edf",
    "ST7072E0-PSG.edf",
    "ST7072EC-Hypnogram.edf",
    "ST7081E0-PSG.edf",
    "ST7081EC-Hypnogram.edf",
    "ST7082E0-PSG.edf",
    "ST7082EC-Hypnogram.edf",
    "ST7091E0-PSG.edf",
    "ST7091EC-Hypnogram.edf",
    "ST7092E0-PSG.edf",
    "ST7092EC-Hypnogram.edf",
    "ST7101E0-PSG.edf",
    "ST7101EC-Hypnogram.edf",
    "ST7102E0-PSG.edf",
    "ST7102EC-Hypnogram.edf",
    "ST7111E0-PSG.edf",
    "ST7111EC-Hypnogram.edf",
    "ST7112E0-PSG.edf",
    "ST7112EC-Hypnogram.edf",
    "ST7121E0-PSG.edf",
    "ST7121EC-Hypnogram.edf",
    "ST7122E0-PSG.edf",
    "ST7122EC-Hypnogram.edf",
    "ST7131E0-PSG.edf",
    "ST7131EC-Hypnogram.edf",
    "ST7132E0-PSG.edf",
    "ST7132EC-Hypnogram.edf",
    "ST7141E0-PSG.edf",
    "ST7141EC-Hypnogram.edf",
    "ST7142E0-PSG.edf",
    "ST7142EC-Hypnogram.edf",
    "ST7151E0-PSG.edf",
    "ST7151EC-Hypnogram.edf",
    "ST7152E0-PSG.edf",
    "ST7152EC-Hypnogram.edf",
    "ST7161E0-PSG.edf",
    "ST7161EC-Hypnogram.edf",
    "ST7162E0-PSG.edf",
    "ST7162EC-Hypnogram.edf",
    "ST7171E0-PSG.edf",
    "ST7171EC-Hypnogram.edf",
    "ST7172E0-PSG.edf",
    "ST7172EC-Hypnogram.edf",
    "ST7181E0-PSG.edf",
    "ST7181EC-Hypnogram.edf",
    "ST7182E0-PSG.edf",
    "ST7182EC-Hypnogram.edf",
    "ST7191E0-PSG.edf",
    "ST7191EC-Hypnogram.edf",
    "ST7192E0-PSG.edf",
    "ST7192EC-Hypnogram.edf",
    "ST7201E0-PSG.edf",
    "ST7201EC-Hypnogram.edf",
    "ST7202E0-PSG.edf",
    "ST7202EC-Hypnogram.edf",
    "ST7211E0-PSG.edf",
    "ST7211EC-Hypnogram.edf",
    "ST7212E0-PSG.edf",
    "ST7212EC-Hypnogram.edf",
    "ST7221E0-PSG.edf",
    "ST7221EC-Hypnogram.edf",
    "ST7222E0-PSG.edf",
    "ST7222EC-Hypnogram.edf",
    "ST7231E0-PSG.edf",
    "ST7231EC-Hypnogram.edf",
    "ST7232E0-PSG.edf",
    "ST7232EC-Hypnogram.edf",
]

# Sleep stage mapping
STAGE_MAP = {
    0: "W",  # Wake
    1: "N1",  # Stage 1
    2: "N2",  # Stage 2
    3: "N3",  # Stage 3 (slow wave)
    4: "N3",  # Stage 4 (slow wave, merged with N3 in AASM)
    5: "REM",  # REM
    6: "MOVE",  # Movement time
    9: "UNK",  # Unscored
}

STAGE_TO_INT = {v: k for k, v in STAGE_MAP.items()}


def _record_url(psg_file: str) -> str:
    """Direct PhysioNet URL for a Sleep-EDF record (SC/ST cohort subfolders)."""
    prefix = psg_file[:2]
    subdir = SLEEP_EDF_SUBDIR.get(prefix)
    if subdir is None:
        raise ValueError(f"Unknown Sleep-EDF cohort prefix: {psg_file!r}")
    return f"{SLEEP_EDF_BASE_URL}{subdir}/{psg_file}"


def _check_edf_integrity(path: Path, min_bytes: int) -> bool:
    """True when path looks like a real EDF file (magic + size)."""
    try:
        if path.stat().st_size < min_bytes:
            return False
        with open(path, "rb") as f:
            return f.read(8) == b"0       "
    except OSError:
        return False


def _download_file(url: str, dest: Path, min_bytes: int) -> None:
    """Stream-download url to dest, quarantining poison/incomplete files."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with requests.get(url, stream=True, timeout=60) as response:
            response.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    if chunk:
                        f.write(chunk)
        if not _check_edf_integrity(tmp, min_bytes):
            _quarantine(tmp, url)
        tmp.replace(dest)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _quarantine(path: Path, url: str) -> None:
    """Remove a poison download and report it (kept separate per TRY301)."""
    path.unlink(missing_ok=True)
    raise ValueError(f"Downloaded file failed EDF integrity check: {url}")


def download_sleep_edf(data_dir: Path, subjects: list[str] | None = None) -> Path:
    """Download Sleep-EDF Expanded records from PhysioNet via direct HTTP.

    wfdb.dl_database cannot fetch .edf files, so PSG + hypnogram pairs are
    streamed from the sleep-cassette/ and sleep-telemetry/ subfolders with
    EDF magic + size integrity checks (poison-cache quarantine).

    Args:
        data_dir: Directory for raw Sleep-EDF data.
        subjects: Subject IDs like "SC4001" (default: closure subjects only;
            pass explicit IDs to fetch more; nothing downloads when every
            requested pair is already cached and valid).

    Returns:
        The data directory.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    if subjects is None:
        subjects = list(CLOSURE_SUBJECTS)

    wanted = [f"{s}E0-PSG.edf" for s in subjects] + [f"{s}EC-Hypnogram.edf" for s in subjects]
    missing = [
        f
        for f in wanted
        if not _check_edf_integrity(
            data_dir / f, _MIN_PSG_BYTES if "PSG" in f else _MIN_HYPNOGRAM_BYTES
        )
    ]
    # Quarantine stale invalid files so a retry re-downloads them.
    for f in wanted:
        path = data_dir / f
        if path.exists() and f in missing:
            with contextlib.suppress(OSError):
                path.unlink()

    for record in tqdm(missing, desc="Downloading Sleep-EDF"):
        try:
            _download_file(
                _record_url(record),
                data_dir / record,
                _MIN_PSG_BYTES if "PSG" in record else _MIN_HYPNOGRAM_BYTES,
            )
        except Exception as e:
            logger.warning(f"Failed to download {record}: {e}")

    return data_dir


def _physical_samples(signal: Any) -> npt.NDArray[np.float64]:
    """Convert an edfio signal's raw digital samples to physical units (µV)."""
    digital = np.asarray(signal.digital, dtype=np.float64)
    span = float(signal.digital_max - signal.digital_min)
    if span == 0:
        return digital
    scale = float(signal.physical_max - signal.physical_min) / span
    return np.asarray(float(signal.physical_min) + (digital - float(signal.digital_min)) * scale)


def _annotation_text(annotation: Any) -> str:
    """Best-effort annotation text across edfio versions (.text / .description)."""
    text = getattr(annotation, "text", getattr(annotation, "description", ""))
    if isinstance(text, bytes):
        with contextlib.suppress(UnicodeDecodeError):
            text = text.decode("utf-8", errors="replace")
    return str(text)


def expand_hypnogram_annotations(
    annotations: list[Any],
    epoch_duration: float = EPOCH_DURATION_S,
) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.float64]]:
    """Expand Sleep-EDF bout annotations into fixed 30 s epoch labels.

    Real hypnogram files annotate stage bouts (onset + long duration), not
    individual epochs: each bout emits floor(duration / epoch_duration) epochs
    starting at its onset. Unparseable bouts are skipped; unscored ("?") bouts
    are kept as UNK (9) so kappa excludes them without shifting alignment.

    Returns:
        (hypnogram, hypnogram_times): native Sleep-EDF codes and epoch onsets
        in seconds from recording start.
    """
    stages: list[int] = []
    times: list[float] = []
    for ann in annotations:
        onset = float(getattr(ann, "onset", 0.0) or 0.0)
        duration = float(getattr(ann, "duration", 0.0) or 0.0)
        stage = _parse_hypnogram_stage(_annotation_text(ann))
        if stage is None or duration <= 0:
            continue
        n_epochs = int(duration // epoch_duration)
        for k in range(n_epochs):
            stages.append(stage)
            times.append(onset + k * epoch_duration)
    return np.array(stages, dtype=np.int64), np.array(times, dtype=np.float64)


def load_sleep_edf_record(psg_path: Path, hypnogram_path: Path) -> dict[str, Any]:
    """Load a Sleep-EDF record pair (PSG + Hypnogram).

    Returns:
        Dictionary with:
        - 'eeg': dict of EEG channels (name -> signal array)
        - 'eog': dict of EOG channels
        - 'emg': dict of EMG channels
        - 'other': dict of other channels (airflow, etc.)
        - 'hypnogram': array of sleep stage labels per 30s epoch
        - 'hypnogram_times': timestamps for each epoch (seconds from start)
        - 'fs_dict': sampling rates per channel
        - 'duration_s': total recording duration
        - 'metadata': recording info
    """
    import edfio

    # Load PSG (signals) and hypnogram (bout annotations)
    psg = edfio.read_edf(psg_path)
    hyp = edfio.read_edf(hypnogram_path)

    # Organize channels by type (edfio>=0.4: .label / .sampling_frequency)
    eeg_channels: dict[str, npt.NDArray[np.float64]] = {}
    eog_channels: dict[str, npt.NDArray[np.float64]] = {}
    emg_channels: dict[str, npt.NDArray[np.float64]] = {}
    other_channels: dict[str, npt.NDArray[np.float64]] = {}
    fs_dict: dict[str, int] = {}

    for ch in psg.signals:
        label = str(ch.label).upper()
        signal_data = _physical_samples(ch)
        fs = int(float(ch.sampling_frequency))
        fs_dict[label] = fs

        if "EEG" in label or "FPZ" in label or "PZ" in label or "CZ" in label or "OZ" in label:
            eeg_channels[label] = signal_data
        elif "EOG" in label:
            eog_channels[label] = signal_data
        elif "EMG" in label:
            emg_channels[label] = signal_data
        else:
            other_channels[label] = signal_data

    # Expand bout annotations into fixed 30 s epochs
    hypnogram_arr, hypnogram_times_arr = expand_hypnogram_annotations(
        list(hyp.annotations), epoch_duration=EPOCH_DURATION_S
    )

    duration_s = float(psg.duration)

    metadata = {
        "subject_id": psg_path.stem.replace("-PSG", "").replace("E0", "").replace("EC", ""),
        "header": {
            "patient": str(getattr(psg, "patient", "")),
            "recording": str(getattr(psg, "recording", "")),
            "duration": duration_s,
        },
    }

    return {
        "eeg": eeg_channels,
        "eog": eog_channels,
        "emg": emg_channels,
        "other": other_channels,
        "hypnogram": hypnogram_arr,
        "hypnogram_times": hypnogram_times_arr,
        "fs_dict": fs_dict,
        "duration_s": duration_s,
        "metadata": metadata,
    }


def _parse_hypnogram_stage(description: str) -> int | None:
    """Parse sleep stage from hypnogram annotation description.

    Matches on the trailing token ("Sleep stage W" -> "W", "Movement time" ->
    "TIME") so stage digits never collide with other words. Returns native
    Sleep-EDF codes; "?" (unscored) maps to UNK (9) instead of being dropped.
    """
    text = str(description).strip().upper()
    if not text:
        return None
    token = text.split()[-1]
    return {
        "W": 0,
        "WAKE": 0,
        "0": 0,
        "1": 1,
        "N1": 1,
        "2": 2,
        "N2": 2,
        "3": 3,
        "N3": 3,
        "4": 4,
        "R": 5,
        "REM": 5,
        "5": 5,
        "MOVE": 6,
        "MOVEMENT": 6,
        "TIME": 6,
        "?": 9,
        "UNSCORED": 9,
        "UNK": 9,
        "9": 9,
    }.get(token)


def extract_epochs(
    eeg_signal: npt.NDArray[np.float64],
    fs: int,
    hypnogram: npt.NDArray[np.int64],
    hypnogram_times: npt.NDArray[np.float64],
    epoch_duration: float = 30.0,
) -> dict[str, list[npt.NDArray[np.float64]]]:
    """Extract EEG epochs aligned with hypnogram stages.

    Args:
        eeg_signal: EEG signal array (n_samples,)
        fs: Sampling rate in Hz
        hypnogram: Sleep stage per epoch
        hypnogram_times: Start time of each epoch (seconds)
        epoch_duration: Duration of each epoch in seconds (default 30s)

    Returns:
        Dict mapping stage name -> list of EEG epochs (each epoch is n_samples array)
    """
    epochs_by_stage: dict[str, list[npt.NDArray[np.float64]]] = {
        stage: [] for stage in STAGE_MAP.values()
    }
    epoch_samples = int(epoch_duration * fs)

    for i, (stage_code, epoch_start) in enumerate(zip(hypnogram, hypnogram_times)):
        stage_name = STAGE_MAP.get(stage_code, "UNK")

        start_sample = int(epoch_start * fs)
        end_sample = start_sample + epoch_samples

        if end_sample <= len(eeg_signal):
            epoch = eeg_signal[start_sample:end_sample]
            if len(epoch) == epoch_samples:
                epochs_by_stage[stage_name].append(epoch)

    return epochs_by_stage


def compute_sleep_edf_quality(
    eeg_channels: dict[str, npt.NDArray[np.float64]],
    fs_dict: dict[str, int],
    hypnogram: npt.NDArray[np.int64],
    hypnogram_times: npt.NDArray[np.float64],
    thresholds: QualityThresholds,
) -> dict[str, Any]:
    """Compute EEG quality metrics per channel and per sleep stage."""
    results: dict[str, Any] = {
        "per_channel": {},
        "per_stage": {},
        "overall": {},
    }

    all_flatness = []
    all_alpha = []

    # Per-channel quality (using first EEG channel as representative)
    for ch_name, signal in eeg_channels.items():
        fs = fs_dict.get(ch_name, 100)
        if len(signal) < fs * 10:  # Need at least 10 seconds
            continue

        # Overall quality (eyes-closed sleep state)
        quality = compute_eeg_quality(signal, fs, state="resting_eyes_closed")

        results["per_channel"][ch_name] = {
            "sampling_rate": fs,
            "duration_s": len(signal) / fs,
            "spectral_flatness": quality["spectral_flatness"],
            "alpha_band_ratio": quality["alpha_band_ratio"],
            "band_powers": quality["band_powers"],
            "quality_pass": quality["quality_pass"],
        }

        all_flatness.append(quality["spectral_flatness"])
        all_alpha.append(quality["alpha_band_ratio"])

    # Per-stage quality (using Fpz-Cz if available, else first EEG)
    reference_channel = None
    for preferred in ["EEG FPZ-CZ", "EEG Fpz-Cz", "FPZ-CZ", "Fpz-Cz"]:
        if preferred in eeg_channels:
            reference_channel = preferred
            break
    if reference_channel is None and eeg_channels:
        reference_channel = list(eeg_channels.keys())[0]

    if reference_channel:
        ref_signal = eeg_channels[reference_channel]
        ref_fs = fs_dict.get(reference_channel, 100)

        epochs_by_stage = extract_epochs(ref_signal, ref_fs, hypnogram, hypnogram_times)

        for stage_name, epochs in epochs_by_stage.items():
            if len(epochs) < 3:  # Need at least 3 epochs for meaningful stats
                continue

            stage_flatness = []
            stage_alpha = []

            for epoch in epochs:
                q = compute_eeg_quality(epoch, ref_fs, state="resting_eyes_closed")
                stage_flatness.append(q["spectral_flatness"])
                stage_alpha.append(q["alpha_band_ratio"])

            results["per_stage"][stage_name] = {
                "n_epochs": len(epochs),
                "mean_spectral_flatness": float(np.mean(stage_flatness)),
                "std_spectral_flatness": float(np.std(stage_flatness)),
                "mean_alpha_ratio": float(np.mean(stage_alpha)),
                "std_alpha_ratio": float(np.std(stage_alpha)),
            }

    # Overall
    if all_flatness:
        results["overall"] = {
            "mean_spectral_flatness": float(np.mean(all_flatness)),
            "std_spectral_flatness": float(np.std(all_flatness)),
            "mean_alpha_ratio": float(np.mean(all_alpha)),
            "std_alpha_ratio": float(np.std(all_alpha)),
            "n_channels": len(all_flatness),
        }

    return results


def _build_signal_streams(
    channels: dict[str, npt.NDArray[np.float64]],
    fs_dict: dict[str, int],
    subject_id: str,
    stream_type: str,
    tier: Tier,
    unit: str = "µV",
) -> list[dict[str, Any]]:
    """Build XDF streams for a group of channels."""
    streams = []
    for ch_name, signal in channels.items():
        fs = fs_dict.get(ch_name, 100)
        n_samples = len(signal)

        info = create_stream_info(
            StreamConfig(
                name=f"SYNAPSE_{stream_type}_{subject_id}_{ch_name.replace(' ', '_')}",
                stream_type=f"{stream_type}_T1",
                channel_count=1,
                sampling_rate=fs,
                channel_names=[ch_name],
                channel_units=[unit],
                tier=tier.value,
                metadata={"dataset": "Sleep-EDF", "subject": subject_id, "channel": ch_name},
            )
        )
        timestamps = generate_synthetic_timestamps(n_samples, fs)
        streams.append(
            {
                "info": info,
                "data": signal.reshape(-1, 1).astype(np.float32),
                "timestamps": timestamps.astype(np.float64),
            }
        )
    return streams


def process_sleep_edf_subject(
    psg_file: str,
    data_dir: Path,
    output_dir: Path,
    tier: Tier = Tier.T1,
) -> dict[str, Any]:
    """Process a single Sleep-EDF subject (PSG + Hypnogram pair)."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    psg_path = data_dir / psg_file
    # Real naming: SC4001E0-PSG.edf <-> SC4001EC-Hypnogram.edf (E0->EC swap).
    if psg_file.endswith("E0-PSG.edf"):
        hypnogram_file = psg_file[: -len("E0-PSG.edf")] + "EC-Hypnogram.edf"
    else:  # Fallback for non-canonical names
        hypnogram_file = psg_file.replace("PSG", "Hypnogram")
    hypnogram_path = data_dir / hypnogram_file

    if not psg_path.exists() or not hypnogram_path.exists():
        raise FileNotFoundError(f"Missing PSG or hypnogram: {psg_path}, {hypnogram_path}")

    # Load data
    data = load_sleep_edf_record(psg_path, hypnogram_path)

    eeg_channels = data["eeg"]
    eog_channels = data["eog"]
    emg_channels = data["emg"]
    other_channels = data["other"]
    hypnogram = data["hypnogram"]
    hypnogram_times = data["hypnogram_times"]
    fs_dict = data["fs_dict"]
    duration_s = data["duration_s"]
    metadata = data["metadata"]

    # Tier-aware thresholds
    thresholds = QualityThresholds.for_tier(tier)

    # Compute quality metrics
    quality_results = compute_sleep_edf_quality(
        eeg_channels, fs_dict, hypnogram, hypnogram_times, thresholds
    )

    # Build XDF streams
    streams = []

    # EEG, EOG, EMG, Other streams
    streams.extend(
        _build_signal_streams(eeg_channels, fs_dict, metadata["subject_id"], "EEG", tier)
    )
    streams.extend(
        _build_signal_streams(eog_channels, fs_dict, metadata["subject_id"], "EOG", tier)
    )
    streams.extend(
        _build_signal_streams(emg_channels, fs_dict, metadata["subject_id"], "EMG", tier)
    )
    streams.extend(
        _build_signal_streams(
            other_channels, fs_dict, metadata["subject_id"], "Other", tier, "a.u."
        )
    )

    # Hypnogram marker stream (irregular, one marker per 30s epoch)
    hypnogram_markers = []
    for stage_code, epoch_time in zip(hypnogram, hypnogram_times):
        stage_name = STAGE_MAP.get(stage_code, "UNK")
        hypnogram_markers.append((float(epoch_time), f"Stage_{stage_name}"))

    streams.append(
        create_marker_stream(hypnogram_markers, f"SYNAPSE_Hypnogram_{metadata['subject_id']}")
    )

    # Quality metadata stream
    overall_quality = SignalQualityMetrics(
        spectral_flatness=quality_results["overall"].get("mean_spectral_flatness"),
        alpha_band_ratio=quality_results["overall"].get("mean_alpha_ratio"),
        sampling_rate_hz=100,  # Typical EEG fs
        duration_s=duration_s,
        modality="eeg",
        tier=tier,
        thresholds=thresholds,
    )
    overall_quality_dict = overall_quality.to_dict()
    overall_quality_dict["subject_id"] = metadata["subject_id"]
    overall_quality_dict["dataset"] = "Sleep-EDF"
    overall_quality_dict["per_channel"] = quality_results["per_channel"]
    overall_quality_dict["per_stage"] = quality_results["per_stage"]
    overall_quality_dict["hypnogram_distribution"] = {
        STAGE_MAP.get(k, "UNK"): int(v) for k, v in zip(*np.unique(hypnogram, return_counts=True))
    }

    streams.append(
        create_quality_metadata_stream(
            overall_quality_dict, f"SYNAPSE_Metadata_{metadata['subject_id']}"
        )
    )

    # Write XDF
    xdf_path = output_dir / f"{metadata['subject_id']}_sleep_edf.xdf"
    write_xdf(xdf_path, streams)

    # Save per-subject quality JSON
    with open(output_dir / f"{metadata['subject_id']}_quality.json", "w") as f:
        json.dump(
            {
                "subject_id": metadata["subject_id"],
                "xdf_path": str(xdf_path),
                "fs_dict": fs_dict,
                "duration_s": duration_s,
                "quality": quality_results,
                "overall_quality": overall_quality_dict,
                "metadata": metadata,
            },
            f,
            indent=2,
            default=str,
        )

    return {
        "subject_id": metadata["subject_id"],
        "xdf_path": str(xdf_path),
        "fs_dict": fs_dict,
        "duration_s": duration_s,
        "quality": quality_results,
        "overall_quality": overall_quality_dict,
        "hypnogram": hypnogram.tolist(),
        "hypnogram_times": hypnogram_times.tolist(),
        "metadata": metadata,
    }


def ingest_sleep_edf(
    data_dir: Path = Path("data/sleep_edf"),
    output_dir: Path = Path("data/processed"),
    records: list[str] | None = None,
    tier: Tier = Tier.T1,
) -> list[dict[str, Any]]:
    """Full Sleep-EDF Expanded ingestion pipeline with XDF export.

    Args:
        data_dir: Directory for raw Sleep-EDF data
        output_dir: Directory for processed outputs
        records: List of PSG record filenames to process (default: all available)
        tier: Acquisition tier for threshold selection (default: T1 for sleep)

    Returns:
        List of per-subject results with XDF paths and quality metrics
    """
    data_dir = Path(data_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if records is None:
        download_sleep_edf(data_dir)
    else:
        download_sleep_edf(data_dir, subjects=[r[: -len("E0-PSG.edf")] for r in records])

    # Find available PSG files
    available_psg = sorted([f.name for f in data_dir.glob("*PSG.edf")])

    if records is None:
        records = available_psg
    else:
        # Validate requested records exist
        records = [r for r in records if r in available_psg]

    all_results = []
    for psg_file in tqdm(records, desc="Processing Sleep-EDF subjects"):
        try:
            result = process_sleep_edf_subject(psg_file, data_dir, output_dir, tier)
            all_results.append(result)
        except Exception as e:
            logger.exception(f"Failed to process {psg_file}")

    # Save summary
    summary = {
        "dataset": "Sleep-EDF Expanded",
        "subjects_processed": len(all_results),
        "subjects": [r["subject_id"] for r in all_results],
        "tier": tier.name,
    }
    with open(output_dir / "sleep_edf_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    return all_results
