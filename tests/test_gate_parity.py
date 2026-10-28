"""Pins the CI gate and the local gate script to one shared command sequence.

``.woodpecker/ci.yml`` is the gate; ``scripts/run_gate_locally.sh`` runs the
same command sequence directly on the host (same order, per AGENTS.md). The
two drift apart the moment someone edits one and not the other — the exact
failure this file prevents: a check green locally but never run on CI, or a
new check on CI that the local script never told you about.

The comparison is command-by-command, in order. Historical shape differences
are normalised (``uv run python -m mod`` vs ``uv run mod``, ``--no-sync``),
and the genuine differences are declared with a reason:

* ``uv sync --dev --frozen``                    — pipeline provisioning only
* step ``firmware-band`` (ESP-IDF image)        — pipeline step, replaced by
  the host C gate (tests/firmware_host) locally
* ``scripts/gcc_probe.py``                      — local extra (zig-cc probe)

A new difference must be registered here with a reason, or implemented on the
other side — never silently.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI_YML = ROOT / ".woodpecker" / "ci.yml"
GATE_SCRIPT = ROOT / "scripts" / "run_gate_locally.sh"

_EXEMPT_PIPELINE = {
    # Provisioning: the local `uv run` self-syncs against the committed lock.
    "uv sync --dev --frozen": "environment provisioning (local uv run self-syncs)",
}
# Local-only steps, with grounds. `gcc_probe` is the zig-cc/clang-vs-gcc
# probe: needless on the server, decisive on this host.
_EXEMPT_LOCAL: dict[str, str] = {
    "scripts/gcc_probe.py": "local toolchain probe (zig cc vs server gcc)",
}
# Steps the local host cannot run, with the local pattern that covers the
# same risk. A new step name must land here or be implemented locally.
_EXEMPT_STEPS: dict[str, str] = {
    "firmware-band": "ESP-IDF image build; the host C gate (tests/firmware_host) covers the C contract locally",
}


def _normalise(tokens: list[str]) -> str:
    """One shape for both sides: ``python -m mod`` is ``mod``, ``--no-sync`` drops."""
    cmd = list(tokens)
    if len(cmd) >= 4 and cmd[:2] == ["uv", "run"] and cmd[2] == "python" and cmd[3] == "-m":
        cmd = cmd[:2] + cmd[4:]
    cmd = [token for token in cmd if token != "--no-sync"]
    return " ".join(cmd)


def _pipeline_commands() -> list[str]:
    """Normalised commands of the pipeline's gated (non-exempt) steps, in order."""
    data = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    steps = data.get("steps", [])
    unknown = [s.get("name") for s in steps if s.get("name") not in {"python", *_EXEMPT_STEPS}]
    assert not unknown, (
        f"undeclared pipeline steps: {unknown}; add them to _EXEMPT_STEPS with a reason "
        "or run their commands on the host script too"
    )
    commands: list[str] = []
    for step in steps:
        if step.get("name") != "python":
            continue
        for raw in step.get("commands", []):
            commands.append(_normalise(shlex.split(raw)))
    return commands


def _local_commands() -> list[str]:
    """Normalised commands of the host script's ``step`` calls, in order."""
    commands = []
    for line in GATE_SCRIPT.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("step "):
            continue
        commands.append(_normalise(shlex.split(stripped)[1:]))
    return commands


def _exempt_match(command: str, table: dict[str, str]) -> str | None:
    return next((key for key in table if key in command), None)


def test_pipeline_order_is_kept_locally() -> None:
    """Every local command runs in the same relative order as the pipeline."""
    pipeline = _pipeline_commands()
    local = _local_commands()
    cursor = 0
    for command in local:
        if _exempt_match(command, _EXEMPT_LOCAL):
            continue
        while cursor < len(pipeline) and pipeline[cursor] != command:
            cursor += 1
        assert cursor < len(pipeline), (
            f"local step '{command}' has no pipeline counterpart (in order). "
            "The local gate and the CI gate must run the same sequence: "
            "add it to .woodpecker/ci.yml or register the difference with a reason."
        )
        cursor += 1


def test_pipeline_steps_are_run_locally() -> None:
    """No pipeline command quietly exists without its local twin."""
    pipeline = _pipeline_commands()
    local = _local_commands()
    cursor = 0
    for command in pipeline:
        if _exempt_match(command, _EXEMPT_PIPELINE):
            continue
        while cursor < len(local) and local[cursor] != command:
            cursor += 1
        assert cursor < len(local), (
            f"pipeline command '{command}' is never run by scripts/run_gate_locally.sh. "
            "Add it to the script (same position) or declare the difference."
        )
        cursor += 1


def test_exemptions_still_reference_being_run() -> None:
    """A stale exemption hides a real drift; each must still match a real command."""
    pipeline = _pipeline_commands()
    for key in _EXEMPT_PIPELINE:
        assert any(key in command for command in pipeline), (
            f"stale exemption: '{key}' no longer appears in .woodpecker/ci.yml — remove it"
        )
    local = _local_commands()
    for key in _EXEMPT_LOCAL:
        assert any(key in command for command in local), (
            f"stale exemption: '{key}' no longer appears in scripts/run_gate_locally.sh — remove it"
        )
    names = [
        step.get("name")
        for step in yaml.safe_load(CI_YML.read_text(encoding="utf-8")).get("steps", [])
    ]
    for key in _EXEMPT_STEPS:
        assert key in names, (
            f"stale exemption: step '{key}' no longer exists in .woodpecker/ci.yml — remove it"
        )
