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
| T03 | Sensors answer on the I²C bus | `bench_suite.py sensors --port COM3` | 0x57 PPG, 0x68 IMU, 0x40 INA219 in the boot/factory log | `evidence/T03_*.json` |
| T04 | The flashed config is the contract | `validate_phase1_entry.py --live-hardware …` + gate report | Every readback field equals `config/hardware.yaml`; a missing field is FAIL | `data/live_validation/gate_report.json` |
| T05 | 60 s of raw streaming without loss | `bench_suite.py stream --live --duration 60` | Zero sequence gaps, zero `dropped` bytes, valid XDF | `evidence/T05_*.json` |
| T06 | PPG quality is sustained | Same run as T05 | SQI ≥ 0.3 and MAP ≤ 0.5 for > 30 s | `gate_report.json` |
| T07 | Heart rate agrees with a manual pulse count | Same run + stopwatch | MAE ≤ 2 bpm | `gate_report.json`, `verified_by` |
| T08 | RMSSD agrees with NeuroKit2 | Same run + reference `.npz` | MAE < 5 ms | `gate_report.json` |
| T09 | INA219 reads a known load | `g1_measure.py` / multimeter | Current within ±5 % of `V/R` | `gate_report.json` |
| T10 | Two pods stay aligned | Two devkits, ACC cross-correlation | Residual ≤ 10 ms over 30 min | `sync.pod_pair_residual_ms` |
| T11 | The SOP gate report exists | `bench_suite.py g1-report --live` | `rehearsal: false`, overall status PASS/FAIL recorded | `evidence/T11_*.json` |
| T12 | Six-hour soak | `validate_phase1_entry.py --live-hardware --soak --duration 21600` | Zero drops, monotonic XDF, battery sufficient | `data/live_validation/gate_report.json` |

## Rules

1. **No N/A on a live run for a required gate.** A missing reference, a silent
   POWER stream or an unreadable config is FAIL.
2. **Three attempts before a FAIL** for a BLE read that may be transient; all
   three are logged.
3. **A hand-filled row needs two names**: the tester who measured and the
   maintainer who verified (`verified_by` in the report).
4. **A soak is not a rehearsal.** `--soak-rehearsal` output is marked
   `rehearsal: true` and is never evidence for T12.
5. **Retention is 24 months.** Keep session manifests, XDF and reports
   together under `data/sessions/` (gitignored).
