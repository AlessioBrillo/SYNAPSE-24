# Band fixtures

`band_scenarios_v1.json` holds **synthetic** band sessions with an answer key, for the parsers and
screens that consume the contract before a real band exists. Nothing in it is a measurement.

* `raw[]` — raw research frames (`contracts/raw_frame_v1.json`) as a phone receives them, each with
  `recv_ms` (the phone's monotonic clock since the recording started) and `hex`. Scenarios: a clean
  session, lost frames, a band restart, a 32-bit timestamp wrap, a device-reported loss, invalid
  frames, a repeated and a late frame.
* `l1` — two minutes of feature frames (`contracts/gatt_v2.json`) at 1 Hz, with sentinels, two lost
  frames, a restart and one frame of an unknown version.
* `expected` — what a correct consumer concludes, from a reference implementation of the rules in
  `raw_frame_v1.json` (the same sequence rule as Averyn's `SynapseSequenceTracker`).

Regenerate with `uv run python scripts/make_band_fixtures.py`; `--check` fails on drift. The bytes
come from `synapse_device.codec.encode_frame` and `synapse_device.consumer.encode_feature`, never
from a hand-written struct.
