#!/usr/bin/env bash
# Runs the exact command sequence of the CI gate (.woodpecker/ci.yml), locally.
# Used because the containerized `ci-local` cannot run on this host (the
# workspace is a Windows bind mount and the machine runs other pipelines).
set -uo pipefail
cd "$(dirname "$0")/.."
fail=0
step() {
  echo ""
  echo "=== $* ==="
  if "$@"; then
    echo "--- OK: $*"
  else
    echo "--- FAIL: $*"
    fail=1
  fi
}

step uv run python -m ruff check .
step uv run python -m ruff format --check .
step uv run python -m mypy --package synapse24 --config-file pyproject.toml
step uv run python -m mypy sdk/python/synapse_device --config-file pyproject.toml
step uv run python tools/check_contract.py --verbose
step uv run python tools/check_interfaces.py
step uv run python tools/check_baseline_claim.py
step uv run python scripts/validate_phase1_entry.py --config config/hardware_bringup.yaml --dry-run --verbose
step uv run python -m pytest tests/test_phase1_dry_run.py -v --no-cov
step uv run python -m pytest sdk/python/tests -v --no-cov
step uv run python -m pytest tests/test_format_conformance.py -v --no-cov
step uv run python -m pytest --cov=src --cov=worker --cov=sdk/python --cov=backend --cov-fail-under=80 --cov-report=term-missing
step uv run python -m pytest tests/test_research_service_host.py tests/test_raw_frame_parity.py tests/test_firmware_host_loadgate.py tests/test_triage_feature_parity.py tests/test_firmware_ppg_imu_host.py tests/test_firmware_ppg_sqi_host.py tests/test_firmware_ble_contract.py -v --no-cov -p no:cacheprovider

echo ""
if [ "$fail" -eq 0 ]; then
  echo "GATE SEQUENCE: ALL GREEN"
else
  echo "GATE SEQUENCE: FAILURES PRESENT"
fi
exit "$fail"
