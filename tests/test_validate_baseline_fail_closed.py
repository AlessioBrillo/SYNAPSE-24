"""validate_baseline.py fails closed (non-zero exit) on missing/unverified datasets.

Refuses to score empty/partial data instead of reporting a misleading
0.0 accuracy. See READINESS_GATE.md "Known failures on the dev host".
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "validate_baseline.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("validate_baseline_failclosed", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["validate_baseline_failclosed"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def baseline_module():
    return _load_module()


def test_require_verified_datasets_raises_on_missing_wesad(baseline_module, tmp_path):
    """An empty data dir must raise DatasetIntegrityError for 'wesad'."""
    empty = tmp_path / "data"
    empty.mkdir()
    with pytest.raises(baseline_module.DatasetIntegrityError, match="wesad"):
        baseline_module.require_verified_datasets(empty, ["wesad"])


def test_require_verified_datasets_passes_when_all_present(baseline_module, tmp_path, monkeypatch):
    """All-present datasets do not raise."""
    module = baseline_module
    # require_verified_datasets imports verify_all_datasets lazily from
    # scripts.download_datasets, so patch the name it resolves at call time.
    import scripts.download_datasets as dd  # noqa: PLC0415

    orig = dd.verify_all_datasets
    dd.verify_all_datasets = lambda _: {"wesad": True, "mitbih": True, "sleep_edf": True}
    try:
        module.require_verified_datasets(tmp_path / "data", ["wesad", "mitbih", "sleep_edf"])
    finally:
        dd.verify_all_datasets = orig


def test_main_returns_nonzero_on_missing_wesad(baseline_module, tmp_path, monkeypatch):
    """main() returns exit code 2 (not 0) when WESAD is missing."""
    module = baseline_module
    empty = tmp_path / "data"
    empty.mkdir()

    old_argv = sys.argv
    sys.argv = ["validate_baseline.py", "--data-dir", str(empty), "--dataset", "wesad"]
    try:
        rc = module.main()
    finally:
        sys.argv = old_argv

    assert rc == 2


def test_main_returns_zero_when_integrity_ok(baseline_module, monkeypatch):
    """main() returns 0 when integrity passes and a real dataset validates.

    Uses MIT-BIH, which is present in the repo and already passes the Phase 0
    gate. This asserts the integrity-ok path does not hit the exit-code-2
    fail-closed branch.
    """
    module = baseline_module
    import scripts.download_datasets as dd  # noqa: PLC0415

    orig = dd.verify_all_datasets
    dd.verify_all_datasets = lambda _: {"wesad": True, "mitbih": True, "sleep_edf": True}
    try:
        old_argv = sys.argv
        sys.argv = ["validate_baseline.py", "--data-dir", "data", "--dataset", "mitbih"]
        try:
            rc = module.main()
        finally:
            sys.argv = old_argv
        assert rc == 0
    finally:
        dd.verify_all_datasets = orig
