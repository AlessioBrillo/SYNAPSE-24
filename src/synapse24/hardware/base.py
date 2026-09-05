"""Base classes for hardware abstraction."""

from __future__ import annotations

import contextlib
import time
import types
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import numpy as np
import numpy.typing as npt

try:
    import brainflow
    from brainflow import BoardIds, BoardShim, BrainFlowError, BrainFlowInputParams
except ImportError:  # pragma: no cover - brainflow not available in all envs
    brainflow = None
    BoardIds = None
    BoardShim = None
    BrainFlowInputParams = None
    BrainFlowError = Exception

from synapse24.signal_quality import Tier

# Base board adapters registry (empty, populated by adapter modules)
BOARD_ADAPTERS: dict[str, type] = {}


class BoardState(Enum):
    """Board connection state."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    STREAMING = "streaming"
    ERROR = "error"


@dataclass(frozen=True)
class BoardConfig:
    """Immutable board configuration."""

    board_id: str  # BrainFlow board ID string (e.g., "SYNTHETIC_BOARD")
    serial_port: str = ""
    mac_address: str = ""
    ip_address: str = ""
    ip_port: int = 0
    ip_protocol: int = 0
    other_info: str = ""
    timeout: int = 0
    sampling_rate: int = 0  # 0 = use board default
    channels: list[int] = field(default_factory=list)  # empty = all channels
    preset: str = "default"  # for boards with multiple presets

    def to_brainflow_params(self) -> BrainFlowInputParams:
        """Convert to BrainFlow input parameters."""
        if BrainFlowInputParams is None:
            raise RuntimeError("brainflow not installed")
        params = BrainFlowInputParams()
        params.serial_port = self.serial_port
        params.mac_address = self.mac_address
        params.ip_address = self.ip_address
        params.ip_port = self.ip_port
        params.ip_protocol = self.ip_protocol
        params.other_info = self.other_info
        params.timeout = self.timeout
        return params

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BoardConfig:
        """Create from dictionary."""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class BoardManager:
    """Manages a BrainFlow board session with lifecycle management."""

    def __init__(self, config: BoardConfig) -> None:
        """Initialize board manager."""
        self.config = config
        self._board: BoardShim | None = None
        self._state = BoardState.DISCONNECTED
        self._board_id: int | None = None
        self._n_channels: int = 0
        self._sampling_rate: int = 0
        self._channel_names: list[str] = []
        self._channel_units: list[str] = []

    @property
    def state(self) -> BoardState:
        return self._state

    @property
    def is_streaming(self) -> bool:
        return self._state == BoardState.STREAMING

    @property
    def n_channels(self) -> int:
        return self._n_channels

    @property
    def sampling_rate(self) -> int:
        return self._sampling_rate

    @property
    def channel_names(self) -> list[str]:
        return self._channel_names

    @property
    def channel_units(self) -> list[str]:
        return self._channel_units

    def _resolve_board_id(self) -> int:
        """Resolve board ID string to BrainFlow integer constant."""
        if BoardIds is None:
            raise RuntimeError("brainflow not installed")
        if not hasattr(BoardIds, self.config.board_id):
            raise ValueError(f"Unknown board ID: {self.config.board_id}")
        board_id_attr: int = getattr(BoardIds, self.config.board_id)
        return board_id_attr

    def prepare(self) -> None:
        """Prepare the board session (connect, configure)."""
        if self._state != BoardState.DISCONNECTED:
            raise RuntimeError(f"Board already prepared (state: {self._state.value})")

        if brainflow is None:
            raise RuntimeError("brainflow not installed")

        self._state = BoardState.CONNECTING
        board_id = self._resolve_board_id()
        params = self.config.to_brainflow_params()

        try:
            self._board = BoardShim(board_id, params)
            self._board.prepare_session()
            self._board_id = board_id

            # Get board info
            self._n_channels = BoardShim.get_num_rows(board_id, self.config.preset)
            self._sampling_rate = (
                self.config.sampling_rate
                if self.config.sampling_rate > 0
                else BoardShim.get_sampling_rate(board_id, self.config.preset)
            )

            # Get channel names/units if available
            try:
                names = BoardShim.get_eeg_names(board_id, self.config.preset)
                if names:
                    self._channel_names = names.split(",")
            except Exception:
                self._channel_names = [f"CH{i}" for i in range(self._n_channels)]

            self._channel_units = ["µV"] * self._n_channels  # Default

            self._state = BoardState.CONNECTED

        except BrainFlowError as e:
            self._state = BoardState.ERROR
            if self._board:
                with contextlib.suppress(Exception):
                    self._board.release_session()
                self._board = None
            raise RuntimeError(f"Failed to prepare board: {e}") from e

    def start_stream(self, buffer_size: int = 450000) -> None:
        """Start data streaming."""
        if self._state != BoardState.CONNECTED:
            raise RuntimeError(f"Board not ready (state: {self._state.value})")

        if self._board is None:
            raise RuntimeError("Board not initialized")

        self._board.start_stream(buffer_size)
        self._state = BoardState.STREAMING

    def stop_stream(self) -> None:
        """Stop data streaming."""
        if self._board and self._state == BoardState.STREAMING:
            self._board.stop_stream()
            self._state = BoardState.CONNECTED

    def get_board_data(self, num_samples: int | None = None) -> npt.NDArray[np.float64]:
        """Get board data as (n_channels, n_samples) array."""
        if self._board is None:
            raise RuntimeError("Board not initialized")

        if num_samples is None:
            return np.asarray(self._board.get_board_data(), dtype=np.float64)
        return np.asarray(self._board.get_current_board_data(num_samples), dtype=np.float64)

    def get_board_timestamp(self) -> npt.NDArray[np.float64]:
        """Get timestamps for the last data chunk."""
        if self._board is None:
            raise RuntimeError("Board not initialized")
        data = self._board.get_board_data()
        if data.size == 0:
            return np.array([], dtype=np.float64)
        # Last row is timestamp
        return data[-1, :] if data.shape[0] > self._n_channels else np.array([], dtype=np.float64)

    def release(self) -> None:
        """Release the board session."""
        if self._board:
            with contextlib.suppress(Exception):
                if self._state == BoardState.STREAMING:
                    self._board.stop_stream()
                self._board.release_session()
            self._board = None
        self._state = BoardState.DISCONNECTED

    def __enter__(self) -> BoardManager:
        self.prepare()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        self.release()

    def session(self) -> BoardSession:
        """Return a context manager for a complete session."""
        return BoardSession(self)


class BoardSession:
    """Context manager for a complete board session."""

    def __init__(self, manager: BoardManager) -> None:
        self.manager = manager

    def __enter__(self) -> BoardManager:
        self.manager.prepare()
        self.manager.start_stream()
        return self.manager

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        self.manager.stop_stream()
        self.manager.release()


class BoardAdapter(ABC):
    """Abstract base for board-specific adapters."""

    @property
    @abstractmethod
    def board_id(self) -> str:
        """BrainFlow board ID string."""

    @property
    @abstractmethod
    def default_sampling_rate(self) -> int:
        """Default sampling rate in Hz."""

    @property
    @abstractmethod
    def default_channels(self) -> dict[str, list[int]]:
        """Default channel mapping by modality."""

    @abstractmethod
    def create_config(self, **overrides: Any) -> BoardConfig:
        """Create board configuration with overrides."""

    @abstractmethod
    def get_stream_mapping(self) -> dict[str, dict[str, Any]]:
        """Get LSL stream mapping for this board."""


@dataclass(frozen=True)
class SensorPodConfig:
    """Configuration for a sensor pod."""

    pod_id: str
    name: str
    board_type: str  # References BoardAdapter class name
    modalities: list[str]
    tier: Tier
    channels: int | dict[str, int]
    sampling_rate: int | dict[str, int]
    ble_address: str = ""
    serial_port: str = ""
    placement: str = ""
    electrode_type: str = ""
    is_hub: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_board_config(self, **overrides: Any) -> BoardConfig:
        """Convert to BoardConfig for acquisition."""
        from dataclasses import replace

        from .registry import BOARD_ADAPTERS

        adapter_class = BOARD_ADAPTERS.get(self.board_type)
        if adapter_class is None:
            raise ValueError(f"Unknown board type: {self.board_type}")

        adapter = adapter_class()
        config: BoardConfig = adapter.create_config()

        # Prepare overrides dict
        config_overrides = dict(overrides)
        if self.serial_port:
            config_overrides["serial_port"] = self.serial_port
        if self.ble_address:
            config_overrides["mac_address"] = self.ble_address

        # Apply overrides using dataclasses.replace (since BoardConfig is frozen)
        return replace(config, **config_overrides)


class DeviceRegistry:
    """Registry of all sensor pods and their configurations."""

    def __init__(self) -> None:
        self._pods: dict[str, SensorPodConfig] = {}

    def register(self, pod: SensorPodConfig) -> None:
        """Register a sensor pod."""
        if pod.pod_id in self._pods:
            raise ValueError(f"Pod {pod.pod_id} already registered")
        self._pods[pod.pod_id] = pod

    def unregister(self, pod_id: str) -> None:
        """Unregister a sensor pod."""
        self._pods.pop(pod_id, None)

    def get(self, pod_id: str) -> SensorPodConfig:
        """Get pod configuration by ID."""
        if pod_id not in self._pods:
            raise KeyError(f"Pod {pod_id} not found")
        return self._pods[pod_id]

    def get_by_tier(self, tier: Tier) -> list[SensorPodConfig]:
        """Get all pods for a specific tier."""
        return [p for p in self._pods.values() if p.tier == tier]

    def get_by_modality(self, modality: str) -> list[SensorPodConfig]:
        """Get all pods with a specific modality."""
        return [p for p in self._pods.values() if modality in p.modalities]

    def all(self) -> list[SensorPodConfig]:
        """Get all registered pods."""
        return list(self._pods.values())

    def load_from_yaml(self, path: Path) -> None:
        """Load pod configurations from YAML file.

        Guardian security checklist: BLE MACs / serials never hardcoded.
        ``ble_address`` / ``serial_port`` support ``${ENV_VAR}`` expansion
        (e.g. ``${SYNAPSE_HEAD_BLE}``); unset vars resolve to empty string
        and the pod must be provisioned at deploy time, not in git.
        """
        import os
        import re

        import yaml

        def _expand_env(value: str) -> str:
            """Expand $VAR / ${VAR}; unset vars become empty (never a fake MAC)."""
            expanded = os.path.expandvars(value)
            return re.sub(
                r"\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*",
                "",
                expanded,
            )

        with open(path) as f:
            data = yaml.safe_load(f)

        pods = data.get("pods", [])
        if isinstance(pods, dict):
            pod_items = pods.values()
        else:
            pod_items = pods
        for pod_data in pod_items:
            sampling_rate = pod_data.get("sampling_rate", pod_data.get("sampling_rates", 0))
            pod = SensorPodConfig(
                pod_id=pod_data["pod_id"],
                name=pod_data["name"],
                board_type=pod_data["board_type"],
                modalities=pod_data["modalities"],
                tier=Tier(pod_data["tier"]),
                channels=pod_data["channels"],
                sampling_rate=sampling_rate,
                ble_address=_expand_env(str(pod_data.get("ble_address", ""))),
                serial_port=_expand_env(str(pod_data.get("serial_port", ""))),
                placement=pod_data.get("placement", ""),
                electrode_type=pod_data.get("electrode_type", ""),
                is_hub=pod_data.get("is_hub", False),
                metadata=pod_data.get("metadata", {}),
            )
            self.register(pod)

    def save_to_yaml(self, path: Path) -> None:
        """Save pod configurations to YAML file."""
        import yaml

        data: dict[str, list[dict[str, Any]]] = {"pods": []}
        for pod in self._pods.values():
            data["pods"].append(
                {
                    "pod_id": pod.pod_id,
                    "name": pod.name,
                    "board_type": pod.board_type,
                    "modalities": pod.modalities,
                    "tier": pod.tier.value,
                    "channels": pod.channels,
                    "sampling_rate": pod.sampling_rate,
                    "ble_address": pod.ble_address,
                    "serial_port": pod.serial_port,
                    "placement": pod.placement,
                    "electrode_type": pod.electrode_type,
                    "is_hub": pod.is_hub,
                    "metadata": pod.metadata,
                }
            )

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            yaml.safe_dump(data, f, sort_keys=False)
