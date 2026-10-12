# Phase 1 — Next Steps

**Status: see `docs/hardware/READINESS_GATE.md`.** This file is kept for its
record of the earlier Phase 1 firmware work and is superseded by the readiness
gate for anything forward-looking.

## What this file used to claim, and what is true now

The previous version of this document listed five items as "remaining work".
Their state after the Hardware Readiness Gate:

| # | Item | Then | Now |
|---|---|---|---|
| 1 | Host BLE client / clock-sync validation on hardware | Pending hardware | Codec + decoder + HIL bench exist and are tested without hardware. Only the live run remains, and it needs the smoke kit. |
| 2 | IIR filter integration in the PPG task | Skeleton | Unchanged — the Band firmware's research path streams raw samples, so on-device DSP is a product concern, not a dataset one. Deferred past G1. |
| 3 | Power validation | Pending measurement | Part of the G1 SOP (`docs/hardware/PROCUREMENT.md`). |
| 4 | On-device R-peak TFLM validation | Pending | Deferred past G1: it is a product feature, not a prerequisite for recording the dataset. |
| 5 | LSL bridge end-to-end XDF | Framework complete | The wire format is pinned by C↔Python parity; the live path is exercised by the synthetic-pod bench. |

The important change: **items 1 and 5 no longer need hardware to progress,**
because the protocol is now tested against a synthetic pod that speaks the real
frame format.

## The real critical path

G0 used to be the only thing blocking the hardware order. It is **CLOSED
(2026-10-12)**: the toolchain check is READY and both firmware variants
compile on the dev host.

```
G0: firmware has never been compiled
    └── uv run python tools/check_toolchain.py        # currently BLOCKED
    └── install ESP-IDF 5.2+  →  docs/hardware/ESP-IDF_SETUP.md
    └── CI job firmware-build proves both variants

G1: smoke kit (~EUR 100)
    └── docs/hardware/BRINGUP_SOP.md
    └── docs/hardware/PROCUREMENT.md  (measurement SOP, 10 checks)

G2: Cerelog ESP-EEG (~EUR 350), only once G1 has measured
    the sync drift, the BLE budget and zero dropped samples
```

`tools/check_toolchain.py` reports READY with ESP-IDF v5.5.5 installed and
the environment activated (`export.ps1`). The first local compile found three
real defects (a silent variant-profile bug, an `extern "C"` around C++ TFLM
headers, and a stale-sdkconfig override) — exactly the class of bug G0 exists
to catch before hardware, not after.

## Commands

```bash
uv run python tools/check_toolchain.py
uv run python tools/check_contract.py
uv run pytest -q
uv run python scripts/build_and_flash_firmware.py --variant band-core --clean
```
