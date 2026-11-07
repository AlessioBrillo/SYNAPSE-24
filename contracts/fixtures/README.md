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

## Phone sidecars (`session_60s.*`)

What the phone writes while recording, as Averyn stores it (ADR-0030): one append-only file per
kind, each record `u16 length | i64 t_rx_ns | frame bytes` (little endian; `t_rx_ns` is the phone's
monotonic clock since the recording began; the frame is exactly what was notified).

* `session_60s.synapse-raw.bin` — raw research frames (`raw_frame_v1.json`): a minute of ECG, PPG,
  ACC, GYRO and MAG with an ECG band restart, four lost ECG frames, nine samples the band reports
  dropped on the PPG stream, a repeated ACC frame and one frame of an unknown version.
* `session_60s.synapse.bin` — consumer v2 records (`gatt_v2.json`): a feature frame per second (two
  lost on the link, one of an unknown version, a warm-up with sentinels) and a status record every
  5 s. The answer key's `lost_frames` is 3, not 2: the unknown-version frame was skipped but used a
  sequence number, so a reader sees a hole there too and counts that frame apart as unknown; the two kinds are told apart by length (27 B and 40 B).
* `session_60s.expected.json` — the answer key: per stream the frames, values, lost frames, restarts,
  out-of-order frames, device-reported drops and the device timeline extended with the wrap flag.

Regenerate with `uv run python scripts/make_band_sidecars.py`; `--check` fails on drift. Reading the
files by hand is part of `tests/test_band_sidecars.py`, which checks the key against the faults that
were injected, not against the oracle that computed it.
