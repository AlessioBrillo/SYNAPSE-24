"""T10 must return a real residual, and refuse uncorrelated or short runs."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.measure_two_pod_sync import residual_ms  # noqa: E402
from synapse24.utils.xdf import write_xdf  # noqa: E402


def _motion(duration_s: float = 40.0) -> tuple[np.ndarray, np.ndarray]:
    t = np.arange(0.0, duration_s, 0.02)
    value = 1.0 + 0.8 * np.sin(2 * np.pi * 1.3 * t) + 0.2 * np.sin(2 * np.pi * 4.1 * t)
    return t, value


def _write_xdf(path: Path, stamps: np.ndarray, values: np.ndarray, source_id: str = "") -> None:
    write_xdf(
        path,
        [
            {
                "info": {
                    "name": "SYNAPSE_ACC_T0",
                    "type": "ACC",
                    "channel_count": 3,
                    "sampling_rate": 50,
                    "channel_format": "float32",
                    "source_id": source_id,
                },
                "data": np.column_stack([values, np.zeros_like(values), np.zeros_like(values)]),
                "timestamps": stamps,
            }
        ],
    )


def test_a_known_lag_is_recovered_within_the_gate() -> None:
    t, value = _motion()
    result = residual_ms(t, value, t + 0.007, value, min_overlap_s=5.0)
    assert result["correlated"] is True
    assert result["residual_ms"] is not None
    assert abs(result["residual_ms"] - 7.0) < 3.0


def test_a_drifting_clock_is_judged_by_the_worst_window() -> None:
    t, value = _motion()
    # B starts 3 ms ahead and lags by 19 ms at the end: the median is ~11 ms
    # but the final window is far outside the 10 ms criterion.
    shifted = t + 0.003 + 0.0005 * t
    result = residual_ms(t, value, shifted, value, min_overlap_s=5.0)
    from statistics import median

    lags = [abs(item["lag_ms"]) for item in result["per_window"]]
    assert result["correlated"] is True
    assert result["residual_ms"] is not None
    assert result["residual_ms"] == max(lags)
    assert result["residual_ms"] > median(lags) + 2


def test_a_short_overlap_gives_no_residual() -> None:
    t, value = _motion(duration_s=10.0)
    result = residual_ms(t, value, t, value, min_overlap_s=5.0)
    assert result["correlated"] is False
    assert result["residual_ms"] is None
    assert "overlap" in result["reason"]


def test_uncorrelated_motion_is_refused() -> None:
    t, value = _motion()
    rng = np.random.default_rng(42)
    result = residual_ms(t, value, t, rng.normal(size=value.shape), min_overlap_s=5.0)
    assert result["correlated"] is False
    assert result["residual_ms"] is None


def test_the_cli_writes_evidence_for_two_real_xdf_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import measure_two_pod_sync as m2p

    # T10 is a 30-minute claim: the synthetic streams must span it too.
    t, value = _motion(duration_s=1810.0)
    a = tmp_path / "a.xdf"
    b = tmp_path / "b.xdf"
    out = tmp_path / "sync.json"
    _write_xdf(a, t, value, source_id="pod-a")
    _write_xdf(b, t + 0.007, value, source_id="pod-b")
    assert m2p.main(["--pods", str(a), str(b), "--out", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["pass"] is True
    assert payload["overlap_s"] >= 1800
    assert payload["threshold_ms"] == 10.0
    assert payload["method"].startswith("ACC magnitude")


def test_the_threshold_is_taken_from_the_cli(tmp_path: Path) -> None:
    from scripts import measure_two_pod_sync as m2p

    t, value = _motion(duration_s=1810.0)
    a = tmp_path / "a.xdf"
    b = tmp_path / "b.xdf"
    out = tmp_path / "sync.json"
    _write_xdf(a, t, value, source_id="pod-a")
    _write_xdf(b, t + 0.02, value, source_id="pod-b")
    assert m2p.main(["--pods", str(a), str(b), "--threshold-ms", "5", "--out", str(out)]) == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["threshold_ms"] == 5.0
    assert payload["pass"] is False


def test_two_captures_from_the_same_pod_are_refused(tmp_path: Path) -> None:
    from scripts import measure_two_pod_sync as m2p

    t, value = _motion(duration_s=1810.0)
    a = tmp_path / "a.xdf"
    b = tmp_path / "b.xdf"
    out = tmp_path / "sync.json"
    _write_xdf(a, t, value, source_id="pod-a")
    _write_xdf(b, t + 0.007, value, source_id="pod-a")
    assert m2p.main(["--pods", str(a), str(b), "--out", str(out)]) == 2
    assert not out.exists()
