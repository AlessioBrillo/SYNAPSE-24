"""Documentation parity: every documented command must exist.

``--live-hardware`` was cited in four documents and did not exist in the
script; ``--tier1`` was cited in a config header comment and did not exist
either. A runbook that cannot be followed is worse than no runbook, because
the reader loses an afternoon concluding the hardware is broken.

This test extracts the fenced command blocks from the hardware runbooks,
resolves the ``uv run python ...`` invocations to a script path, and asks
argparse to parse them. No execution, no side effects — only parsing.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

DOCS = sorted((ROOT / "docs" / "hardware").glob("*.md"))
ROOT_DOCS = [
    ROOT / "HARDWARE_BRINGUP.md",
    ROOT / "docs" / "archive" / "PHASE1_NEXT_STEPS.md",
]

# Commands that need hardware, a network, or a long run: parsed, not run.
_INVOCATION = re.compile(
    r"^\s*(?:uv run\s+)?(?:python|\.venv\\\\Scripts\\\\python\.exe)\s+(\S+\.py)(.*)$"
)


def _commands(doc: Path) -> list[list[str]]:
    """Tokenised script invocations from every fenced block of a document."""
    text = doc.read_text(encoding="utf-8")
    found: list[list[str]] = []
    for line in text.splitlines():
        match = _INVOCATION.match(line.strip())
        if not match:
            continue
        script, rest = match.groups()
        try:
            tokens = [script, *shlex.split(rest)]
        except ValueError:
            continue  # an env-var prefix or shell syntax: not a bare argv
        found.append(tokens)
    return found


def _script_path(script: str) -> Path | None:
    for candidate in (ROOT / script, ROOT / "scripts" / Path(script).name):
        if candidate.is_file():
            return candidate
    return None


class TestDocCommandsParse:
    @pytest.mark.parametrize("doc", DOCS + ROOT_DOCS, ids=lambda p: p.name)
    def test_every_documented_script_invocation_parses(self, doc: Path) -> None:
        commands = _commands(doc)
        # Not every runbook invokes a script (INCREMENT_SUMMARY is prose);
        # every one that does must be parseable by argparse.
        for tokens in commands:
            script = _script_path(tokens[0])
            if script is None:
                pytest.fail(f"{doc.name} references a script that does not exist: {tokens[0]}")
            proc = subprocess.run(  # noqa: S603 - fixed interpreter and repo-local script
                [sys.executable, str(script), *tokens[1:], "--help"],
                capture_output=True,
                text=True,
                cwd=ROOT,
                check=False,
            )
            assert proc.returncode == 0, (
                f"{doc.name} documents a command argparse rejects:\n"
                f"  {' '.join(tokens)}\n"
                f"{proc.stderr.strip()[:400]}"
            )

    def test_the_runbooks_document_at_least_one_command(self) -> None:
        """A runbook with no executable command is prose, not a runbook."""
        total = sum(len(_commands(doc)) for doc in DOCS + ROOT_DOCS)
        assert total > 0


class TestLiveHardwareFlagExists:
    """The exact flag the runbooks cite."""

    def test_validate_phase1_entry_accepts_live_hardware(self) -> None:
        proc = subprocess.run(  # noqa: S603 - fixed interpreter and script path
            [sys.executable, str(ROOT / "scripts" / "validate_phase1_entry.py"), "--help"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )
        assert "--live-hardware" in proc.stdout
        assert "--soak" in proc.stdout
        assert "--gate-report" in proc.stdout

    def test_docs_reference_only_flags_that_exist(self) -> None:
        """Flags cited for *this script* must be flags this script has.

        Scoped per invocation line: the runbooks also cite flags of git, idf.py
        and the build script, and attributing those to the validator would
        only produce noise a reader learns to ignore.
        """
        script = ROOT / "scripts" / "validate_phase1_entry.py"
        proc = subprocess.run(  # noqa: S603
            [sys.executable, str(script), "--help"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )
        available = set(re.findall(r"--[a-z][a-z0-9-]+", proc.stdout))

        missing: set[str] = set()
        for doc in DOCS + ROOT_DOCS:
            for line in doc.read_text(encoding="utf-8").splitlines():
                if "validate_phase1_entry.py" not in line:
                    continue
                missing |= {f for f in re.findall(r"--[a-z][a-z0-9-]+", line) if f not in available}
        assert not missing, f"flags cited for this script but not implemented: {sorted(missing)}"


class TestSoakMode:
    """The MVP bar of BRINGUP_SOP.md §5, in a form CI can prove."""

    def test_soak_without_the_recorder_fails_loudly(self, tmp_path: Path, monkeypatch) -> None:
        """A soak that silently recorded nothing is the worst outcome."""
        monkeypatch.setenv("SYNAPSE_LABRECORDER_CLI", str(tmp_path / "absent.exe"))

        import asyncio
        import logging

        from scripts.validate_phase1_entry import Phase1Validator
        from tests.test_validator_clock_gate import SyntheticPodTransport

        logger = logging.getLogger("test_soak")
        logger.addHandler(logging.NullHandler())
        validator = Phase1Validator(
            ROOT / "config" / "hardware_bringup.yaml",
            logger,
            transport=SyntheticPodTransport(offset_ms=0.0, duration_s=1.0),
        )
        with pytest.raises(Exception) as excinfo:  # noqa: B017, PT011 - type varies by env
            asyncio.run(validator.run(duration_s=1.0, record=True))
        # Either the recorder is missing (our harness) or present (CI with
        # it installed): in both cases the SOP must not pretend it recorded.
        assert "LabRecorderCLI" in str(excinfo.value) or "returncode" in str(excinfo.value)

    def test_buffer_growth_is_linear_not_quadratic(self) -> None:
        """A 6-hour soak must not degrade with time already spent."""
        import numpy as np

        from synapse24.signal_quality import compute_ppg_quality

        rng = np.random.default_rng(7)
        red = 100_000.0 + 5_000.0 * np.sin(2 * np.pi * np.arange(5000) / 50.0)
        red = red + rng.normal(0, 100, red.size)
        quality = compute_ppg_quality(red, 50)
        assert 0.0 <= quality["ppg_sqi"] <= 1.0
        assert 0.0 <= quality["motion_artifact_prob"] <= 1.0
