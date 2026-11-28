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

## Safety — read before touching an electrode

This rig is a research prototype, **not a medical device**, and it is built from
breadboard parts with no galvanic isolation. These rules are not optional:

1. **Electrodes on the body only when the board runs on its LiPo, with the USB cable
   unplugged.** A board on USB is referenced to a PC (and through its charger to
   mains); a body-worn ECG input in that loop has no isolation barrier. Flash,
   configure and read logs *before* the electrodes go on, then unplug.
2. **Live monitoring while wearing the rig is BLE only**, or a laptop on battery and
   not charging. If you must have USB while wearing electrodes, use a USB isolator
   rated for it (optional, not part of the kit) — never a bare cable.
3. **A signal generator or any mains-powered bench instrument is connected to the
   inputs only off the body** (the gain test of §4a). Never to a person.
4. **LiPo:** charge off the body and never unattended; use cells with a protection PCB;
   do not use a swollen, dented or hot cell; never short the JST; keep it out of the
   strap contact area.
5. **Electrodes:** single-use Ag/AgCl, clean dry skin, no broken or irritated skin;
   remove at the first sign of redness or itching; do not wear for more than the
   duration of the session you are running (the 6 h soak is the longest).

If any of these cannot be met for a step, skip the on-body part of that step and use
the generator/bench alternative.

## 0. Wiring

ESP32-S3 DevKitC-1 ← sensor breakouts. **I²C on GPIO 8 (SDA) / 9 (SCL)** —
these values are asserted by `tools/check_contract.py` against both the
manifest and the firmware. Do not substitute 21/22: those pins are unusable on
octal-PSRAM S3 modules.

| Signal | GPIO |
|---|---|
| AD8232 OUTPUT | **1** (ADC1_CH0) |
| AD8232 LO+ (lead-off) | **15** (optional, see below) |
| Battery divider (optional) | **4** (ADC1_CH3) |
| MAX30102 + ICM-20948 + INA219 SDA | **8** |
| MAX30102 + ICM-20948 + INA219 SCL | **9** |
| MAX30102 INT | 5 |
| ICM-20948 INT | 6 |
| Wired sync (two-pod bench only) | **10** |
| All VCC | 3V3 |
| All GND | GND |

> **GPIO 26-37 are not usable on the DevKitC-1 N8R8**: 26-32 are the SPI flash and 33-37 the octal PSRAM. Earlier revisions of this document put signals on GPIO 27, 34 and 36; do not wire those.
> The pin map is `config/hardware.yaml` / `firmware/esp32_band/config/Kconfig`;
> `tests/test_firmware_first_light.py` fails if two functions share a pin or one lands on 26-37.

**INA219 goes in series with the rig's supply, not on a tap.** The monitor
measures the current through its shunt: battery+ → INA219 `VIN+`, INA219
`VIN−` → the rail that feeds the ESP32-S3 and every sensor (or the ESP32's
`5V/VIN` input if you ring-fence the MCU only — document which you did). With
only the I²C wires attached the POWER stream will happily report `0 mA`, and a
zero reading is indistinguishable from a working measurement. Before the first
session, verify the reading against a **known load** (a resistor drawing a
known current from the same rail); the ratio measured/expected must be ~1
(§4a). The 0.1 Ω shunt and address 0x40 are declared once in
`config/hardware.yaml` (`metadata.ina219_config`); `tools/generate_contracts.py`
generates the C macros the driver uses, and `tools/check_contract.py` fails on
drift. Do not edit Kconfig to change a shunt: change the contract.

Add 4.7 kΩ pull-ups on SDA/SCL if your breakouts do not include them — a
missing pull-up presents as `ESP_ERR_NOT_FOUND` on the PPG PART_ID.

Electrodes: RA (red) proximal near the elbow, LA (yellow) distal near the
wrist, RL (green) on a bony prominence as reference. Clean the skin with
alcohol; a wet electrode is the difference between a clean QRS and noise.

> **`ecg_config.gpio_lead_off: 15` in `hardware.yaml` is optional.** The driver
> uses it as the lead-off sense pin (LO+; the AD8232 breakouts expose LO+ and
> LO− separately, OR them through two diodes if you want both). The pin is
> pulled **down**: if your clone has no LO pin, leave GPIO 15 unconnected and it
> reads "contact" — the `lead_off` flag of the frames then stays 0, which means
> "not flagged", not "contact verified". (It used to be pulled up and read
> "lead-off" for ever.) The ECG stream is never gated on it. After a real
> lead-off (≥ 1 s) followed by steady contact the driver measures the ECG bias
> again, which takes one second without ECG samples (reported as a gap in
> `dropped`). With the pin unwired nothing ever flags a lead-off, so that re-measure never
> happens either: start the band with the electrodes already applied (the bias is measured
> in the first second). Or set `gpio_lead_off: -1`.
> It used to be GPIO 4, which is also the battery-divider input: do not share them.

## 0b. Device identity and calibration

The band advertises a stable serial built from the **full six MAC bytes**
(`SYN-` + 12 hex) at first boot and kept in NVS. The log line `SYNAPSE_ID`
prints it; copy it into `SYNAPSE_FOREARM_SERIAL_ID` before recording. The
validator binds to that serial (and derives the LSL `source_id`) instead of
taking the first device whose name contains `SYNAPSE`; with two bands on the
bench an ambiguous scan is an error, not a guess.

The **`band-bench`** image already enables `CONFIG_SYNAPSE_FACTORY_MODE=y`, so
the USB serial console offers a calibration shell (`help` lists it) without
`menuconfig`. The host drivers are:

```bash
uv run python scripts/factory_calibrate.py --port COM3 --dump
uv run python scripts/factory_calibrate.py --port COM3 --gain-json <gain.json> \
    --led 1F,1F --battery 1000 --operator "<name>"
# Over BLE, with atomic readback verification:
uv run python scripts/device_profile.py configure <serial> --address <ble-addr> \
    --profile profile.json
uv run python scripts/device_profile.py verify <serial> --address <ble-addr>
```

Commands write a versioned, CRC-checked NVS record that the sensor layer
applies before configuring the drivers: ECG gain, PPG LED currents, IMU zero
offsets and the fitted cell capacity. `cal erase` returns the band to Kconfig
defaults. A profile write sets `pending_reboot: 1` in the readback: reboot the
band before recording, or `device_profile.py verify` fails. The resulting
`data/calibration/<serial>.json` is manufacturing data and is not committed.
Writing per-unit values over BLE is allowed **only when a USB-factory-calibrated
record with complete provenance already exists** for that serial (operator,
instrument, firmware version, gain uncertainty); BLE may then edit only that
provenance-bearing record — it cannot create a new calibrations from scratch,
and it must not be used to overwrite values that were not measured. `feature_rate_hz` is **session-only**: it is not
persisted into the record or the band's NVS.
The ECG mid-rail is **not** forced at boot: the driver re-measures it after
lead-off, and a stale stored DC offset would be worse than none.

Keep the nominal configuration frozen for G1 (ECG 1100x, PPG 50 Hz / 6.2 mA,
IMU 8 g / 500 dps). Record the bench sweep of LED current vs perfusion index
and the T2 IMU clipping check in the calibration JSON; change a nominal value
only with a new deliberate contract update, never mid-session.

The POWER stream's frame flag bit 1 (`power_is_model`) says whether a current
came from the INA219 or from the conservative kit model (425 mW). The G1 power
gate fails when any POWER frame carries the model bit: a target is not a
measurement.

## 1. Build and flash

```bash
rm -f firmware/esp32_band/sdkconfig   # a stale sdkconfig overrides the defaults (gain trap)
uv run python tools/check_contract.py
uv run python scripts/build_and_flash_firmware.py --variant band-bench \
    --port COM3 --clean monitor
```

Expected first-boot sequence:

```
I (xxx) SYNAPSE_BAND: === Synapse Band v1 Starting ===
I (xxx) synapse_i2c: port 0 up: SDA=8 SCL=9 400000 Hz
I (xxx) SYNAPSE_SENSORS: Sensors initialized (ECG:500Hz, PPG:50Hz, IMU:50Hz) [band-bench]
I (xxx) SYNAPSE_RESEARCH: research service registered (6 streams, raw samples only)
I (xxx) INA219: INA219 ready at 0x40 (bus 3.70 V, shunt 0.100 ohm)
I (xxx) SYNAPSE_POWER: INA219 power monitor active: measured current telemetry enabled
I (xxx) SYNAPSE_POWER: power telemetry sink attached
I (xxx) SYNAPSE_BAND: Entering main loop
```

The `synapse_i2c` line shows the wiring the firmware actually uses (the one bus of the PPG, IMU
and INA219; it is configuration, `CONFIG_SYNAPSE_I2C_*`, validated at boot). A sensor that does
not answer is reported (`<sensor> init failed ... continuing without it`) and skipped: the band
keeps running and the rest keeps streaming. During a capture, `ECG input stuck` means the signal
has been on a rail or constant for ~2 s (AD8232 unpowered, broken wire to the ADC pin): that is
not a quiet ECG, and the lead-off pin cannot tell it.

`[band-bench]` must appear — it is the core smoke-kit profile with the factory
shell enabled. If GPS/Temp lines appear, the wrong variant was built. A stale
`[band-core]` here is equally wrong: the boot log now prints the variant that
was actually compiled. The INA219 lines appear only when the monitor is stocked and
answering at 0x40; without it the rig still boots and reports the current
figure as a tier-model estimate rather than a measurement — the SOP's power
row then says so instead of inventing a number.

### 1a. When a sensor does not come up, and the factory test

If the PPG or the IMU fails to initialise, the boot log also lists who *does* answer on the I²C
bus (`I2C scan: 0x57 answers (MAX30102)` …). That separates the causes that used to look alike:
`nobody answers` is power, wiring or pull-ups; a part answering at another address is an address
setting (`CONFIG_SYNAPSE_PPG_I2C_ADDR`, the ICM-20948 AD0 pin: 0x68 or 0x69); the right address
answering with a failed init is the part itself.

The **factory test** is the whole check of an assembled band in one run. It is
already enabled by the `band-bench` image (`CONFIG_SYNAPSE_FACTORY_MODE`; it is
off in the product image). Flash that variant and leave the band **still on the
bench with no electrodes on it** (USB is fine then; the safety rule
at the top is about electrodes on a body). It takes about 13 s and prints (the values are illustrative: no band has run it yet):

```
I (xxx) FACTORY: bus 0: 0x0C AK09916
I (xxx) FACTORY:       registers: 00=48 01=09 31=00
I (xxx) FACTORY: bus 0: 0x40 INA219
I (xxx) FACTORY: bus 0: 0x57 MAX30102
I (xxx) FACTORY:       registers: 02=00 03=00 04=.. ... FE=.. FF=15
I (xxx) FACTORY: bus 0: 0x68 ICM-20948
I (xxx) FACTORY: [PASS] init_ppg: PPG initialised
I (xxx) FACTORY: [PASS] i2c_ppg: 0x57 answers
I (xxx) FACTORY: [PASS] selftest_ppg: id 0x15, die 27.5 C, LED 1F/1F, FIFO running
I (xxx) FACTORY: [PASS] selftest_imu: id 0xEA, |a|=1.00 g at rest, mag 41 uT
I (xxx) FACTORY: [PASS] stream_ppg: 500 samples in 10 s (50 Hz), mean IR 61234
I (xxx) FACTORY: FACTORY RESULT: PASS (14 checks)
```

Every line is `[PASS]` or `[FAIL]` with the number that decided it, and the last line is the
verdict. What a `[FAIL]` means: `init_*` the sensor did not initialise (read the lines above it);
`i2c_*` nothing answers at that address; `selftest_*` the part answers but is not healthy (the
detail says which check: identity, a die temperature out of range, LED registers that lost their
value, a FIFO that does not move, a band that is not at rest, a magnetometer that reads zero, an
INA219 that is not on the rail); `stream_*` a stream delivered less than 80 % of its rate in 10 s.

What it does **not** prove: accuracy (that is §4a), the ECG signal (a station has no electrodes;
the ECG stream is checked for delivery only, and `lead-off` / `stuck` are shown for information),
or the IMU against the datasheet self-test (not implemented; the check is a static sanity test at
rest). The registers printed are only those that are safe to read while the band runs: the
MAX30102 FIFO and interrupt-status registers and the AK09916 ST2 register are never read by the
dump, because reading them changes the part's state. The BLE report and a switch that needs no
reflash come with the BLE configuration (contract 2.4).

## 2. Confirm the pod is discoverable

Point `nRF Connect` (or the host client) at the research service
`A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C70` and confirm seven characteristics exist:
ECG, PPG, ACC, GYRO, MAG, POWER, SYNC. Enable notifications on the ones you want.

If the service is absent, the firmware is not the one you think it is: the
research service is registered only when `CONFIG_SYNAPSE_FEATURE_RESEARCH_SERVICE=y`,
which `sdkconfig.defaults.core` sets.

## 3. Capture and validate

> **Safety:** unplug USB before the electrodes go on (see *Safety* above). The
> capture runs over BLE; the PC never needs a cable to the board for this step.

```bash
export SYNAPSE_FOREARM_BLE="aa:bb:cc:dd:ee:ff"   # or leave as auto
# A human session needs "kind": "human", consent and a pseudonym (S###).
# An off-body bench run uses "kind": "bench" with an operator and a serial
# instead; it must never declare a subject or a consent that did not happen.
uv run python scripts/validate_phase1_entry.py --live-hardware \
    --manifest data/sessions/2026-11-26/manifest.json \
    --config config/hardware_bringup.yaml --duration 60
# Hand-measured rows belong to the same run and need a verifier:
#   ... --bench-values data/bench/values.json --verified-by "<maintainer>"
```

The validator must report, in this order:
1. streams resolved with the expected rates (500 / 50 / 50 / 50 / 50),
2. HR within MAE ≤ 2 bpm of a manual pulse count — set
   `validation.manual_hr_bpm` in `hardware_bringup.yaml` before the run;
   without it the gate is N/A and a live run FAILS,
3. RMSSD within MAE < 5 ms of a NeuroKit2 reference on the same recording —
   `validation.ground_truth_ecg_path` points to an `.npz` with key `r_peaks`;
   without it a live run FAILS,
4. PPG SQI ≥ 0.3 and MAP ≤ 0.5 sustained > 30 s,
5. **zero dropped frames**,
6. XDF round-trip valid and timestamps monotonic — on a soak this is the
   LabRecorder file itself, not a second XDF rebuilt from the spool,
7. measured INA219 POWER frames (a tier model does not count),
8. the pod's boot-time configuration matching `config/hardware.yaml`, read
   from its device-config characteristic.

The ECG stream in the validator's XDF is now written in **µV**, the same unit
`scripts/g1_measure.py` uses for the gain and noise numbers: do not apply the
gain a second time when comparing the recording with the bench measurements
(§4a).

Every gate has a status: PASS, FAIL or N/A. **N/A is not a pass** — the live
profile requires rows 2–8, so a missing reference, a silent POWER stream or a
pod that cannot report its configuration fails the run. Tier-1 sync and EEG
quality are N/A until a second pod and the Cerelog are actually connected.
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

Before blaming capacity, check that **DLE was negotiated** (link-layer packets of 251 B, not the default 27 B): `nRF Connect` shows it under the connection ("Data Length"). The firmware already requests it on connect (`ble_gap_set_data_len(conn_handle, 251, 2120)` in `ble_gatt_server.c`): if the peer still reports 27 B, the limitation is the host adapter or the phone, not a missing firmware call. Do not add a second data-length request to the research service.

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
2. Flash both with `band-bench`; they advertise as two `SYNAPSE-BAND` devices.
3. Subscribe to ECG+PPG+ACC on both; record both pods simultaneously (two
   recorders, or one PC with two BLE adapters). **T10 needs at least 30 minutes
   of overlap** — a shorter capture returns no residual, not a pass. Then
   compute the residual:

   ```bash
   uv run python scripts/bench_suite.py sync --pods pod_a.xdf pod_b.xdf
   ```

   The tool interpolates both ACC magnitudes on a 1 kHz grid, cross-correlates
   six windows across the overlap and writes **`residual_ms`** (not
   `sync.pod_pair_residual_ms`) with the observed correlation and the **worst**
   window lag (a median would hide a clock drifting out of tolerance at the
   end). A window below `min_acc_correlation: 0.7` returns **no residual**,
   never a zero. The 10 ms limit is not a literal: read it from
   `sync.tier_budgets.T0.max_residual_drift_ms` in `config/hardware.yaml`
   (10 ms today). **≤10 ms over ≥30 min PASSES** the sync precondition for the
   Cerelog order.
4. The wired-sync line (GPIO 10 between the pods, not GPIO 27: a flash pin on the N8R8) is driven by the hub-side
   driver already compiled into band-core, but *emitting* the pulse is a G2
   feature (real head pod, second body site): for the bench measurement above
   leave GPIO 10 unconnected.

Attach the tool's `pod_pair_sync.json` (or `evidence/T10_*.json`) to the gate
report — `PROCUREMENT.md` treats the pod↔pod number, not pod↔host, as the
evidence for spending €350.

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
   **Pass (provisional): std ≤ 30 µV on the stream as emitted** (the wire carries µV
   computed with the configured gain, nominal 1100, so this is ≈ 40 ADC counts).
   The tool reports `true_noise_uv` (std × nominal 1100 / configured gain) when
   a calibration record exists; that is the electrode-referred figure to keep, and the
   30 µV bound applies to it too. The bound comes from the AD8232 datasheet input
   noise (of the order of 100 µVpp ≈ 15 µV rms) plus margin for the ESP32 ADC; it is an
   estimate, not a measurement. If the first real run misses it, record the value,
   find the cause (floating RL, USB noise, wiring) and only then revise the
   threshold, with the reason, in this file.
2. **AD8232 gain calibration (per-rig).** Generic clones' gain is set by
   their instrumentation resistors, not by `hardware.yaml`'s `gain: 1100.0`
   nominal value. Feed a known signal into the electrode inputs (a function
   generator through a divider, or a 1 mVpp 10 Hz sine), read the ECG stream,
   and compute `gain_measured = (mV measured at the output) / (mV applied)`.
   **Write the measured value into the band's NVS**, never into `hardware.yaml`
   or `SYNAPSE_ECG_GAIN`: `scripts/g1_measure.py gain` emits the JSON that
   `scripts/factory_calibrate.py --gain-json` writes to
   `data/calibration/<serial>.json` and to the device. The firmware applies
   that record at boot, so no reflash is needed per unit. `CONFIG_SYNAPSE_ECG_GAIN`
   and `ecg_config.gain` are only the factory default; `ecg_config.gain_measured`
   in the manifest is documentation of the nominal value. The µV
   scale of every ECG SNR claim hangs on this number, so it is per-rig and
   recalibrated whenever a breakout is replaced. The generator is connected
   **off the body only** (see *Safety*).
   **Pass:** three consecutive runs agree within ±5 % (the 1 % divider and the
   generator amplitude set the floor); a `gain_measured` more than ±15 % away from
   the nominal 1100 is accepted but must be written down as "clone differs from
   nominal", and one more than 2× away means a wrong Kconfig value or breakout.
   **Proof:** the validator reads the device-config characteristic before the
   recording and fails `flashed_config_matches_contract` if the pod's live gain,
   rates, LED currents or battery capacity differ from the contract.
3. **I²C sanity**: if the PPG or IMU is absent from the boot log, add 4,7 kΩ
   pull-ups on SDA/SCL before replacing sensors — see the troubleshooting
   table. (The kit assumes your breakouts include them; if not, add them
   before blaming the sensors.)
4. **INA219 reality check**: with the known load from §0 (wiring), the POWER
   stream's `current_ma` must match the resistor's expected current within
   **±5 %**; a zero or wildly low reading means the shunt is miswired, not
   that the pod is efficient. Procedure: use a 1 % resistor of about 100 Ω
   (≈ 37 mA at 3.7 V), measure the rail voltage `V` with the multimeter, expected
   current = `V / R`; the INA219 reading **with the load minus without it** must be
   within ±5 % of that. (With the 0.1 Ω shunt, 37 mA is a 3.7 mV drop, well above
   the INA219's 10 µV step.)
5. **RR from the PPG (`CONFIG_SYNAPSE_RR_ENABLE`, off until this is done).** On the body, so
   battery only and USB unplugged (see *Safety*). Record 5 minutes seated
   with the PPG on a fingertip and a reference ECG (the band's own AD8232 on the other hand is
   enough) at the same time, with the RR switched on. Compare the intervals the band publishes
   (`rr_last`, in 1/1024 s) with the ECG R-R intervals beat by beat: the mean absolute error must
   be under 15 ms and RMSSD within 15 ms of the ECG's. If it is not, tune the thresholds in
   `firmware/esp32_band/signal_quality/synapse_rr.h` (amplitude floor, 20 % tolerance, 75 % clean
   share) and `firmware/esp32_band/main/synapse_feature_map.h` (SQI 0.5, motion 0.3), repeat, and only then enable the flag
   in the product build. The detector has only ever seen synthetic pulses
   (`tests/test_firmware_s5_rr.py`).

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
| **Real soak (`--soak`)** — the G1 evidence | `uv run python scripts/bench_suite.py soak --live --manifest data/sessions/<id>/manifest.json --duration 21600` | six physical hours on the pod: radio, ring, clock wrap, battery. Writes the XDF through LabRecorderCLI (single writer, see the note at the top) and the gate report to `data/live_validation/gate_report.json` (override with `--gate-report`) |
| **Rehearsal (`--soak-rehearsal`)** — machinery proof only | `uv run python scripts/validate_phase1_entry.py --config config/hardware_bringup.yaml --duration 300 --soak-rehearsal` | the same validation loop and gate-report path over a deterministic synthetic spool, runnable in CI and without LabRecorderCLI. Its artifact marks itself (`rehearsal: true`, `source: "spool (synthetic rehearsal)"`) and is **never** G1 evidence |

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
  150–600 mW (`docs/hardware/POWER_BUDGET.md`): on a 500 mAh cell (about
  1.85 Wh at 3.7 V) that is 3.1–12.3 h of runtime, so the worst case does not
  reach 6 h. A soak that dies at hour three is a gate that never
  ran. Use the 1000+ mAh cell from the kit for the 6-hour soak, or log the
  battery percentage at start and end (`measured_battery_pct`) and say so. The
  firmware divides the charge it has consumed by the **per-unit** `battery_mah`
  (1000 in `band-bench` defaults): if the cell you fit is not 1000 mAh, write its
  measured capacity with `factory_calibrate.py --battery <mAh> --reboot` (or
  `device_profile.py configure` after the USB record exists): editing
  menuconfig alone changes the compile-time default, not the value the band
  reports.

### 5a. What the bench must show about the loss counters

The pod reports the loss in two independent ways, and the bench has to look
at both (they answer different questions):

| Where | What it means | What to do when it is not zero |
|---|---|---|
| **Sequence gap** in the host log (`dropped_frames` of the gate report) | whole frames the stack accepted that never reached the host (a refused frame is retried with the same number): the host sees a hole in the 16-bit sequence | a hole in the sequence is a radio/bandwidth problem: shorten the connection interval, subscribe to fewer streams, re-run |
| **`dropped` byte** in the frame header (byte 11) | samples lost for that stream since the previous frame, saturated at 255 | the pod's ring overflowed or a frame could not be encoded: the flush threshold does not fit the radio budget — reduce the number of subscribed streams |
| **`dropped_frames`** in the Status record | notifications the BLE stack refused or had no buffer for. Nothing is lost by them (the frame is retried with the same sequence number and its samples stay queued), so neither a hole nor a `dropped` byte shows | a count that keeps growing is a congested link: it ends in a ring overflow and then in a `dropped` byte. Treat it as the early warning, not as a loss |

The counters are not interchangeable. A hole in the sequence is a frame the
stack accepted that never reached the host. A `dropped` byte is data the pod
discarded itself (ring overflow), with or without a hole. A refused
notification shows only in `dropped_frames`. All three must be zero for the
run to count, and the `dropped` byte is in *samples*, not in values: a
3-channel stream loses 4 samples, not 12 values.

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
| `ESP_ERR_NOT_FOUND` on PPG PART_ID | I²C wiring or missing pull-ups | Verify GPIO 8/9, add 4.7 kΩ pull-ups. The boot log's `I2C scan` lines say whether anything answers and at which address (§1a) |
| `[FAIL]` in the factory test | See §1a for what each line means | Fix the line named; the number in it is the measurement that failed |
| `[band-bench]` missing / `[band-core]` present in the boot log | Wrong build variant (stale image) | Rebuild with `--variant band-bench` |
| ECG reads 0, PPG/IMU fine | AD8232 unpowered | Verify 3V3/GND. Leave an unused LO± unconnected (grounding the module's LO outputs shorts them) or set `gpio_lead_off: -1` |
| ECG flat or stuck at a rail (about +1.3 / −1.5 mV) from the start | The band started with the electrodes off or still settling, so the resting level was measured from nothing | Apply the electrodes and restart the band; with LO+ wired, a long lead-off followed by steady contact re-measures it by itself |
| Research service absent | `RESEARCH_SERVICE` disabled or wrong firmware | Check `sdkconfig.defaults.core` |
| HR reads 0 or 200+ bpm | Electrode contact / motion | Clean skin, snug electrodes, hold still |
| Dropped frames > 0 | BLE budget exceeded | Reduce the streams you subscribe to |
| Timestamps jump backwards | 32-bit wrap mishandled | Confirm `DeviceClock` is in the path |

## References

- `ESP-IDF_SETUP.md` — toolchain and build variants
- `PROCUREMENT.md` — the gates and the measurement SOP
- `POWER_BUDGET.md` — the two energy budgets (kit measured vs system target)
- `../../config/hardware.yaml` — the contract every pin and rate comes from
