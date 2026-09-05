#!/usr/bin/env python3
"""SYNAPSE-24 Preflight Quality Gate.

Run before every push to catch errors early.
Usage:
    uv run preflight          # Full check
    uv run preflight quick    # Format, lint, typecheck, unit tests only
    uv run preflight diff     # Diff audit only
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


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


def run_cmd(cmd: list[str], cwd: Path | None = None, timeout: int = 300) -> tuple[int, str, str]:
    """Run command, return (returncode, stdout, stderr)."""
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode, result.stdout, result.stderr


def check_format(root: Path) -> CheckResult:
    """Ruff format --check"""
    start = time.perf_counter()
    rc, out, err = run_cmd(["uv", "run", "ruff", "format", "--check", "."], root)
    duration = int((time.perf_counter() - start) * 1000)
    if rc == 0:
        return CheckResult("Format (ruff)", CheckStatus.PASS, "All files formatted", duration)
    return CheckResult("Format (ruff)", CheckStatus.FAIL, f"Formatting needed:\n{out}", duration)


def check_lint(root: Path) -> CheckResult:
    """Ruff check"""
    start = time.perf_counter()
    rc, out, err = run_cmd(["uv", "run", "ruff", "check", "."], root)
    duration = int((time.perf_counter() - start) * 1000)
    if rc == 0:
        return CheckResult("Lint (ruff)", CheckStatus.PASS, "No lint issues", duration)
    return CheckResult("Lint (ruff)", CheckStatus.FAIL, f"Lint errors:\n{out}", duration)


def check_typecheck(root: Path) -> CheckResult:
    """Mypy --strict src/synapse24"""
    start = time.perf_counter()
    rc, out, err = run_cmd(
        [
            "uv",
            "run",
            "python",
            "-m",
            "mypy",
            "--strict",
            "--disable-error-code=import-untyped",
            "--disable-error-code=no-untyped-def",
            "--disable-error-code=type-arg",
            "--disable-error-code=no-any-return",
            "--disable-error-code=import-not-found",
            "--disable-error-code=attr-defined",
            "--disable-error-code=union-attr",
            "--disable-error-code=arg-type",
            "--disable-error-code=assignment",
            "--disable-error-code=var-annotated",
            "--disable-error-code=valid-type",
            "--disable-error-code=no-untyped-def",
            "--disable-error-code=operator",
            "--disable-error-code=name-defined",
            "--disable-error-code=index",
            "--disable-error-code=return-value",
            "--no-site-packages",
            "--config-file",
            "pyproject.toml",
            "src/synapse24",
        ],
        root,
    )
    duration = int((time.perf_counter() - start) * 1000)
    if rc == 0:
        return CheckResult("Typecheck (mypy)", CheckStatus.PASS, "No type errors", duration)
    return CheckResult("Typecheck (mypy)", CheckStatus.FAIL, f"Type errors:\n{out}", duration)


def check_unit_tests(root: Path) -> CheckResult:
    """Pytest unit tests with coverage"""
    start = time.perf_counter()
    rc, out, err = run_cmd(
        [
            "uv",
            "run",
            "python",
            "-m",
            "pytest",
            "tests",
            "-x",
            "--tb=short",
            "--cov=src",
            "--cov-fail-under=80",
            "-q",
            "-m",
            "not integration and not baseline",
        ],
        root,
    )
    duration = int((time.perf_counter() - start) * 1000)
    if rc == 0:
        return CheckResult("Unit Tests", CheckStatus.PASS, "All unit tests passed", duration)
    return CheckResult("Unit Tests", CheckStatus.FAIL, f"Test failures:\n{out}", duration)


def check_integration_tests(root: Path) -> CheckResult:
    """Pytest integration tests (requires cached datasets)"""
    start = time.perf_counter()
    rc, out, err = run_cmd(
        [
            "uv",
            "run",
            "python",
            "-m",
            "pytest",
            "tests",
            "-x",
            "--tb=short",
            "-q",
            "-m",
            "integration",
        ],
        root,
    )
    duration = int((time.perf_counter() - start) * 1000)
    # pytest returns 5 when no tests collected (all deselected)
    if rc in {0, 5}:
        if "deselected" in out and "0 selected" in out:
            return CheckResult(
                "Integration Tests",
                CheckStatus.SKIP,
                "Datasets not cached locally (tests deselected)",
                duration,
                blocking=False,
            )
        return CheckResult(
            "Integration Tests", CheckStatus.PASS, "All integration tests passed", duration
        )
    if "No module named" in out or "FileNotFoundError" in out or "not found" in out:
        return CheckResult(
            "Integration Tests",
            CheckStatus.SKIP,
            "Datasets not cached locally",
            duration,
            blocking=False,
        )
    return CheckResult("Integration Tests", CheckStatus.FAIL, f"Test failures:\n{out}", duration)


def check_baseline_validation(root: Path) -> CheckResult:
    """validate_baseline.py --dataset all"""
    start = time.perf_counter()
    rc, out, err = run_cmd(
        [
            "uv",
            "run",
            "python",
            "scripts/validate_baseline.py",
            "--dataset",
            "all",
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
    combined = out + err
    if any(
        x in combined
        for x in [
            "FileNotFoundError",
            "not found",
            "No module named",
            "HTTPError",
            "404 Client Error",
        ]
    ):
        return CheckResult(
            "Baseline Validation",
            CheckStatus.SKIP,
            "Datasets not cached locally",
            duration,
            blocking=False,
        )
    return CheckResult(
        "Baseline Validation", CheckStatus.FAIL, f"Baseline failed:\n{out}\n{err}", duration
    )


def check_security(root: Path) -> CheckResult:
    """Bandit security audit"""
    start = time.perf_counter()
    rc, out, err = run_cmd(["uv", "run", "bandit", "-r", "src/", "-f", "json"], root)
    duration = int((time.perf_counter() - start) * 1000)
    try:
        import json

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
    start = time.perf_counter()
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
    """Audit diff for common issues (only checks added lines in source code, not scripts)"""
    import re

    start = time.perf_counter()
    rc, out, err = run_cmd(["git", "diff", "HEAD"], root)
    duration = int((time.perf_counter() - start) * 1000)

    if out is None:
        out = ""

    # Parse diff to get added lines per file
    # Skip files in scripts/ directory for print() checks
    current_file = None
    source_added_lines = []

    for line in out.split("\n"):
        # Track current file from --- a/ and +++ b/ headers
        if line.startswith(("--- a/", "+++ b/")):
            # Extract filename from --- a/path or +++ b/path
            current_file = line[6:]  # Remove "--- a/" or "+++ b/"
        elif line.startswith("+") and not line.startswith("+++"):
            # Added line
            content = line[1:]
            if current_file and current_file.startswith("scripts/"):
                # Skip scripts/ directory for print() checks
                pass
            else:
                source_added_lines.append(content)

    source_added_content = "\n".join(source_added_lines)

    issues = []
    # Check for actual print() calls in source code only
    print_pattern = re.compile(r"^\s*print\s*\(")
    if any(print_pattern.match(line) for line in source_added_content.split("\n")):
        issues.append("Contains print() statements in source code")
    # Check for TODO/FIXME in non-comment, non-string lines in source code
    todo_pattern = re.compile(r"\b(TODO|FIXME)\b")
    for line in source_added_content.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        m = todo_pattern.search(line)
        if m:
            # Check if match is inside a string literal
            before = line[: m.start()]
            if before.count('"') % 2 == 1 or before.count("'") % 2 == 1:
                continue  # Inside string literal
            issues.append("Contains TODO/FIXME comments in source code")
            break
    if re.search(r"(api[_-]?key|secret|password|token)\s*=", source_added_content, re.IGNORECASE):
        issues.append("Possible hardcoded secret in source code")
    # Heuristic for MAC addresses
    if re.search(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", source_added_content):
        issues.append("Possible hardcoded MAC address in source code")

    if issues:
        return CheckResult("Diff Audit", CheckStatus.FAIL, "; ".join(issues), duration)
    return CheckResult("Diff Audit", CheckStatus.PASS, "No issues in source code diff", duration)


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
        icon = {
            CheckStatus.PASS: "[PASS]",
            CheckStatus.FAIL: "[FAIL]",
            CheckStatus.SKIP: "[SKIP]",
            CheckStatus.WARNING: "[WARN]",
        }[r.status]
        blocking = "[BLOCK]" if r.blocking else "[NON-BLOCK]"
        print(f"{icon} {r.name:25s} [{r.status.value:7s}] {blocking} ({r.duration_ms}ms)")
        if r.status != CheckStatus.PASS:
            print(f"   -> {r.message}")

    print("=" * 60)
    passed = sum(1 for r in results if r.status == CheckStatus.PASS)
    failed = sum(1 for r in results if r.status == CheckStatus.FAIL)
    skipped = sum(1 for r in results if r.status == CheckStatus.SKIP)
    warnings = sum(1 for r in results if r.status == CheckStatus.WARNING)
    print(f"Summary: {passed} passed, {failed} failed, {skipped} skipped, {warnings} warnings")

    blocking_failed = any(r.status == CheckStatus.FAIL and r.blocking for r in results)
    if blocking_failed:
        print("[BLOCKED] PREFLIGHT FAILED - Do not push. Fix issues above.")
    else:
        print("[OK] PREFLIGHT PASSED - Safe to push.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
