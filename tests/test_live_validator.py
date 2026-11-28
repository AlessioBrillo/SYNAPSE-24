"""The live Tier-0 validator must write the whole session and fail closed."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from synapse24.acquisition.live_validator import (
    EXPECTED_STREAMS,
    LiveTier0Validator,
    ValidatorConfig,
    run_live_validation,
)

try:
    from pylsl import local_clock
except ImportError:  # pragma: no cover
    local_clock = None


def _now() -> float:
    return local_clock() if local_clock else time.time()


def _seed_spool(validator: LiveTier0Validator, n: int, duration_s: float) -> None:
    """Simulate a full session written sample-by-sample (as run() does)."""
    t0 = _now() - duration_s
    validator._start_time = t0
    for i in range(n):
        ts = t0 + i * duration_s / n
        validator.spool.append("ecg", [float(i)], ts)
        validator.spool.append("ppg", [0.5, 0.4], ts)
        validator.spool.append("acc", [1.0], ts)


def test_configured_streams_are_all_required() -> None:
    validator = LiveTier0Validator(ValidatorConfig(save_xdf=False, save_json=False))
    validator._start_time = _now()
    with pytest.raises(RuntimeError, match="Every configured stream must connect"):
        validator.run(duration_s=1.0)


def test_run_live_validation_requires_every_stream(monkeypatch) -> None:
    monkeypatch.setattr(
        LiveTier0Validator,
        "connect",
        lambda _self: {"ecg": True, "ppg": False, "acc": True},
    )
    with pytest.raises(RuntimeError, match="missing: ppg"):
        run_live_validation(duration_s=1.0)


def test_completeness_gate_fails_on_low_rate() -> None:
    validator = LiveTier0Validator(ValidatorConfig(save_xdf=False, save_json=False))
    gate = validator._completeness_gate({"ecg": 10, "ppg": 10, "acc": 10}, elapsed_s=10.0)
    assert gate["overall_pass"] is False
    assert gate["streams"]["ecg"]["pass"] is False
    assert gate["streams"]["ecg"]["observed_rate_hz"] == pytest.approx(1.0)


def test_completeness_gate_passes_at_nominal_rate() -> None:
    validator = LiveTier0Validator(ValidatorConfig(save_xdf=False, save_json=False))
    gate = validator._completeness_gate({"ecg": 2500, "ppg": 1000, "acc": 1000}, elapsed_s=10.0)
    assert gate["overall_pass"] is True


def test_finalize_writes_the_full_session_not_the_tail(tmp_path: Path) -> None:
    import pyxdf

    validator = LiveTier0Validator(ValidatorConfig(output_dir=tmp_path))
    n = 1200
    _seed_spool(validator, n=n, duration_s=4.0)
    # The rolling analysis buffers are deliberately tiny, as they are after
    # an assessment has trimmed them to the 5 s tail.
    validator._ecg_buffer = validator._ecg_buffer[-10:]
    validator._ppg_red_buffer = validator._ppg_red_buffer[-10:]

    result = validator._finalize()

    assert result["sample_counts"] == {"ecg": n, "ppg": n, "acc": n}
    assert result["all_streams_connected"] is False  # no inlets in this unit test
    xdf = Path(result["xdf_path"])
    streams, _ = pyxdf.load_xdf(str(xdf))
    by_name = {s["info"]["name"][0]: s for s in streams}
    assert len(by_name["SYNAPSE_ECG_T0"]["time_stamps"]) == n
    assert len(by_name["SYNAPSE_PPG_T0"]["time_stamps"]) == n
    assert len(by_name["SYNAPSE_ACC_T0"]["time_stamps"]) == n
    assert len(by_name["SYNAPSE_ECG_T0"]["time_stamps"]) > 10  # not the 10-sample tail

    summary = json.loads(Path(result["json_path"]).read_text())
    assert summary["session"]["sample_counts"]["ecg"] == n
    assert summary["completeness"]["streams"]["ecg"]["samples"] == n


def test_expected_streams_constant() -> None:
    assert EXPECTED_STREAMS == ("ecg", "ppg", "acc")
