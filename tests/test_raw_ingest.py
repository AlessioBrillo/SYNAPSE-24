"""Tests for the live validator's raw-frame ingestion path.

The validator must accept the Synapse Research Service frame format (the
canonical one) and still say so honestly when a payload is not that format, so
a legacy firmware gets a clear answer rather than a silent mis-parse.
"""

from __future__ import annotations

from synapse24.hardware.raw_codec import RawFormat, RawStream, encode_frame
from synapse24.hardware.raw_ingest import OUTLET_BY_STREAM, RawFrameIngestor, ingest_frame_bytes


class _Recorder:
    """Captures what the ingestor pushes to LSL."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[float], float]] = []

    def __call__(self, outlet: str, values: list[float], ts: float) -> None:
        self.calls.append((outlet, values, ts))


def _frame(stream: RawStream, seq: int, values: list[int]) -> bytes:
    return encode_frame(stream, seq, 1_000_000 + seq, 0, RawFormat.S16, values)


def test_outlets_match_the_bringup_manifest() -> None:
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    with (root / "config" / "hardware_bringup.yaml").open(encoding="utf-8") as handle:
        bringup = yaml.safe_load(handle)
    declared = {s["name"] for s in bringup["bringup"]["lsl"]["streams"]}

    for outlet in OUTLET_BY_STREAM.values():
        assert outlet in declared, f"{outlet} is not an outlet the validator creates"


def test_ecg_frame_is_forwarded_to_the_right_outlet() -> None:
    recorder = _Recorder()
    ingestor = RawFrameIngestor(push_callback=recorder)

    accepted = ingestor.handle_notification(
        "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C71", _frame(RawStream.ECG, 0, [10, 20, 30])
    )

    assert accepted
    assert [c[0] for c in recorder.calls] == ["SYNAPSE_ECG_T0"] * 3
    # Engineering units, the same conversion ResearchStreamDecoder.arrays
    # applies: the LSL streams and the decoded arrays must not be two
    # different signals from one wire. ECG quantises to microvolts, so the
    # raw counts 10/20/30 arrive as 0.010/0.020/0.030.
    assert [c[1] for c in recorder.calls] == [[0.01], [0.02], [0.03]]
    assert ingestor.sample_counts["ecg"] == 3


def test_ppg_frame_spreads_across_channels() -> None:
    """PPG carries red and IR, so one sample row must have two values."""
    recorder = _Recorder()
    ingestor = RawFrameIngestor(push_callback=recorder)

    # Interleaved red, IR, red, IR.
    payload = encode_frame(RawStream.PPG, 0, 1000, 0, RawFormat.S32, [100, 200, 300, 400])
    accepted = ingestor.handle_notification("A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C72", payload)

    assert accepted
    assert [c[1] for c in recorder.calls] == [[100.0, 200.0], [300.0, 400.0]]
    assert ingestor.sample_counts["ppg"] == 4


def test_legacy_payload_is_reported_not_misparsed() -> None:
    """A payload that is not a research frame must be declined, not guessed at."""
    ingestor = RawFrameIngestor(push_callback=_Recorder())
    legacy = b"\x00" * 60  # the old sensor_sample_t notification

    assert ingestor.handle_notification("anything", legacy) is False
    assert ingestor.decoder.frames_rejected == 1


def test_gaps_survive_the_ingestion_path() -> None:
    """A dropped notification must still be visible after ingestion."""
    ingestor = RawFrameIngestor(push_callback=_Recorder())
    char = "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C71"

    for seq in (0, 1, 5):  # 2, 3 and 4 are missing
        ingestor.handle_notification(char, _frame(RawStream.ECG, seq, [1, 2]))

    assert not ingestor.is_clean()
    assert ingestor.drop_summary() == {"ECG": 3}


def test_ingest_frame_bytes_reports_bad_payloads() -> None:
    stream, samples = ingest_frame_bytes(_frame(RawStream.ECG, 0, [1, 2, 3]))
    assert stream is RawStream.ECG
    assert samples.tolist() == [1, 2, 3]

    stream, samples = ingest_frame_bytes(b"garbage")
    assert stream is None
    assert samples.size == 0


def test_ingestor_counts_by_lsl_stream_name() -> None:
    """Counts are keyed by the lowercase stream name the report already uses."""
    ingestor = RawFrameIngestor(push_callback=_Recorder())
    ingestor.handle_notification(
        "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C73", _frame(RawStream.ACC, 0, [1, 2, 3, 4, 5, 6])
    )
    assert ingestor.sample_counts == {"acc": 6}
