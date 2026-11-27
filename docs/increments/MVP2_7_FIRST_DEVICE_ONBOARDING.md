# MVP-2.7 — First device onboarding

**Status:** implementation complete; hardware verification (G1) still open.

This increment does not add a modality. It removes the ways in which the
existing bench suite could report a green result over a red gate, and gives
the operator one command to configure and verify a band. No board, sensor,
radio or person was used in its implementation.

## What changed

- **Evidence is derived, not assumed.** `scripts/bench_suite.py` writes
  `claim`, `status` and `details` on every row. A subprocess exit code is not a
  verdict: T05/T11/T12 require a *fresh*, live, green gate report; T03 parses
  the boot log for 0x57/0x68/0x40 instead of trusting a `--dump` exit code.
  `pass` is kept as an alias for older tooling.
- **Two manifest kinds.** `kind: human` still needs an `S###` pseudonym and
  consent; `kind: bench` declares an operator, serial and condition instead,
  writes to `data/bench/`, and drops the human-reference gates (HR/RMSSD) as
  N/A rather than counting them as free passes. Bench values that a person
  measures by hand are supplied through `--bench-values` and require
  `--verified-by`, so they survive in the append-only history of the run.
- **One configuration command.** `scripts/device_profile.py configure|verify`
  writes one atomic per-unit profile (ECG gain, PPG LEDs, IMU biases, fitted
  cell capacity, feature rate), reads every field back and refuses to commit
  the host record on any mismatch. Firmware gained a JSON whitelist parser
  that accepts a whole profile at once, a `pending_reboot` readback bit and a
  serial-bound cell capacity in calibration v3. The old Protobuf stub was
  removed.
- **Bench firmware variant.** `band-bench` enables factory mode on top of the
  core smoke-kit profile, so no operator has to run `menuconfig`. CI builds it
  as a fifth config. Build metadata now comes from the generated `sdkconfig`
  instead of the variant name: `band-product` no longer claims a registered
  raw research service.
- **T10 is executable.** `scripts/measure_two_pod_sync.py` cross-correlates
  the ACC streams of two recordings over six windows and writes
  `residual_ms`, the observed correlation and the method. Uncorrelated or
  short captures return no residual instead of a zero.
- **Product decisions are recorded, not implemented.**
  `docs/product/DECISIONS.md` is the open risk register (OTA, pairing,
  component lifecycle, power hardware, traceability).

## Verification

Commands run on this host (2026-11-27) against the final tree:

- `ruff check .` and `ruff format --check .`: clean (the `mypy` executable is
  intermittently blocked by Windows Application Control; the same three checks
  via `python -m mypy` are clean — 64 + 9 + 5 source files).
- `check_contract` (167 checks), `generate_contracts --check`,
  `check_interfaces`, `check_baseline_claim`, phase1 dry-run and format
  conformance: green.
- `bash scripts/run_gate_locally.sh` (the same command sequence as
  `.woodpecker/ci.yml`): **GATE SEQUENCE: ALL GREEN** on the final tree,
  including the full suite with coverage over `src`, `worker`, `backend`
  (**83.72 %**, above the 80 % gate) and the SDK suite (**89.41 %**, 70 passed).
- Linux replay of the C host gate in `python:3.12` with `uv sync --dev --frozen`:
  **177 passed, zero skips**. This is the evidence for the runtime-only C
  assertions that Windows Application Control can only compile here.
- The new factory-mode JSON whitelist helpers were additionally compiled
  standalone with `-Wall -Wextra -Werror` and exercised on a sample profile.
- Firmware builds: **not run on this host** — ESP-IDF is not available here.
  The new `band-bench` target is in `.woodpecker/ci.yml` and must be green on
  the Woodpecker `firmware-band` step before this increment is called merged.

Hardware evidence remains open: no board, sensor, radio, long BLE read or
human session has been exercised. Status stays in `docs/hardware/STATUS.md`.
