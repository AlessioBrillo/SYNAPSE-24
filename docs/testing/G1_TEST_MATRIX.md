# G1 test matrix — one ID per reproducible check

Every row is a test the tester can run without guessing. Each run writes
`evidence/<ID>_<timestamp>.json` through `scripts/bench_suite.py` or the
script named in the command column. A failed run writes evidence too: deleting
it is not allowed.

The IDs match `docs/onboarding/TESTER_PLAYBOOK.md`.

| ID | What it proves | Command | Pass criterion | Evidence |
|---|---|---|---|---|
| T01 | Kit and toolchain are usable | `bench_suite.py incoming` | Toolchain READY and contract green; five manual rows checked | `evidence/T01_*.json` |
| T02 | The band has an identity and a factory record | `bench_suite.py factory --port COM3` | DIS/calibration serial `SYN-` + 12 hex | `evidence/T02_*.json` |
| T03 | Sensors answer on the I²C bus | `bench_suite.py sensors --port COM3 --log boot.log` | Boot/factory log contains 0x57 PPG, 0x68 IMU, 0x40 INA219 | `evidence/T03_*.json` |
| T04 | The flashed config is the contract | `validate_phase1_entry.py --live-hardware …` + gate report | Every readback field equals `config/hardware.yaml`; a missing field is FAIL | `data/live_validation/gate_report.json` |
| T05 | 60 s of raw streaming without loss | `bench_suite.py stream --live --duration 60 --manifest <bench.json>` | Fresh gate report, zero sequence gaps, zero `dropped` bytes, valid XDF | `evidence/T05_*.json` |
| T06 | PPG quality is sustained | Same run as T05 | SQI ≥ 0.3 and MAP ≤ 0.5 for > 30 s | `gate_report.json` |
| T07 | Heart rate agrees with a manual pulse count | Same run with a **human** manifest + stopwatch | MAE ≤ 2 bpm | `gate_report.json`, `verified_by` |
| T08 | RMSSD agrees with NeuroKit2 | Same run + reference `.npz` | MAE < 5 ms | `gate_report.json` |
| T09 | INA219 reads a known load | `g1_measure.py` / multimeter | Current within ±5 % of `V/R` | `gate_report.json` |
| T10 | Two pods stay aligned | `bench_suite.py sync --pods a.xdf b.xdf` | Correlated ACC windows and residual ≤ 10 ms over 30 min | `evidence/T10_*.json` |
| T11 | The SOP gate report exists | `bench_suite.py g1-report --live --manifest <manifest.json>` | Fresh report, `rehearsal: false`, overall PASS/FAIL recorded | `evidence/T11_*.json` |
| T12 | Six-hour soak | `bench_suite.py soak --live --manifest <manifest.json> --duration 21600` | Fresh report, zero drops, monotonic XDF, battery sufficient | `data/live_validation/gate_report.json` |

## Rules

1. **No N/A on a live run for a required gate.** A missing reference, a silent
   POWER stream or an unreadable config is FAIL. Human-reference gates are N/A
   only in a `kind: bench` manifest, where no person was measured.
2. **Three attempts before a FAIL** for a BLE read that may be transient; all
   three are logged.
3. **A hand-filled row needs two names**: the tester who measured and the
   maintainer who verified (`--verified-by` for `--bench-values`). The row is
   recorded inside the run's history, not typed into a later report.
4. **A soak is not a rehearsal.** `--soak-rehearsal` output is marked
   `rehearsal: true` and is never evidence for T12.
5. **Retention is 24 months.** Keep manifests, XDF and reports together under
   `data/sessions/` (human) or `data/bench/` (technical). Both are gitignored.
