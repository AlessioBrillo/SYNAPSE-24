import pytest
import json
from synapse24.utils.xdf import StreamConfig, StreamCapability, create_stream_info

def test_stream_capability_embedding():
    cap = StreamCapability(
        stream_type="EEG",
        units="uV",
        sampling_rate=250.0,
        quality_metrics={"snr_db": 20.0, "impedance_kohm": 5.0}
    )
    
    config = StreamConfig(
        name="test_stream",
        stream_type="EEG",
        channel_count=1,
        sampling_rate=250.0,
        capability=cap
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
