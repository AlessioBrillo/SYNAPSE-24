# ESP-IDF Setup — SYNAPSE-24 Firmware Toolchain

Step 0 of the Hardware Readiness Gate. Nothing else in the firmware track is
worth doing until this is green:

```bash
uv run python tools/check_toolchain.py
```

## Why this comes first

At the time this document was written the repository carried ~50k lines of C/C++
across four parallel ESP32 projects, **none of which had ever been through a
compiler**. The canonical firmware is now `firmware/esp32_band`; the legacy apps
(`esp32_tier0`, `esp32_head`, `esp32_forearm`) are archived, and the shared
library `firmware/common/` is retained because `esp32_band` and the host-side
parity tests in `tests/firmware_host/` both depend on it.

The gate is: **`firmware/esp32_band` compiles with `-Wall -Wextra -Werror`**,
and CI proves it on every push so the gate can never silently regress.

## Pinned version: v5.5.5

The project pins **ESP-IDF v5.5.5** everywhere (local host + CI
`firmware-build` job). Rationale (verified October 2026):

- v5.5.5 is the **latest stable 5.x** (16 Jul 2026). v5.3 is at end of
  maintenance (last bugfix v5.3.6, 15 Sep 2026) and v6.0/6.1, while released,
  carry breaking changes the firmware has not been evaluated against — the
  NimBLE and TFLM component APIs used here are 5.x APIs.
- Installing a version other than v5.5.5 locally reintroduces the
  "compiles in CI, fails on my desk" failure mode the pin exists to prevent.

## 1. Install

### Windows

Option A — pinned download (recommended, reproducible):

```powershell
# scripts/tools/download_esp_idf.ps1 fetches the pinned v5.5.5 archive from
# the Espressif asset CDN (falls back to codeload), with resume + retry for
# flaky links, into %USERPROFILE%\esp\esp-idf-v5.5.5.zip. Expand it, then:
powershell -ExecutionPolicy Bypass -File scripts\tools\download_esp_idf.ps1
tar -xf $env:USERPROFILE\esp\esp-idf-v5.5.5.zip -C $env:USERPROFILE\esp
Rename-Item $env:USERPROFILE\esp\esp-idf-v5.5.5 $env:USERPROFILE\esp\esp-idf
powershell -ExecutionPolicy Bypass -File scripts\tools\fetch_idf_submodules.ps1
cd $env:USERPROFILE\esp\esp-idf
.\install.ps1 esp32s3
```

Option B — git clone (simplest on a reliable link):

```powershell
git clone -b v5.5.5 --recursive https://github.com/espressif/esp-idf.git $env:USERPROFILE\esp\esp-idf
cd $env:USERPROFILE\esp\esp-idf
./install.ps1 esp32s3
```

Option C — GUI installer: download
<https://dl.espressif.com/dl/esp-idf/esp-idf-tools-setup-1.2.exe> and select
**v5.5.x** (do not let it default to a different 5.x, and do not use 6.x yet).
When the installer asks which targets to install, select **ESP32-S3**.
Install the USB-serial driver for your board (CP210x or CH340).

> **Submodules (zip installs only):** the release ZIP (`esp-idf-v5.5.5.zip`,
> ~1,9 GB, root folder `esp-idf-v5.5.5`) does not contain submodules.
> `scripts/tools/fetch_idf_submodules.ps1` materialises the ten submodules the
> Band build needs (NimBLE host, the esp32c3-family BLE controller used by
> S3, wifi/phy/coex libs, lwip, mbedtls, tlsf, cJSON, micro-ecc) **at their
> exact v5.5.5-pinned SHAs** (depth-1 fetch by SHA, with retry). Without
> `esp-nimble`/`esp32c3-bt-lib` the NimBLE build fails at CMake time.
>
> **`version.txt` does not exist in the ZIP archive** (it is generated from
> `git describe`). `tools/check_toolchain.py` falls back to `idf.py --version`
> automatically, so this is handled — no manual step.

Activate the environment in **every new terminal** (dot-source it, or the
environment variables do not persist to the rest of your session):

```powershell
. $env:USERPROFILE\esp\esp-idf\export.ps1
```

### Linux / macOS

```bash
git clone --depth 1 --branch v5.3 --recursive https://github.com/espressif/esp-idf.git ~/esp/esp-idf
cd ~/esp/esp-idf && ./install.sh esp32s3
source ~/esp/esp-idf/export.sh
```

## 2. Verify

```bash
uv run python tools/check_toolchain.py
```

Expected: every `OK`, no `FAIL`. A `WARN` on the Python interpreter is acceptable
if you build through the `uv`-managed 3.11 used by the rest of the project.

## 3. Python interpreter

ESP-IDF 5.x runs on Python 3.8+. This project pins its host interpreter to
**3.11** (`pyproject.toml`: `requires-python = ">=3.11,<3.15"`).

If your system Python is newer (3.12/3.14 are common) do **not** force ESP-IDF
onto it. Let IDF use its own bundled interpreter for the build, and use the
`uv`-managed interpreter for the host tooling.

## 4. Build variants

The Band firmware has two build profiles, selected with `SDKCONFIG_DEFAULTS`:

| Variant | Sensors / features | Use |
|---|---|---|
| `band-core` | ECG (AD8232) + PPG (MAX30102) + IMU (ICM-20948), no GPS, no TMP117, no FIT, no provisioning, no OTA | **Smoke kit bringup** — the €65–90 rig. This is what Phase 1 hardware validation runs against. |
| `band-full` (default) | Everything: + GPS MAX-M10S, + TMP117, + FIT writer, + BLE provisioning, + signed OTA | Product firmware for the Synapse Band v1. |

`band-core` exists so that bringup does not block on hardware you have not
bought yet. It is the default for `scripts/build_and_flash_firmware.py`.

```powershell
# band-core (smoke kit)
$env:SDKCONFIG_DEFAULTS = "sdkconfig.defaults.core;sdkconfig.defaults"
idf.py set-target esp32s3
idf.py build
```

```bash
# band-full (default behaviour, no env var needed)
idf.py set-target esp32s3
idf.py build
```

## 5. Flash

```powershell
idf.py -p COM3 flash monitor
```

Expected first-boot log:

```
I (xxx) SYNAPSE_BAND: === Synapse Band v1 Starting ===
I (xxx) SYNAPSE_SENSORS: Sensors initialized (ECG:500Hz, PPG:50Hz, IMU:50Hz)
I (xxx) SYNAPSE_RESEARCH: Research service ready (raw PPG/ECG/IMU + sync)
I (xxx) SYNAPSE_BAND: Entering main loop
```

If you see `ESP_ERR_NOT_FOUND` from the PPG or IMU driver, the I²C bus is wrong:
the Band firmware hard-codes **SDA = GPIO 8, SCL = GPIO 9** (ESP32-S3 has no
usable GPIO 21–25 on octal-PSRAM modules — do not use the classic ESP32 21/22
pair).

## 6. Partition table

`sdkconfig.defaults` requests a 16 MB flash with a custom partition table
(`partitions.csv`): 1.5 MB app + 2 MB TFLM arena + OTA slots + NVS + SPIFFS for
FIT storage. If your board only has 4 MB, set
`CONFIG_ESPTOOLPY_FLASHSIZE_4MB` and disable `SYNAPSE_FEATURE_FIT_STORAGE`.

## References

- `firmware/esp32_band/README.md` — module layout and BLE GATT contract
- `HARDWARE_BRINGUP.md` — wiring, electrodes, live validation
- `docs/hardware/READINESS_GATE.md` — the plan this step belongs to
