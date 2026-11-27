# MVP-2.8 — Hardware readiness fixes

**Status:** implementation complete on this tree; hardware verification (G1) still open;
server `firmware-band` step still to be confirmed on the pushed branch.

This increment does not add a modality. It closes the review findings of MVP-2.7
that would have made the first real device produce mis-scaled ECG, unverifiable
evidence, or a "valid" calibration with no measurement provenance. No board,
sensor, radio or person was used in its implementation.

## What changed

**ECG units and gain truth.**
`scripts/validate_phase1_entry.py` now converts the live ECG row from mV (the
research client's analysis unit) to µV (the contract display unit) at the LSL
push boundary, so its XDFs agree with `synapse record` and `g1_measure.py`.
`g1_measure.py` also detects a flat-topped (clipped) sine. Without this, a 1 mVpp
input fitted a gain of ~1.1 and would have written `sdkconfig_ecg_gain ≈ 110`.

**Bench evidence derives its verdict from the observation.**
`bench_suite.py` forwards `--bench-values` and requires `--verified-by`; T01
marks the five hand-checked kit rows `unverified` and fails without a verifier;
T02 reads DIS `2A25` and compares it with the calibration serial; T03 requires
a fresh log with `… answers` lines; T05/T11/T12 require the report's
`duration_s` to match the request and a real `LabRecorderCLI` source on a soak;
a missing `--manifest` writes FAIL evidence instead of crashing. T10 now
requires at least 1800 s of overlap and takes the 10 ms threshold from
`sync.tier_budgets.T0` in the contract; `measure_two_pod_sync.py` also refuses
two captures from the same pod.

**Per-unit configuration is traceable and fail-closed.**
The BLE device-config JSON parser is now a production header
(`firmware/esp32_band/config/synapse_config_json.h`) shared with a compiled host
test. It rejects signed/overflowing integers, duplicate keys, quoted values,
malformed arrays and trailing junk. A BLE write of a per-unit field is accepted
only when a provenance-complete USB calibration already exists; the four
provenance fields travel in the readback and into the host record.
`feature_rate_hz` is explicitly session-only. `pending_reboot` compares the
sensor-effective CRC, so adding metadata no longer looks like an unapplied
profile. `device_profile.py configure` no longer commits the host record:
`verify` does, after the reboot and only on a complete match.

**Bench tooling actually usable on hardware.**
`factory_calibrate.py --dump` always prints the JSON record and never writes a
host file. `ppg_sweep.py` forwards a bench manifest, finds XDFs in both output
directories, requires exit 0, refuses a sweep with no usable signal, and
reboots the band between LED pairs using the new `cal reboot` factory command.

**Docs, contract and gate hygiene.**
The tester playbook, SOP, matrix, STATUS and increment docs now describe the
behaviour the code implements; `check_contract` asserts the fitted soak cell
capacity matches the flashed band-core value; the local gate header no longer
overstates its coverage and runs the new compiled parser test.

## Verification

Commands run on this host (2026-11-27) against the final tree:

- `ruff check .` / `ruff format --check .`: clean; `mypy` over `synapse24`,
  the SDK, `worker` and `backend`: clean (64 + 9 + 5 files).
- `check_contract` (167 checks), `generate_contracts --check`,
  `check_interfaces`, `check_baseline_claim`, phase1 dry-run: green.
- Full test suite in chunks with `--cov-append`: **all chunks green after the
  two fixes below**; `coverage report --include="src/*,worker/*,backend/*"`
  gives **83.72 %** (gate ≥ 80 %). SDK suite: **89.41 %**, 70 passed.
- C host gate: on Windows, 158 passed and 25 skipped by Application Control on
  freshly built DLLs (environment policy, not a code failure). The same 12
  modules were replayed in `ghcr.io/astral-sh/uv:python3.12-bookworm` with
  `uv sync --dev --frozen`: **183 passed, zero skips**, including the new
  compiled parser test (`-Wall -Wextra -Werror`). `gcc_probe.py`: "all the
  host-gate sources compile".
- Firmware builds: **not run on this host** (no local ESP-IDF image). The
  `firmware-band` step now builds five configs, including `band-bench` third;
  it must be green on the pushed branch before this increment is called merged.

Two failures found by the gate and fixed before this record: the shared parser
header initially lived in `tests/` (moved into the firmware tree), and the
new reboot command needed an `esp_system.h` host stub plus a corrected
`consumer_v2` lookup in the flashed-config expectation.

Hardware evidence remains open: no board, sensor, radio, long BLE read or
human session has been exercised. Status stays in `docs/hardware/STATUS.md`.
