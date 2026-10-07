"""Tests for stream capability descriptors in XDF/LSL export."""

from __future__ import annotations

import json

from synapse24.utils.xdf import StreamCapability, StreamConfig, create_stream_info


def test_stream_capability_embedding() -> None:
    """Test embedding stream capability descriptor into LSL stream info."""
    cap = StreamCapability(
        stream_type="EEG",
        units="uV",
        sampling_rate=250.0,
        quality_metrics={"snr_db": 20.0, "impedance_kohm": 5.0},
    )

    config = StreamConfig(
        name="test_stream",
        stream_type="EEG",
        channel_count=1,
        sampling_rate=250.0,
        capability=cap,
    )

    info = create_stream_info(config)

    # Check if capability is in description
    desc = info.desc()
    cap_node = desc.child("capability")
    assert not cap_node.empty()

    descriptor_json = cap_node.child_value("descriptor")
    descriptor = json.loads(descriptor_json)

    assert descriptor["stream_type"] == "EEG"
    assert descriptor["quality_metrics"]["snr_db"] == 20.0
