# MVP-2.10 — Real-device onboarding truth

**Status:** implementation complete on this branch; hardware verification (G1) still open.
No board, sensor, radio or person was used in its implementation.

MVP-2.9 hardened the evidence and the per-unit configuration but left live
paths that a correctly wired band could still fail, or that could pass without
ever measuring a real device. This increment closes those paths. The intended
use remains wellness/research with no medical claims; the QMS work for a later
medical SKU is tracked in `docs/product/DECISIONS.md` and is not implemented
here.

## What changed

**The live capture no longer subscribes to incompatible UUIDs.**
`scripts/validate_phase1_entry.py` subscribed to every key of
`bringup.ble.characteristics`, including the service UUID and SYNC, which the
firmware declares `READ|WRITE` only. A real bleak client rejects both and the
recording aborts before the first sample; the synthetic transports no-opped and
hid it. Only the six notify characteristics are subscribed now; the selection
is pinned by `tests/test_device_onboarding_truth.py`.

**The flashed-config readback is checked on both sides.**
`tools/check_contract.py` now pins every readback key the firmware publishes
and every key the host validator expects, instead of seven of them. A field
dropped from either side can no longer keep CI green and fail on the bench.
The full JSON long-read still needs an HIL test with a real NimBLE stack —
`docs/hardware/STATUS.md` keeps it open.

**A calibrated cell is no longer refused.**
Both the validator and the per-unit profile derive the expected battery
capacity from the serial-bound calibration record when one exists. A band
carrying a real 1500 mAh cell and a matching host record now passes T04.

**The USB factory shell accepts real provenance.**
The C CLI used `strtok` and stored only the first token, so `cal operator "A.
Brillo"` was saved as `A.`; the host fake preserved the whole string and the
tests could never see it. The parser now accepts quoted values (and rejects
malformed ones loudly), the fake mirrors the real parser, a host-C test
compiles the actual CLI, and `factory_calibrate.py --reboot` polls for a reply
instead of sleeping a fixed two seconds.

**XDF files are self-describing and in declared units.**
Outlet headers carry channel labels, units and tier; the round-trip writer
preserves tier, device, model and `source_id`; the gyro stream is converted
from rad/s to the declared `dps` (a 57.3x silent error before). `uV` ASCII is
the canonical unit. Every live session records an LSL<->Unix anchor
(`utc0_s`/`lsl0_s`) and a required `utc_anchor_recorded` gate: without it a
recording cannot be exported to FIT later. XDF drift correction maps pod ids
to source ids explicitly, refuses a correction set that matches no stream,
preserves `<desc>` metadata, drops stale CLOCK chunks and derives channel byte
widths from `channel_format`.

**No synthetic data can satisfy a live gate.**
The validator no longer registers the hub as its own comparison pod (self
comparison gave corr=1, lag=0 and a free sync pass). The Tier-1 coordinator
pushes generated in-ear EEG only when explicitly constructed with a synthetic
transport; otherwise missing device timestamps drop the chunk instead of
fabricating `np.linspace` host stamps, and connect/start failures always set
`results[pod_id] = False`. The standalone live validator writes the full
spooled session, requires every configured stream and exposes a completeness
gate. `validate_live_sync.py` is labelled a loopback rehearsal and uses the
real `per_stream` result key; the Tier-1 residual now participates in its
verdict.

**Evidence stays fail-closed.**
Mode, manifest and hand-value refusals now append a failed attempt to
`history.jsonl` instead of exiting silently. The host-C gate runs through
`scripts/host_c_gate.py`: any skip or failure exits non-zero and prints the
skip reasons, so a policy-blocked DLL load can no longer be read as a pass.
Local and server gates share the same environment variables, the contract
codegen test writes all six artifacts into `tmp_path`, the gcc probe covers
the sources the host gate compiles, and a second coverage gate measures the
onboarding scripts (84.02 %).

**Operational docs match the shipped scripts.**
`--monitor`, `bench_suite.py soak`, `--bench-values`/`--verified-by`,
`band-bench`, `0x68`/`0x69`, measured `elapsed_s`, IMU orientation and the
OS-pairing prerequisite are now what the SOP, playbook, matrix and procurement
guide actually instruct. Stale claims (Actions as gate/release automation,
`firmware-build`, "Phase 0 complete", CRA duties in the future, edge triage
"implemented", 83 contract checks) were corrected. Increments remain an
immutable archive; `docs/hardware/STATUS.md` is the single source of truth.

## Verification

Baseline on `db5e295` before any change: `bash scripts/run_gate_locally.sh`
reported `GATE SEQUENCE: ALL GREEN` — 1471 passed, 134 skipped, 83.72 %
coverage; SDK 70 passed, 89.41 %; host C 157 passed, 43 OS-policy skips
(the old gate counted those skips as passes).

Final tree (2026-11-28):

- `ruff check .` and `ruff format --check .`: clean (330 files).
- mypy over `synapse24` (64 files), SDK (9) and `worker`/`backend` (5): clean.
- `check_contract` (182 checks), `generate_contracts --check`,
  `check_interfaces`, `check_baseline_claim`, phase-1 dry run: green.
- Full suite: **1652 passed, 27 skipped, 85.66 %** coverage (>= 80 %). SDK
  suite (70 passed, 89.41 %) and format conformance (11 passed) green. New
  onboarding-truth, host-C-wrapper and Tier-1/XDF tests added; the coverage
  target now includes the onboarding scripts (second gate: 84.02 %).
- Host C gate now **fails closed**: a smoke run of `host_c_gate.py --group
  parser` on this Windows host exited 1 with the OS-policy skip reason, as
  intended. Replay in `ghcr.io/astral-sh/uv:python3.12-bookworm`: **204 passed,
  zero skips** across 13 modules (including the compiled factory-CLI parser)
  plus 2 parser-step tests. The gate is expected RED on this Windows host;
  the server `firmware-band` result is still required before G1 can be called
  green.

Hardware evidence remains open: no board, sensor, radio, long BLE read, INA219
known-load check, six-hour soak or human session was exercised. Status stays
in `docs/hardware/STATUS.md`.
