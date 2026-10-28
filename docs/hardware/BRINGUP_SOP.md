# Hardware Bringup SOP — from box to first valid XDF

The step-by-step procedure for the smoke kit (G1). Every step has an explicit
check; do not proceed past a failure, because a failure here is invisible later
as corrupted data rather than as a broken build.

> **Before anything else:** `uv run python tools/check_toolchain.py` must pass.
> If ESP-IDF is missing, stop and read `ESP-IDF_SETUP.md`. Building firmware
> without a toolchain check is how you burn an afternoon on a driver bug that
> was never installed.

> **Soak mode needs LabRecorderCLI.** The 6-hour soak (`--soak`) records
> continuously through LabRecorderCLI, which is *not* installable from this
> repository (it is the single writer for the real soak): download it from
> <https://github.com/labstreaminglayer/App-LabRecorder> (Windows: run the
> installer; the default path is already searched), then either leave it on
> PATH or point `SYNAPSE_LABRECORDER_CLI` at it. Doing this after the rig is
> wired is a wasted evening; do it with the rest of the prerequisites.
> (`--soak-rehearsal` does not need the recorder: it writes a marked
> synthetic spool, and its output is never G1 evidence.)

## 0. Wiring

ESP32-S3 DevKitC-1 ← sensor breakouts. **I²C on GPIO 8 (SDA) / 9 (SCL)** —
these values are asserted by `tools/check_contract.py` against both the
manifest and the firmware. Do not substitute 21/22: those pins are unusable on
octal-PSRAM S3 modules.

| Signal | GPIO |
|---|---|
| AD8232 OUTPUT | 36 (ADC1_CH0) |
| AD8232 LO+ / LO− | 39 / 34 |
| MAX30102 + ICM-20948 + INA219 SDA | **8** |
| MAX30102 + ICM-20948 + INA219 SCL | **9** |
| MAX30102 INT | 5 |
| ICM-20948 INT | 6 |
| All VCC | 3V3 |
| All GND | GND |

**INA219 goes in series with the rig's supply, not on a tap.** The monitor
measures the current through its shunt: battery+ → INA219 `VIN+`, INA219
`VIN−` → the rail that feeds the ESP32-S3 and every sensor (or the ESP32's
`5V/VIN` input if you ring-fence the MCU only — document which you did). With
only the I²C wires attached the POWER stream will happily report `0 mA`, and a
zero reading is indistinguishable from a working measurement. Before the first
session, verify the reading against a **known load** (a resistor drawing a
known current from the same rail); the ratio measured/expected must be ~1
(§4a). `hardware.yaml` declares the 0.1 Ω shunt and address 0x40; a shunt of a
different value is a config fix, not a firmware one.

Add 4.7 kΩ pull-ups on SDA/SCL if your breakouts do not include them — a
missing pull-up presents as `ESP_ERR_NOT_FOUND` on the PPG PART_ID.

Electrodes: RA (red) proximal near the elbow, LA (yellow) distal near the
wrist, RL (green) on a bony prominence as reference. Clean the skin with
alcohol; a wet electrode is the difference between a clean QRS and noise.

> **`ecg_config.gpio_drdy: 4` in `hardware.yaml` is optional.** The driver
> uses it as the lead-off sense pin. If your AD8232 clone has no DRDY/LO pin
> free on GPIO 4, leave it unconnected (the internal pull-up then reads as
> "lead-off" permanently — noisy logs, but no effect on streaming), or set
> `gpio_drdy: -1` in `config/hardware.yaml`. Do not rewire the I²C bus over it.

## 1. Build and flash

```bash
uv run python tools/check_contract.py
uv run python scripts/build_and_flash_firmware.py --variant band-core \
    --port COM3 --clean monitor
```

Expected first-boot sequence:

```
I (xxx) SYNAPSE_BAND: === Synapse Band v1 Starting ===
I (xxx) SYNAPSE_SENSORS: Sensors initialized (ECG:500Hz, PPG:50Hz, IMU:50Hz) [band-core]
I (xxx) SYNAPSE_RESEARCH: research service registered (6 streams, raw samples only)
I (xxx) INA219: INA219 ready at 0x40 (bus 3.70 V, shunt 0.100 ohm)
I (xxx) SYNAPSE_POWER: INA219 power monitor active: measured current telemetry enabled
I (xxx) SYNAPSE_POWER: power telemetry sink attached
I (xxx) SYNAPSE_BAND: Entering main loop
```

`[band-core]` must appear. If GPS/Temp lines appear instead, the wrong variant
was built. The INA219 lines appear only when the monitor is stocked and
answering at 0x40; without it the rig still boots and reports the current
figure as a tier-model estimate rather than a measurement — the SOP's power
row then says so instead of inventing a number.

## 2. Confirm the pod is discoverable

Point `nRF Connect` (or the host client) at the research service
`A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C70` and confirm seven characteristics exist:
ECG, PPG, ACC, GYRO, MAG, POWER, SYNC. Enable notifications on the ones you want.

If the service is absent, the firmware is not the one you think it is: the
research service is registered only when `CONFIG_SYNAPSE_FEATURE_RESEARCH_SERVICE=y`,
which `sdkconfig.defaults.core` sets.

## 3. Capture and validate

```bash
export SYNAPSE_FOREARM_BLE="aa:bb:cc:dd:ee:ff"   # or leave as auto
uv run python scripts/validate_phase1_entry.py --live-hardware \
    --config config/hardware_bringup.yaml --duration 60
```

The validator must report, in this order:
1. streams resolved with the expected rates (500 / 50 / 50 / 50 / 50),
2. HR within tolerance of a manual pulse count,
3. RMSSD within tolerance of a NeuroKit2 reference on the same recording,
4. PPG SQI ≥ 0.3 and MAP ≤ 0.5 sustained,
5. **zero dropped frames**,
6. XDF round-trip valid and timestamps monotonic.

Step 5 is the one people skip. A recording with a hole is worse than a
recording that stopped, because the hole is silent. `ResearchStreamDecoder`
counts it; do not override it.

### 3a. Staged stream subscription (BLE budget triage)

The full rig streams ~2,3–2,4 kB/s sustained (ECG 1,0 + PPG 0,42 + IMU 0,95;
see `config/hardware.yaml` `raw_frame.*`). That fits the physical link with
MTU 247 + DLE, but if measurement 4 (dropped frames) is non-zero it means the
negotiated link is under-performing. Debug in stages by commenting
characteristics out of `config/hardware_bringup.yaml` `bringup.ble`:

1. First run: **ECG only** (~1,0 kB/s). Zero drops here → the link and the
   frame path are sound; the fault is capacity, not correctness.
2. Second run: ECG + PPG (~1,4 kB/s).
3. Third run: everything, including the three IMU sub-streams.

Before blaming capacity, check that **DLE was negotiated** (link-layer
packets of 251 B, not the default 27 B): `nRF Connect` shows it under the
connection ("Data Length"). With 27-byte PDUs the ~244-byte notifications
fragment into 9 link-layer packets each and the budget collapses. If DLE is
missing on the pod side, set the preferred data length after connect
(`esp_ble_gap_set_data_length`) — one function call in the research service.

### 3b. Two-pod bench — the number that actually gates the Cerelog

G2's criterion #1 is **pod↔pod** alignment, not pod↔host. The second devkit
from the kit plus the ACC cross-correlation method make that measurable today,
for €0:

1. Wire the two devkits **rigidly coupled** to the same support (both boards
   on one block, or taped to the same rigid object). The method is
   cross-correlation of accelerometer streams: two pods moving as one body is
   the only configuration where `min_acc_correlation: 0.7` is physically
   attainable — pods on different body sites (head vs forearm) see different
   motion and will never correlate, whatever the clocks do.
2. Flash both with `band-core`; they advertise as two `SYNAPSE-BAND` devices.
3. Subscribe to ECG+PPG+ACC on both; run the validator once per pod; align
   the two recordings via `PodClockModel`/`ACC cross-correlation` and record
   the residual. **≤10 ms over 30 min PASSES** the sync precondition for the
   Cerelog order.
4. The wired-sync line (GPIO 27 between the pods) is driven by the hub-side
   driver already compiled into band-core, but *emitting* the pulse is a G2
   feature (real head pod, second body site): for the bench measurement above
   leave GPIO 27 unconnected.

Write the residual into `gate_report.json` under `sync.pod_pair_residual_ms`
— `PROCUREMENT.md` treats the pod↔pod number, not pod↔host, as the evidence
for spending €350.

## 4. Record the numbers

Write every measured value into `data/live_validation/gate_report.json` —
current per tier, throughput, drift, HR/RMSSD errors, SQI/MAP. `PROCUREMENT.md`
treats those numbers as the only legitimate basis for the Phase 2 order.

### 4a. Bench characterization (before wearing the rig)

Three cheap bench numbers that make the G1 dataset interpretable later:

1. **AD8232 noise floor**: electrodes unplugged, RA/LA/RL shorted together,
   60 s of recording. Record the std of the raw stream in µV-equivalent units.
   *The µV-equivalent is only true once the AD8232's gain is calibrated
   (next): use the ADC counts until it is.*
2. **AD8232 gain calibration (per-rig).** Generic clones' gain is set by
   their instrumentation resistors, not by `hardware.yaml`'s `gain: 6.0`
   placeholder. Feed a known signal into the electrode inputs (a function
   generator through a divider, or a 1 mVpp 10 Hz sine), read the ECG stream,
   and compute `gain_measured = (mV measured at the output) / (mV applied)`.
   Write it into `config/hardware.yaml` (`ecg_config.gain_measured`); the µV
   scale of every ECG SNR claim hangs on this number, so it is per-rig and
   recalibrated whenever a breakout is replaced.
3. **I²C sanity**: if the PPG or IMU is absent from the boot log, add 4,7 kΩ
   pull-ups on SDA/SCL before replacing sensors — see the troubleshooting
   table. (The kit assumes your breakouts include them; if not, add them
   before blaming the sensors.)
4. **INA219 reality check**: with the known load from §0 (wiring), the POWER
   stream's `current_ma` must match the resistor's expected current within a
   few percent; a zero or wildly low reading means the shunt is miswired, not
   that the pod is efficient.

### 4b. Motion session (informational, not a gate)

Record 5 minutes of walking plus desk-tapping while wearing the rig. This is
**not** a G1 pass criterion (the gate is seated-and-still per `PROCUREMENT.md`),
but it is the first real data the motion gate (`Architecture.md §74`) will ever
have seen. Save it alongside the gate report; do not let it annul a green run.

## 5. Soak test — the MVP bar

The SOP's 30-minute zero-drop run is a proxy. The project promises 24/7
Tier-0, and the pod's 32-bit microsecond timestamp wraps every ~71.6 minutes —
so only a multi-hour run exercises `DeviceClock` for real. (The wrap path is
also exercised in CI by `tests/test_live_wrap_timecompression.py` — 10 hours
of device clock compressed into milliseconds, with the wrap flag on *and*
off — so the bench run is the physical confirmation, not the only witness.)

**MVP criterion: one 6-hour continuous recording (overnight is fine) with
zero dropped frames, monotonic XDF, and the pod on battery.** Passing that,
plus the SOP numbers in `data/live_validation/gate_report.json`, is what
unlocks the G2 order.

There are exactly two soak modes, and only one of them counts:

| Mode | Command | What it proves |
|---|---|---|
| **Real soak (`--soak`)** — the G1 evidence | `uv run python scripts/validate_phase1_entry.py --live-hardware --config config/hardware_bringup.yaml --duration 21600 --soak` | six physical hours on the pod: radio, ring, clock wrap, battery. Writes the XDF through LabRecorderCLI (single writer, see the note at the top) and the gate report to `data/live_validation/gate_report.json` (override with `--gate-report`) |
| **Rehearsal (`--soak-rehearsal`)** — machinery proof only | `uv run python scripts/validate_phase1_entry.py --live-hardware --config config/hardware_bringup.yaml --duration 300 --soak-rehearsal` | the same validation loop and gate-report path over a deterministic synthetic spool, runnable in CI and without LabRecorderCLI. Its artifact marks itself (`rehearsal: true`, `source: "spool (synthetic rehearsal)"`) and is **never** G1 evidence |

The two flags are mutually exclusive — the script refuses the combination.
A rehearsal run is for CI and for verifying the harness before the real
overnight run; its gate report carries `rehearsal: true`, and
`tests/test_soak_truthfulness.py` pins that no rehearsal-row report can
be presented as the 6-hour soak.

Two rules that keep the bar honest:

- **Zero means zero.** Any dropped frame fails the run; a recording with a
  hole is worse than one that stopped, because the hole is silent. If drops
  appear, diagnose with the staged subscription of §3a (ECG first), fix the
  cause, and **re-run** — every attempt goes into the gate report, green or
  red. A half-empty history is a signal; a deleted history is a lie.
- **Size the battery before the run, not after.** The kit envelope is
  150–600 mW (`docs/hardware/POWER_BUDGET.md`): on a 500 mAh cell that is
  4.3–7.4 h of runtime. A soak that dies at hour three is a gate that never
  ran. Use the 1000+ mAh cell from the kit for the 6-hour soak, or log the
  battery percentage at start and end (`measured_battery_pct`) and say so.

### 5a. What the bench must show about the loss counters

The pod reports the loss in two independent ways, and the bench has to look
at both (they answer different questions):

| Where | What it means | What to do when it is not zero |
|---|---|---|
| **Sequence gap** in the host log (`dropped_frames` of the gate report) | whole frames the BLE stack refused: the host sees a hole in the 16-bit sequence | a hole in the sequence is a radio/bandwidth problem: shorten the connection interval, subscribe to fewer streams, re-run |
| **`dropped` byte** in the frame header (byte 11) | samples lost for that stream since the previous frame, saturated at 255 | the pod's ring overflowed (or a frame was refused): the flush threshold does not fit the radio budget — reduce the number of subscribed streams |

The two counters are not independent: a refused frame leaves a hole in the
sequence *and* is counted in the `dropped` byte of the next frame. A
`dropped` byte **without** a hole means the pod lost samples before framing
them (the ring overflowed). Both must be zero for the run to count, and the
`dropped` byte is in *samples*, not in values: a refused frame of a
3-channel stream loses 4 samples, not 12.

### 5b. How to read the advertisement on the bench

The pod advertises **21 bytes** (flags + the 128-bit consumer service UUID)
and answers the scan request with **18 bytes** (the complete name
`SYNAPSE-BAND` + the 16-bit Heart Rate UUID). If a scanner shows a name
without the service UUID, or a payload longer than 31 bytes, the build is
not the one this SOP describes: check that the firmware was flashed from
`firmware/esp32_band` with the `band-core` variant, and that the UUIDs came
from the generated contract header
(`uv run python tools/generate_contracts.py --check` must pass).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ESP_ERR_NOT_FOUND` on PPG PART_ID | I²C wiring or missing pull-ups | Verify GPIO 8/9, add 4.7 kΩ pull-ups |
| `[band-core]` missing from the boot log | Wrong build variant | Rebuild with `--variant band-core` |
| ECG reads 0, PPG/IMU fine | AD8232 unpowered, or LO pins floating | Verify 3V3/GND; tie LO± to GND if unused |
| Research service absent | `RESEARCH_SERVICE` disabled or wrong firmware | Check `sdkconfig.defaults.core` |
| HR reads 0 or 200+ bpm | Electrode contact / motion | Clean skin, snug electrodes, hold still |
| Dropped frames > 0 | BLE budget exceeded | Reduce the streams you subscribe to |
| Timestamps jump backwards | 32-bit wrap mishandled | Confirm `DeviceClock` is in the path |

## References

- `ESP-IDF_SETUP.md` — toolchain and build variants
- `PROCUREMENT.md` — the gates and the measurement SOP
- `POWER_BUDGET.md` — the two energy budgets (kit measured vs system target)
- `../../config/hardware.yaml` — the contract every pin and rate comes from
