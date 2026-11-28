"""The validate_live_sync CLI is a loopback rehearsal and reads per_stream."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

from synapse24.utils.xdf import verify_xdf_roundtrip


def _load_script():
    script = Path(__file__).resolve().parents[1] / "scripts" / "validate_live_sync.py"
    spec = importlib.util.spec_from_file_location("validate_live_sync_under_test", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _result() -> dict:
    return {
        "run_id": "abc",
        "overall_pass": True,
        "total_expected": 4,
        "total_recovered": 4,
        "per_stream": [{"name": "ECG", "residual": {"within_10ms_pct": 100.0}}],
        "tier1_marker": {"stream": "SYNAPSE_ECG_T0", "within_1ms_pct": 100.0},
        "xdf_proof": {
            "all_streams_valid": True,
            "n_streams": 1,
            "total_dropped": 0,
            "per_stream": [
                {
                    "name": "SYNAPSE_ECG_T0",
                    "expected": 4,
                    "recovered": 4,
                    "dropped": 0,
                    "valid": True,
                }
            ],
        },
    }


def test_cli_docstring_and_header_say_loopback_rehearsal(capsys) -> None:
    module = _load_script()
    doc = module.__doc__ or ""
    assert "loopback" in doc.lower()
    assert "NOT a live BLE sync gate" in doc
    module._print_header(
        argparse.Namespace(
            duration=1.0,
            ecg_fs=250,
            ppg_fs=100,
            imu_fs=100,
            seed=42,
            resolve_timeout=1.0,
            pull_timeout=1.0,
        )
    )
    printed = capsys.readouterr().out
    assert "LOOPBACK REHEARSAL" in printed
    assert "NOT a live BLE sync gate" in printed


def test_print_xdf_results_reads_per_stream_not_streams(capsys) -> None:
    module = _load_script()
    result = _result()
    assert "streams" not in result["xdf_proof"]  # pin the actual shape
    module._print_xdf_results(result)
    printed = capsys.readouterr().out
    assert "SYNAPSE_ECG_T0: 4/4 samples" in printed


def test_saved_report_is_labelled_as_rehearsal(tmp_path: Path) -> None:
    module = _load_script()
    out = tmp_path / "report.json"
    args = argparse.Namespace(
        output=out,
        duration=1.0,
        ecg_fs=250,
        ppg_fs=100,
        imu_fs=100,
        seed=42,
    )
    module._save_report(args, _result(), _result()["xdf_proof"])
    report = json.loads(out.read_text())
    assert report["test"] == "lsl_2pod_loopback_rehearsal"
    assert report["phase"] == "loopback_rehearsal_not_live_ble"


def test_real_verify_xdf_roundtrip_shape_matches_the_cli(tmp_path: Path) -> None:
    """Guard against the verifier silently changing its per_stream contract."""
    import numpy as np

    proof = verify_xdf_roundtrip(
        [
            {
                "name": "ECG",
                "type": "ECG",
                "data": np.zeros((4, 1)),
                "timestamps": np.arange(4, dtype=np.float64),
                "sampling_rate": 100.0,
            }
        ],
        tmp_path / "proof.xdf",
    )
    assert "per_stream" in proof
    assert "streams" not in proof
    assert set(proof["per_stream"][0]) >= {"name", "expected", "recovered", "dropped", "valid"}
