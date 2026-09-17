#!/usr/bin/env python3
"""SYNAPSE-24 Phase 0 Exit Gate Validation (Targeted Mode).

Runs only the critical Phase 0 closure gate tests and aggregates results.
This is the gate that unblocks Phase 1 hardware procurement per Roadmap.md §137.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class GateResult:
    """Result of a single validation gate."""

    name: str
    passed: bool
    value: str
    threshold: str
    details: str = ""
    provenance: str = "test"


@dataclass
class Phase0ExitReport:
    """Complete Phase 0 Exit Gate validation report."""

    run_id: str
    timestamp: str
    seed: int
    gates: list[GateResult]
    overall_pass: bool
    test_summary: dict[str, Any]
    baseline_summary: dict[str, Any]
    coverage: dict[str, Any]
    lint_typecheck: dict[str, Any]


# Only the critical closure gate tests - these are the Phase 0 exit criteria
GATE_TESTS = {
    "wesad_3class_surrogate": {
        "threshold": "GroupKFold 3-class accuracy >=80% (surrogate)",
        "tests": [
            "tests/test_wesad_fusion_closure.py::TestSurrogateClosureGate::test_groupkfold_3class_ge_80_on_surrogate",
            "tests/test_wesad_fusion_closure.py::TestSurrogateClosureGate::test_surrogate_flag_survives_validation",
            "tests/test_wesad_fusion_closure.py::TestSurrogateXdfProof::test_surrogate_xdf_zero_drop",
        ],
        "provenance": "test",
    },
    "wesad_int8_closure": {
        "threshold": "FP32->INT8 accuracy drop <=2% on WESAD triage",
        "tests": [
            "tests/test_wesad_int8_closure.py::TestSurrogateInt8ClosureGate::test_groupkfold_fp32_ge_80_with_measured_int8_drop",
            "tests/test_wesad_int8_closure.py::TestSurrogateInt8ClosureGate::test_hub_fusion_profile_passes_same_closure",
        ],
        "provenance": "test",
    },
    "mitbih_closure": {
        "threshold": "Se>=99.6%, PPV>=99.6%, RMSSD MAE <5ms (records 100, 119)",
        "tests": [
            "tests/test_mitbih_closure_gate.py::TestMitbihClosureGate::test_closure_passes_despite_207",
            "tests/test_mitbih_closure_gate.py::TestMitbihClosureGate::test_closure_constants_match_ingestion",
        ],
        "provenance": "test",
    },
    "sleep_edf_closure": {
        "threshold": "Median Cohen's kappa >=0.65 (Fpz-Cz EEG-only, 5 subjects)",
        "tests": [
            "tests/test_sleep_edf_real_data_closure.py::TestSleepEdfRealDataClosure::test_closure_median_kappa_gate",
            "tests/test_sleep_edf_real_data_closure.py::TestSleepEdfRealDataClosure::test_sc4001_fprz_cz_clears_per_subject_floor",
            "tests/test_sleep_edf_real_data_closure.py::TestSleepEdfRealDataClosure::test_sc4101_fprz_cz_clears_per_subject_floor",
        ],
        "provenance": "test",
    },
    "phase0_contracts": {
        "threshold": "All schema/baseline/XDF zero-drop contracts valid",
        "tests": [
            "tests/test_phase0_exit_gate.py::TestPhase0ExitGate",
            "tests/test_phase0_exit_gate.py::TestPerTierSyncBudget",
            "tests/test_phase0_exit_gate.py::TestQuantifyResidualDriftWithTier",
            "tests/test_phase0_exit_gate.py::TestBaselineReportSchema",
            "tests/test_phase0_exit_gate.py::TestBaselineReportSchemaValidator",
            "tests/test_phase0_exit_gate.py::TestXdfZeroDropRoundtrip",
            "tests/test_phase0_exit_gate.py::TestPhase0EdgeGate",
        ],
        "provenance": "test",
    },
    "phase0_real_data": {
        "threshold": "Real data ingestion produces valid XDF, corruption quarantine",
        "tests": [
            "tests/test_phase0_real_data_closure.py::TestMitbihAllQrsReference",
            "tests/test_phase0_real_data_closure.py::TestMitbihXdfValidity",
            "tests/test_phase0_real_data_closure.py::TestMitbihCorruptionQuarantine",
            "tests/test_phase0_real_data_closure.py::TestXdfWriterFormat",
            "tests/test_phase0_real_data_closure.py::TestWesadPoisonGuard",
        ],
        "provenance": "test",
    },
    "tiered_acquisition": {
        "threshold": "T0/T1/T2 transitions, power budget, motion gate, tier durations",
        "tests": [
            "tests/test_tier_promotion_cycle.py::TestTierPromotionCycle",
            "tests/test_tier_durations.py::TestTierDurationReset",
            "tests/test_power_budget_physics.py::TestPowerBudgetPhysics",
            "tests/test_motion_gating_blocks_tier1.py::TestCleanMotionPromotes",
            "tests/test_motion_gating_blocks_tier1.py::TestHighMotionBlocks",
            "tests/test_motion_gating_blocks_tier1.py::TestExtractionTimeGate",
            "tests/test_motion_gating_blocks_tier1.py::TestLiveValidatorOverall",
        ],
        "provenance": "test",
    },
    "sync_xdf": {
        "threshold": "Multi-pod clock sync <=10ms T0 / <=1ms T1, XDF zero-drop",
        "tests": [
            "tests/test_clock_sync.py::TestSyncConfig",
            "tests/test_clock_sync.py::TestSyncMarker",
            "tests/test_clock_sync.py::TestSyncMarkerManager",
            "tests/test_clock_sync.py::TestClockDriftEstimator",
            "tests/test_clock_sync.py::TestTimestampCorrector",
            "tests/test_clock_sync.py::TestMultiPodClockSync",
            "tests/test_clock_sync.py::TestQuantifyResidualDrift",
            "tests/test_clock_sync.py::TestIntegrationScenarios",
            "tests/test_lsl_live_sync.py::TestLiveTwoPodResolveAndStream",
            "tests/test_lsl_live_sync.py::TestLiveResidualWithinTier0",
            "tests/test_lsl_live_sync.py::TestLiveToXdfZeroDrop",
            "tests/test_xdf_correction.py::TestXDFCorrection",
            "tests/test_xdf_correction.py::TestCorrectionIntegration",
        ],
        "provenance": "test",
    },
    "signal_quality": {
        "threshold": "ECG/PPG/EEG quality metrics within literature thresholds",
        "tests": [
            "tests/test_signal_quality.py::TestECGQuality",
            "tests/test_signal_quality.py::TestPPGQuality",
            "tests/test_signal_quality.py::TestEEGQuality",
            "tests/test_signal_quality.py::TestYASASleepStaging",
            "tests/test_signal_quality.py::TestSignalQualityMetrics",
            "tests/test_signal_quality.py::TestIntegration",
            "tests/test_signal_quality.py::TestXDFUtils",
        ],
        "provenance": "test",
    },
}


def run_gate_tests(gate_name: str, test_list: list[str]) -> tuple[bool, int, int, int]:
    """Run a specific gate's tests and return (passed, passed_count, failed_count, skipped_count)."""
    if not test_list:
        return False, 0, 0, 0

    cmd = ["uv", "run", "pytest", *test_list, "-v", "--tb=short", "-q", "--disable-warnings"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        output = result.stdout + result.stderr

        passed_count = 0
        failed_count = 0
        skipped_count = 0

        # Parse output for summary - look at the last few lines for the summary
        for raw_line in output.splitlines():
            line = raw_line.strip()
            # Expected format: "X passed, Y failed, Z skipped in Ws" or "X passed in Ws"
            if "passed" in line and (
                "failed" in line or "skipped" in line or "error" in line or "in" in line
            ):
                # Match patterns like "1 passed", "2 failed", "3 skipped"
                passed_match = re.search(r"(\d+)\s+passed", line)
                failed_match = re.search(r"(\d+)\s+failed", line)
                skipped_match = re.search(r"(\d+)\s+skipped", line)
                error_match = re.search(r"(\d+)\s+error", line)

                if passed_match:
                    passed_count = int(passed_match.group(1))
                if failed_match:
                    failed_count = int(failed_match.group(1))
                if skipped_match:
                    skipped_count = int(skipped_match.group(1))
                if error_match:
                    failed_count += int(error_match.group(1))
                break

        # Also check return code
        overall_passed = result.returncode == 0 and failed_count == 0

        return overall_passed, passed_count, failed_count, skipped_count
    except subprocess.TimeoutExpired:
        return False, 0, 1, 0
    except Exception as e:
        return False, 0, 1, 0


def run_lint_typecheck() -> tuple[bool, dict]:
    """Run ruff lint and mypy typecheck."""
    results = {}

    try:
        result = subprocess.run(
            ["uv", "run", "ruff", "check", "."], capture_output=True, text=True, timeout=60
        )
        results["ruff_lint"] = {"passed": result.returncode == 0}
    except Exception as e:
        results["ruff_lint"] = {"passed": False, "error": str(e)}

    try:
        result = subprocess.run(
            ["uv", "run", "ruff", "format", "--check", "."],
            capture_output=True,
            text=True,
            timeout=60,
        )
        results["ruff_format"] = {"passed": result.returncode == 0}
    except Exception as e:
        results["ruff_format"] = {"passed": False, "error": str(e)}

    try:
        result = subprocess.run(
            ["uv", "run", "mypy", "--package", "synapse24", "--config-file", "pyproject.toml"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        results["mypy"] = {"passed": result.returncode == 0}
    except Exception as e:
        results["mypy"] = {"passed": False, "error": str(e)}

    all_passed = all(r.get("passed", False) for r in results.values())
    return all_passed, results


def run_coverage() -> tuple[bool, dict]:
    """Run pytest with coverage (fast mode - only unit tests)."""
    try:
        result = subprocess.run(
            [
                "uv",
                "run",
                "pytest",
                "--cov=src",
                "--cov-fail-under=80",
                "--cov-report=json",
                "--cov-report=term-missing",
                "-q",
                "--disable-warnings",
                "--tb=no",
                "-m",
                "not slow and not integration",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )

        coverage_file = Path("coverage.json")
        coverage_data = {}
        if coverage_file.exists():
            with open(coverage_file) as f:
                coverage_data = json.load(f)

        totals = coverage_data.get("totals", {})
        line_cov = totals.get("percent_covered", 0)
        branch_cov = totals.get("percent_covered_branches", 0)

        return result.returncode == 0, {
            "line_coverage": line_cov,
            "branch_coverage": branch_cov,
            "target_met": line_cov >= 80 and branch_cov >= 80,
            "files": len(coverage_data.get("files", {})),
        }
    except Exception as e:
        return False, {
            "line_coverage": 0,
            "branch_coverage": 0,
            "target_met": False,
            "error": str(e),
        }


def load_baseline_report() -> dict:
    """Load existing baseline report."""
    report_path = Path("data/processed/baseline_report.json")
    if report_path.exists():
        with open(report_path) as f:
            return json.load(f)
    return {}


def _print_header() -> None:
    """Print validation header."""
    print("=" * 70)
    print("SYNAPSE-24 PHASE 0 EXIT GATE VALIDATION")
    print("=" * 70)
    print(f"Architecture.md: {Path('Architecture.md').exists()}")
    print(f"Roadmap.md: {Path('Roadmap.md').exists()}")
    print("Seed: 42 (deterministic)")
    now = datetime.now(UTC)
    print(f"Timestamp: {now.isoformat()}")
    print("=" * 70)


def _run_gate_tests() -> tuple[list[GateResult], dict[str, Any], bool]:
    """Run all gate tests and return (gates, summaries, overall_pass)."""
    all_gates = []
    gate_summaries = {}
    overall_pass = True

    # Run each gate's tests
    for gate_name, gate_def in GATE_TESTS.items():
        print(f"\n[RUNNING] {gate_name}...")
        threshold = gate_def["threshold"]
        test_list = gate_def["tests"]
        provenance = gate_def["provenance"]

        passed, passed_count, failed_count, skipped_count = run_gate_tests(gate_name, test_list)
        total_count = passed_count + failed_count + skipped_count

        gate_summaries[gate_name] = {
            "passed": passed,
            "total": total_count,
            "passed_count": passed_count,
            "failed_count": failed_count,
            "skipped_count": skipped_count,
        }

        gate_passed = passed and total_count > 0
        overall_pass = overall_pass and gate_passed

        gate_result = GateResult(
            name=gate_name,
            passed=gate_passed,
            value="PASS" if gate_passed else "FAIL",
            threshold=threshold,
            details=f"{passed_count}/{total_count} passed, {failed_count} failed, {skipped_count} skipped",
            provenance=provenance,
        )
        all_gates.append(gate_result)

        status = "PASS" if gate_passed else "FAIL"
        print(f"  [{status}] {gate_name}: {gate_result.details}")

    return all_gates, gate_summaries, overall_pass


def _run_lint_typecheck(
    all_gates: list[GateResult], overall_pass: bool
) -> tuple[list[GateResult], bool]:
    """Run lint and typecheck, append results to gates."""
    print("\n[RUNNING] Lint & Type Check...")
    lint_passed, lint_results = run_lint_typecheck()
    all_gates.append(
        GateResult(
            name="lint_typecheck",
            passed=lint_passed,
            value="PASS" if lint_passed else "FAIL",
            threshold="Zero lint errors, zero type errors",
            details=f"ruff_lint: {lint_results.get('ruff_lint', {}).get('passed')}, "
            f"ruff_format: {lint_results.get('ruff_format', {}).get('passed')}, "
            f"mypy: {lint_results.get('mypy', {}).get('passed')}",
            provenance="measurement",
        )
    )
    overall_pass = overall_pass and lint_passed
    print(f"  [{'PASS' if lint_passed else 'FAIL'}] lint_typecheck")
    return all_gates, overall_pass


def _run_coverage(
    all_gates: list[GateResult], overall_pass: bool
) -> tuple[list[GateResult], bool, dict]:
    """Run coverage check, append results to gates."""
    print("\n[RUNNING] Coverage...")
    cov_passed, cov_results = run_coverage()
    all_gates.append(
        GateResult(
            name="coverage",
            passed=cov_passed,
            value="PASS" if cov_passed else "FAIL",
            threshold="Line coverage >=80%, Branch coverage >=80%",
            details=f"Line: {cov_results.get('line_coverage', 0):.1f}%, Branch: {cov_results.get('branch_coverage', 0):.1f}%",
            provenance="measurement",
        )
    )
    overall_pass = overall_pass and cov_passed
    print(
        f"  [{'PASS' if cov_passed else 'FAIL'}] coverage: Line {cov_results.get('line_coverage', 0):.1f}%, Branch {cov_results.get('branch_coverage', 0):.1f}%"
    )
    return all_gates, overall_pass, cov_results


def _load_baseline_gates(
    all_gates: list[GateResult], overall_pass: bool
) -> tuple[list[GateResult], bool]:
    """Load baseline report and append gates."""
    print("\n[LOADING] Baseline Report...")
    baseline_report = load_baseline_report()
    if baseline_report:
        datasets = baseline_report.get("datasets", {})
        xdf_val = baseline_report.get("xdf_validation", {})

        # WESAD
        wesad = datasets.get("wesad", {})
        all_gates.append(
            GateResult(
                name="baseline_wesad_3class",
                passed=wesad.get("target_met", False),
                value=f"{wesad.get('accuracy', 0):.3f}" if wesad.get("accuracy") else "N/A",
                threshold=">=0.80 (GroupKFold, 15 subjects)",
                details=f"Feature source: {wesad.get('feature_source', 'unknown')}, Surrogate: {wesad.get('surrogate', False)}",
                provenance="baseline",
            )
        )

        # MIT-BIH
        mitbih = datasets.get("mitbih", {})
        all_gates.append(
            GateResult(
                name="baseline_mitbih_rpeak",
                passed=mitbih.get("closure_target_met", False),
                value=f"Se={mitbih.get('mean_sensitivity', 0):.4f}, PPV={mitbih.get('mean_ppv', 0):.4f}, MAE={mitbih.get('mean_rmssd_mae_ms', 0):.2f}ms",
                threshold="Se>=0.996, PPV>=0.996, RMSSD MAE<5ms (closure records 100, 119)",
                details=f"Closure missing: {mitbih.get('closure_missing', [])}, Full cohort: {mitbih.get('n_records', 0)} records",
                provenance="baseline",
            )
        )

        # Sleep-EDF
        sleep_edf = datasets.get("sleep_edf", {})
        all_gates.append(
            GateResult(
                name="baseline_sleep_edf_kappa",
                passed=sleep_edf.get("target_met", False),
                value=f"median kappa={sleep_edf.get('median_cohen_kappa', 0):.3f}, mean={sleep_edf.get('mean_cohen_kappa', 0):.3f}",
                threshold="Median kappa>=0.65 (Fpz-Cz EEG-only, 5 SC subjects)",
                details=f"Subjects: {sleep_edf.get('n_subjects', 0)}, Target met: {sleep_edf.get('target_met', False)}",
                provenance="baseline",
            )
        )

        # XDF
        all_gates.append(
            GateResult(
                name="baseline_xdf_zero_drop",
                passed=xdf_val.get("files_failed", 1) == 0,
                value=f"{xdf_val.get('files_validated', 0)} validated, {xdf_val.get('files_failed', 0)} failed",
                threshold="Zero XDF files failed",
                details="All generated XDF files pass round-trip validation",
                provenance="baseline",
            )
        )

        for gate in all_gates[-4:]:
            overall_pass = overall_pass and gate.passed
            status = "PASS" if gate.passed else "FAIL"
            print(f"  [{status}] {gate.name}: {gate.value} (threshold: {gate.threshold})")

    return all_gates, overall_pass


def _build_report(
    all_gates: list[GateResult],
    gate_summaries: dict[str, Any],
    overall_pass: bool,
    baseline_report: dict,
    cov_results: dict,
    lint_results: dict,
) -> tuple[Phase0ExitReport, Path]:
    """Build and save the final report."""
    run_id = f"phase0_exit_{int(time.time())}"
    now = datetime.now(UTC)
    report = Phase0ExitReport(
        run_id=run_id,
        timestamp=now.isoformat(),
        seed=42,
        gates=all_gates,
        overall_pass=overall_pass,
        test_summary=gate_summaries,
        baseline_summary=baseline_report,
        coverage=cov_results,
        lint_typecheck=lint_results,
    )

    # Save report
    output_dir = Path("data/processed/phase0_exit")
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"phase0_exit_report_{run_id}.json"
    with open(report_path, "w") as f:
        json.dump(asdict(report), f, indent=2, default=str)

    latest_path = output_dir / "phase0_exit_report_latest.json"
    with open(latest_path, "w") as f:
        json.dump(asdict(report), f, indent=2, default=str)

    return report, report_path


def _print_final_report(report: Phase0ExitReport, report_path: Path) -> None:
    """Print final summary report."""
    print("\n" + "=" * 70)
    print("PHASE 0 EXIT GATE FINAL REPORT")
    print("=" * 70)
    print(f"Run ID: {report.run_id}")
    print(f"Timestamp: {report.timestamp}")
    print(f"Seed: {report.seed}")
    print(
        f"Overall: {'PASS - Phase 0 COMPLETE, Phase 1 UNBLOCKED' if report.overall_pass else 'FAIL - Phase 0 INCOMPLETE'}"
    )
    print("-" * 70)

    for gate in report.gates:
        status = "PASS" if gate.passed else "FAIL"
        print(f"  [{status}] {gate.name:30s} | {gate.value:10s} | {gate.threshold}")

    print("=" * 70)
    print(f"Report saved to: {report_path}")
    print("=" * 70)


def main() -> int:
    _print_header()

    all_gates, gate_summaries, overall_pass = _run_gate_tests()
    all_gates, overall_pass = _run_lint_typecheck(all_gates, overall_pass)
    all_gates, overall_pass, cov_results = _run_coverage(all_gates, overall_pass)
    all_gates, overall_pass = _load_baseline_gates(all_gates, overall_pass)

    baseline_report = load_baseline_report()
    lint_passed, lint_results = run_lint_typecheck()
    _, cov_results = run_coverage()

    report, report_path = _build_report(
        all_gates, gate_summaries, overall_pass, baseline_report, cov_results, lint_results
    )
    _print_final_report(report, report_path)

    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
