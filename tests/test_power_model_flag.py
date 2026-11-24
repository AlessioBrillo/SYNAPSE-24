"""A POWER frame must say whether its current is measured or modelled."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from synapse_device.codec import (
    FLAG_POWER_IS_MODEL,
    FLAG_TS_EPOCH_WRAPPED,
    RawStream,
    encode_frame,
)

from synapse24.hardware.research_client import ResearchStreamDecoder

ROOT = Path(__file__).resolve().parents[1]


def test_flag_masks_are_distinct_and_generated() -> None:
    assert FLAG_TS_EPOCH_WRAPPED == 0x01
    assert FLAG_POWER_IS_MODEL == 0x02
    manifest = yaml.safe_load((ROOT / "config" / "hardware.yaml").read_text(encoding="utf-8"))
    assert manifest["raw_frame"]["flag_bits"]["power_is_model"] == 1
    contract = json.loads((ROOT / "contracts" / "raw_frame_v1.json").read_text(encoding="utf-8"))
    assert contract["flag_bits"]["power_is_model"] == 1
    header = (ROOT / "firmware" / "common" / "ble" / "synapse_raw_frame.h").read_text(
        encoding="utf-8"
    )
    assert "SYNAPSE_RAW_FLAG_POWER_IS_MODEL 0x02u" in header


def test_decoder_counts_modelled_power_frames() -> None:
    frame = encode_frame(
        RawStream.POWER,
        sequence=1,
        timestamp_us=1000,
        flags=FLAG_POWER_IS_MODEL,
        fmt=3,  # s32
        samples=[3700, 115, 425, 100],
    )
    decoder = ResearchStreamDecoder()
    header = decoder.feed(frame)
    assert header.power_is_model is True
    assert decoder.power_model_frames == 1

    measured = encode_frame(
        RawStream.POWER,
        sequence=2,
        timestamp_us=2000,
        flags=0,
        fmt=3,
        samples=[3700, 100, 370, 100],
    )
    decoder.feed(measured)
    assert decoder.power_model_frames == 1
