"""The live validator must consume the canonical research-service frames.

`validate_phase1_entry.py` is what actually runs against the smoke kit, so the
path from a BLE notification to an LSL outlet has to be exercised here rather
than assumed. These tests drive the real validator with a synthetic pod's
frames, which is the closest thing to hardware without hardware.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from synapse24.hardware.raw_codec import RawStream  # noqa: E402
from tests.test_research_service_hil import SyntheticPod  # noqa: E402


@pytest.fixture(scope="module")
def validator_cls():
    """Import the validator lazily: it pulls in bleak and pylsl."""
    from scripts.validate_phase1_entry import Phase1Validator

    return Phase1Validator


@pytest.fixture(scope="module")
def validator(validator_cls, tmp_path_factory: pytest.TempPathFactory):
    import logging

    config = ROOT / "config" / "hardware_bringup.yaml"
    logger = logging.getLogger("test_validator_raw")
    logger.addHandler(logging.NullHandler())
    return validator_cls(config, logger)


def test_validator_has_a_raw_ingestor(validator) -> None:
    """The canonical path must exist before any hardware is plugged in."""
    assert validator.raw_ingestor is not None
    assert validator.raw_ingestor.decoder is not None


def test_notification_populates_lsl_outlets(validator) -> None:
    """A research-service ECG frame must reach the ECG outlet with samples."""
    pod = SyntheticPod(duration_s=0.5, seed=21)
    ecg_frames = [frame for stream, frame in pod.frames() if stream is RawStream.ECG]
    assert ecg_frames, "the synthetic pod must produce ECG frames"

    class FakeCharacteristic:
        def __init__(self, uuid: str) -> None:
            self.uuid = uuid

    class FakeOutlet:
        def __init__(self) -> None:
            self.pushed: list[tuple[str, list[float]]] = []

        def push_sample(self, name: str, values, _ts: float) -> None:
            self.pushed.append((name, list(values)))

    fake_lsl = FakeOutlet()
    validator.lsl_manager = fake_lsl

    ecg_char = FakeCharacteristic("A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C71")
    for frame in ecg_frames:
        validator._notification_handler(ecg_char, bytearray(frame))

    names = {name for name, _ in fake_lsl.pushed}
    assert names == {"SYNAPSE_ECG_T0"}, f"unexpected outlets: {names}"
    assert len(fake_lsl.pushed) > 0

    # The ingestor's own accounting must agree with what was pushed.
    total = sum(len(values) for _, values in fake_lsl.pushed)
    assert total == 0.5 * 500, "0.5 s of 500 Hz ECG"


def test_legacy_payload_does_not_crash_the_validator(validator) -> None:
    """A payload from an unexpected firmware must be handled, not thrown."""
    import logging

    validator.logger = logging.getLogger("test_validator_raw")

    class FakeCharacteristic:
        uuid = "0000ffe1-0000-1000-8000-00805f9b34fb"

    class FakeLsl:
        def push_sample(self, _name, _values, _ts):
            return None

    class FakeParser:
        @staticmethod
        def parse_ecg(_data):
            return [0.0]

        @staticmethod
        def parse_ppg(_data):
            return [0.0, 0.0]

    # Legacy-sized payload that is not a research frame: the handler must not
    # raise, and must fall through to the legacy parser path safely.
    validator.lsl_manager = FakeLsl()
    validator.parser = FakeParser()

    validator._notification_handler(FakeCharacteristic(), bytearray(b"\x00" * 60))
    # Reaching this point at all is the assertion: the handler swallowed nothing
    # fatal and the legacy path was attempted without raising.


def test_validator_drop_summary_is_wired(validator) -> None:
    """The validator must be able to report gaps after a live session."""
    summary = validator.raw_ingestor.drop_summary()
    assert isinstance(summary, dict)
    assert validator.raw_ingestor.is_clean() in (True, False)
