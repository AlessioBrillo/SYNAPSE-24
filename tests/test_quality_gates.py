"""The static gates added in MVP-1.3 PR-E3 stay on (a gate that is quietly switched off is no gate).

The pipeline and the local script are read as *commands*, not as text: a mypy line moved into a
comment, or followed by ``|| true``, must fail here. The lint rule is checked by behaviour: ruff,
with the project's own configuration, must report an unused import.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TYPECHECK = "mypy --explicit-package-bases worker backend --config-file pyproject.toml"


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _pipeline_commands() -> list[str]:
    data = yaml.safe_load((ROOT / ".woodpecker" / "ci.yml").read_text(encoding="utf-8"))
    return [str(c).strip() for step in data["steps"] for c in step.get("commands", [])]


def _local_steps() -> list[str]:
    lines = (ROOT / "scripts" / "run_gate_locally.sh").read_text(encoding="utf-8").splitlines()
    return [ln.strip()[len("step ") :] for ln in lines if ln.strip().startswith("step ")]


def test_unused_imports_are_flagged_by_the_project_configuration(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("import os\n", encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - fixed interpreter, our own files
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--config",
            str(ROOT / "pyproject.toml"),
            "--no-cache",
            str(sample),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    # the project's own rule set decides (a --select on the command line would override its ignore list)
    assert "F401" in result.stdout, "ruff did not report an unused import: F401 is switched off"


def test_only_package_init_files_may_reexport() -> None:
    per_file = _pyproject()["tool"]["ruff"]["lint"]["per-file-ignores"]
    exempt = [glob for glob, rules in per_file.items() if "F401" in rules]
    assert exempt == ["**/__init__.py"], exempt


def test_the_worker_and_the_backend_are_type_checked_by_the_pipeline_and_the_local_gate() -> None:
    # an exact command: a commented-out line or `... || true` is not this command
    assert f"uv run {TYPECHECK}" in _pipeline_commands()
    assert f"uv run python -m {TYPECHECK}" in _local_steps()


def test_the_untyped_third_party_the_worker_imports_is_declared() -> None:
    overrides = _pyproject()["tool"]["mypy"]["overrides"]
    modules = {m for o in overrides if o.get("ignore_missing_imports") for m in o["module"]}
    assert "jsonschema.*" in modules
