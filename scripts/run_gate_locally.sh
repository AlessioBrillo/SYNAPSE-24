#!/usr/bin/env bash
# Runs the Python portion of the CI gate (.woodpecker/ci.yml) step by step,
# locally, plus scripts/gcc_probe.py. It does NOT execute the `firmware-band`
# ESP-IDF step: that step is server-only (it needs the ESP-IDF image and more
# memory than this host has). Used because the containerized `ci-local` cannot
# run on this host (the workspace is a Windows bind mount and the machine runs
# other pipelines).
set -uo pipefail
cd "$(dirname "$0")/.."
# `uv run` must use the committed lock as it is, like the server's `uv sync --frozen`:
# without this it may re-resolve and edit uv.lock in the middle of a gate.
export UV_FROZEN=1
# Same numerical-stack environment as .woodpecker/ci.yml: without these the
# local run can spawn thread pools / loky workers the 2-core CI runner never
# sees, so a green local gate tells us nothing about the server run.
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export JOBLIB_START_METHOD=fork
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
step uv run python -m mypy --explicit-package-bases worker backend --config-file pyproject.toml
step uv run python tools/check_contract.py --verbose
step uv run python tools/generate_contracts.py --check
step uv run python tools/check_interfaces.py
step uv run python tools/check_baseline_claim.py
step uv run python scripts/validate_phase1_entry.py --config config/hardware_bringup.yaml --dry-run --verbose
step uv run python -m pytest tests/test_phase1_dry_run.py -v --no-cov
step uv run python -m pytest tests/test_format_conformance.py -v --no-cov
step uv run python -m pytest --cov=src --cov=worker --cov=backend --cov-fail-under=80 --cov-report=term-missing tests
step uv run python -m pytest tests/test_device_profile.py tests/test_bench_suite_truthfulness.py tests/test_g1_measure.py tests/test_factory_calibrate.py tests/test_device_config_write.py tests/test_bench_suite.py --cov=scripts --cov-fail-under=80 --cov-report=term-missing -v
step uv run python -m pytest sdk/python/tests --cov=sdk/python --cov-fail-under=80 --cov-report=term-missing -v
# Fail-on-skip wrappers (see .woodpecker/ci.yml): a skipped host-C test is a
# red gate, not a green one. Both keep -rs so the skip reasons stay visible.
step uv run python scripts/host_c_gate.py
step uv run python scripts/host_c_gate.py --group parser

# Local-only extra (not in .woodpecker/ci.yml; tests/test_gate_parity.py
# declares it): the local host compiles with zig cc (clang), the server with
# gcc. A source that only clang accepts would pass here and fail there.
step uv run python scripts/gcc_probe.py

echo ""
if [ "$fail" -eq 0 ]; then
  echo "GATE SEQUENCE: ALL GREEN"
else
  echo "GATE SEQUENCE: FAILURES PRESENT"
fi
exit "$fail"
