"""The static gates added in MVP-1.3 PR-E3 stay on (a gate that is quietly switched off is no gate)."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TYPECHECK = "mypy --explicit-package-bases worker backend --config-file pyproject.toml"


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_unused_imports_are_flagged_again() -> None:
    lint = _pyproject()["tool"]["ruff"]["lint"]
    assert "F401" not in lint["ignore"], "F401 hid dead imports (and dead code) for the whole repo"
    # package __init__ files re-export on purpose; nothing else may
    assert lint["per-file-ignores"]["**/__init__.py"] == ["F401"]


def test_the_worker_and_the_backend_are_type_checked_by_the_gate() -> None:
    ci = (ROOT / ".woodpecker" / "ci.yml").read_text(encoding="utf-8")
    local = (ROOT / "scripts" / "run_gate_locally.sh").read_text(encoding="utf-8")
    assert TYPECHECK in ci
    assert TYPECHECK in local


def test_the_untyped_third_party_the_worker_imports_is_declared() -> None:
    overrides = _pyproject()["tool"]["mypy"]["overrides"]
    modules = {m for o in overrides if o.get("ignore_missing_imports") for m in o["module"]}
    assert "jsonschema.*" in modules
