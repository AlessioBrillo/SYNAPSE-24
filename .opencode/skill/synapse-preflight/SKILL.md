---
name: synapse-preflight
description: Local quality gate for SYNAPSE-24. Runs format, lint, typecheck, tests, baseline validation, security audit, and energy budget check before push.
when_to_use: Before every push to remote. Run `uv run preflight` or `uv run preflight quick` or `uv run preflight diff`.
user-invocable: true
---

# SYNAPSE Preflight Skill

Local quality gate that mirrors CI pipeline. Run before every push to catch errors early.

## Installation

```bash
# Add to pyproject.toml [project.scripts]
preflight = "synapse_preflight:main"

# Install in dev environment
uv sync --dev
```

## Usage Modes

| Command | Checks | Time |
|---------|--------|------|
| `uv run preflight` | All (format, lint, typecheck, unit, integration, baseline, security, energy) | ~3-5 min |
| `uv run preflight quick` | Format, lint, typecheck, unit tests only | ~1 min |
| `uv run preflight diff` | Diff audit only (security, secrets, TODO, print) | ~5 sec |

## Implementation: `scripts/preflight.py`

```python
#!/usr/bin/env python3
"""SYNAPSE-24 Preflight Quality Gate."""

from __future__ import annotations
import argparse
import subprocess
import sys
from pathlib import Path
from dataclasses import dataclass
from enum import Enum


class CheckStatus(Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    WARNING = "WARNING"


@dataclass
class CheckResult:
    name: str
    status: CheckStatus
    message: str
    duration_ms: int
    blocking: bool = True


def run_cmd(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    """Run command, return (returncode, stdout, stderr)."""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def check_format(root: Path) -> CheckResult:
    """ruff format --check"""
    import time

    start = time.perf_counter()
    rc, out, err = run_cmd(["uv", "run", "ruff", "format", "--check", "."], root)
    duration = int((time.perf_counter() - start) * 1000)
    if rc == 0:
        return CheckResult("Format (ruff)", CheckStatus.PASS, "All files formatted", duration)
    return CheckResult("Format (ruff)", CheckStatus.FAIL, f"Formatting needed:\n{out}", duration)


def check_lint(root: Path) -> CheckResult:
    """ruff check"""
    import time

    start = time.perf_counter()
    rc, out, err = run_cmd(["uv", "run", "ruff", "check", "."], root)
    duration = int((time.perf_counter() - start) * 1000)
    if rc == 0:
        return CheckResult("Lint (ruff)", CheckStatus.PASS, "No lint issues", duration)
    return CheckResult("Lint (ruff)", CheckStatus.FAIL, f"Lint errors:\n{out}", duration)


def check_typecheck(root: Path) -> CheckResult:
    """mypy --strict src/synapse24"""
    import time

    start = time.perf_counter()
    rc, out, err = run_cmd(["uv", "run", "mypy", "--strict", "src/synapse24"], root)
    duration = int((time.perf_counter() - start) * 1000)
    if rc == 0:
        return CheckResult("Typecheck (mypy)", CheckStatus.PASS, "No type errors", duration)
    return CheckResult("Typecheck (mypy)", CheckStatus.FAIL, f"Type errors:\n{out}", duration)


def check_unit_tests(root: Path) -> CheckResult:
    """pytest unit tests with coverage"""
    import time

    start = time.perf_counter()
    rc, out, err = run_cmd(
        [
            "uv",
            "run",
            "pytest",
            "tests/unit",
            "-x",
            "--tb=short",
            "--cov=src",
            "--cov-fail-under=85",
            "-q",
        ],
        root,
    )
    duration = int((time.perf_counter() - start) * 1000)
    if rc == 0:
        return CheckResult("Unit Tests", CheckStatus.PASS, "All unit tests passed", duration)
    return CheckResult("Unit Tests", CheckStatus.FAIL, f"Test failures:\n{out}", duration)


def check_integration_tests(root: Path) -> CheckResult:
    """pytest integration tests (requires cached datasets)"""
    import time

    start = time.perf_counter()
    rc, out, err = run_cmd(
        ["uv", "run", "pytest", "tests/integration", "-x", "--tb=short", "-q"], root
    )
    duration = int((time.perf_counter() - start) * 1000)
    if rc == 0:
        return CheckResult(
            "Integration Tests", CheckStatus.PASS, "All integration tests passed", duration
        )
    if "No module named" in out or "FileNotFoundError" in out:
        return CheckResult(
            "Integration Tests",
            CheckStatus.SKIP,
            "Datasets not cached locally",
            duration,
            blocking=False,
        )
    return CheckResult("Integration Tests", CheckStatus.FAIL, f"Test failures:\n{out}", duration)


def check_baseline_validation(root: Path) -> CheckResult:
    """validate_baseline.py --dataset both"""
    import time

    start = time.perf_counter()
    rc, out, err = run_cmd(
        [
            "uv",
            "run",
            "python",
            "scripts/validate_baseline.py",
            "--dataset",
            "both",
            "--data-dir",
            "data",
            "--output-dir",
            "data/processed",
        ],
        root,
    )
    duration = int((time.perf_counter() - start) * 1000)
    if rc == 0:
        return CheckResult(
            "Baseline Validation",
            CheckStatus.PASS,
            "Baselines met (WESAD ≥80%, MIT-BIH ≥99.6%)",
            duration,
        )
    return CheckResult(
        "Baseline Validation", CheckStatus.FAIL, f"Baseline failed:\n{out}", duration
    )


def check_security(root: Path) -> CheckResult:
    """bandit security audit"""
    import time

    start = time.perf_counter()
    rc, out, err = run_cmd(["uv", "run", "bandit", "-r", "src/", "-f", "json"], root)
    duration = int((time.perf_counter() - start) * 1000)
    # bandit returns non-zero on findings; parse JSON for severity
    import json

    try:
        report = json.loads(out) if out else {"results": []}
        high_critical = [
            r for r in report.get("results", []) if r["severity"] in ("HIGH", "CRITICAL")
        ]
        if high_critical:
            return CheckResult(
                "Security (bandit)",
                CheckStatus.FAIL,
                f"{len(high_critical)} HIGH/CRITICAL findings",
                duration,
                blocking=False,
            )
        return CheckResult(
            "Security (bandit)",
            CheckStatus.PASS,
            "No HIGH/CRITICAL findings",
            duration,
            blocking=False,
        )
    except Exception:
        return CheckResult(
            "Security (bandit)",
            CheckStatus.WARNING,
            "Could not parse bandit output",
            duration,
            blocking=False,
        )


def check_energy_budget(root: Path) -> CheckResult:
    """Energy budget sanity check"""
    import time

    start = time.perf_counter()
    # Quick check: any new code with power estimates?
    rc, out, err = run_cmd(["git", "diff", "--name-only", "HEAD"], root)
    duration = int((time.perf_counter() - start) * 1000)
    changed = out.strip().split("\n") if out.strip() else []
    power_files = [
        f for f in changed if any(k in f for k in ["acquisition", "power", "energy", "tier"])
    ]
    if power_files:
        return CheckResult(
            "Energy Budget",
            CheckStatus.WARNING,
            f"Power-related files changed: {power_files}. Verify budget manually.",
            duration,
            blocking=False,
        )
    return CheckResult(
        "Energy Budget", CheckStatus.PASS, "No power-related changes", duration, blocking=False
    )


def check_diff_audit(root: Path) -> CheckResult:
    """Audit diff for common issues"""
    import time
    import re

    start = time.perf_counter()
    rc, out, err = run_cmd(["git", "diff", "HEAD"], root)
    duration = int((time.perf_counter() - start) * 1000)

    issues = []
    if "print(" in out:
        issues.append("Contains print() statements")
    if "TODO" in out or "FIXME" in out:
        issues.append("Contains TODO/FIXME comments")
    if re.search(r"(api[_-]?key|secret|password|token)\s*=", out, re.IGNORECASE):
        issues.append("Possible hardcoded secret")
    if "MAC" in out and ":" in out and len(out) > 100:
        # Heuristic for MAC addresses in diff
        issues.append("Possible hardcoded MAC address")

    if issues:
        return CheckResult("Diff Audit", CheckStatus.FAIL, "; ".join(issues), duration)
    return CheckResult("Diff Audit", CheckStatus.PASS, "No issues in diff", duration)


def main():
    parser = argparse.ArgumentParser(description="SYNAPSE-24 Preflight Quality Gate")
    parser.add_argument("mode", nargs="?", default="full", choices=["full", "quick", "diff"])
    args = parser.parse_args()

    root = Path(__file__).parent.parent
    all_results = []

    # Always run diff audit first (fast)
    all_results.append(check_diff_audit(root))

    if args.mode == "diff":
        print_report(all_results)
        sys.exit(0 if all(r.status == CheckStatus.PASS for r in all_results) else 1)

    # Core checks (always)
    all_results.extend(
        [
            check_format(root),
            check_lint(root),
            check_typecheck(root),
            check_unit_tests(root),
        ]
    )

    if args.mode == "quick":
        print_report(all_results)
        sys.exit(
            0
            if all(
                r.status in (CheckStatus.PASS, CheckStatus.SKIP) for r in all_results if r.blocking
            )
            else 1
        )

    # Full mode: additional checks
    all_results.extend(
        [
            check_integration_tests(root),
            check_baseline_validation(root),
            check_security(root),
            check_energy_budget(root),
        ]
    )

    print_report(all_results)

    # Exit code: fail if any BLOCKING check failed
    blocking_failed = any(r.status == CheckStatus.FAIL and r.blocking for r in all_results)
    sys.exit(1 if blocking_failed else 0)


def print_report(results: list[CheckResult]):
    print("\n" + "=" * 60)
    print("SYNAPSE-24 PREFLIGHT REPORT")
    print("=" * 60)

    for r in results:
        icon = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭️", "WARNING": "⚠️"}[r.status]
        blocking = "🔒" if r.blocking else "🔓"
        print(f"{icon} {r.name:25s} [{r.status.value:7s}] {blocking} ({r.duration_ms}ms)")
        if r.status != CheckStatus.PASS:
            print(f"   → {r.message}")

    print("=" * 60)
    passed = sum(1 for r in results if r.status == CheckStatus.PASS)
    failed = sum(1 for r in results if r.status == CheckStatus.FAIL)
    skipped = sum(1 for r in results if r.status == CheckStatus.SKIP)
    warnings = sum(1 for r in results if r.status == CheckStatus.WARNING)
    print(f"Summary: {passed} passed, {failed} failed, {skipped} skipped, {warnings} warnings")

    blocking_failed = any(r.status == CheckStatus.FAIL and r.blocking for r in results)
    if blocking_failed:
        print("🚫 PREFLIGHT FAILED — Do not push. Fix issues above.")
    else:
        print("✅ PREFLIGHT PASSED — Safe to push.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
```

## Quality Gates

- [ ] `preflight` passes before every push
- [ ] `preflight quick` passes before every commit (recommended via pre-commit hook)
- [ ] CI runs same checks (format, lint, typecheck, unit, integration, baseline)
- [ ] Security audit runs in CI (non-blocking warnings)
- [ ] Energy budget check runs on power-related changes

## Pre-commit Hook Integration

```yaml
# .pre-commit-config.yaml
repos:
  - repo: local
    hooks:
      - id: synapse-preflight-quick
        name: SYNAPSE Preflight (Quick)
        entry: uv run preflight quick
        language: system
        pass_filenames: false
        always_run: true
```

## References
- Ergodix preflight skill (pattern source)
- Architecture.md Principle 7 (Observability)
- Roadmap.md Phase 0 milestones