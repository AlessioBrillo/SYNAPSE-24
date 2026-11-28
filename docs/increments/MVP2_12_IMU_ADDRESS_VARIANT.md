# MVP-2.12 — IMU address as a build variant

**Status:** implementation complete on this branch; hardware verification (G1)
still open. No board, sensor or radio was used. This increment is an immutable
archive; `docs/hardware/STATUS.md` is the single source of truth.

The bringup docs have always accepted an ICM-20948 answering at 0x68 or 0x69
(AD0 low/high), but the contract pinned 0x68 and the tester is not allowed to
edit Kconfig. A breakout wired AD0-high was therefore unusable on the bench.

## What changed

**Wiring stays out of the calibration record.** The I²C address is a property of
the assembled rig, frozen in the schematic — not a per-unit measurement.
Adding it to the NVS record would have changed the CRC layout (and forced a v4
migration) and extended the BLE write whitelist for a value the product never
changes. Instead `config/hardware.yaml` declares
`imu_config.allowed_i2c_addrs: [0x68, 0x69]` and each address maps to a
committed firmware profile.

**A sixth build profile.** `firmware/esp32_band/sdkconfig.defaults.ad0` sets
`CONFIG_SYNAPSE_IMU_I2C_ADDR=0x69` on top of `band-core`/`band-bench`, keeping
the factory shell enabled. The flash script accepts
`--variant band-bench-ad0`, so the maintainer flashes a precompiled image and
the tester never touches Kconfig. The `firmware-band` CI step builds it and
asserts the address, the variant name and factory mode in the generated
`sdkconfig`.

**The contract enforces the mapping.** `tools/check_contract.py` rejects an
`imu_config.i2c_addr` outside the declared set and requires an existing
`sdkconfig.defaults` file setting the address for every allowed value; the
Kconfig default must still be 0x68. Generated artifacts (contract JSON and
`docs/hardware/SENSOR_CONFIG.md`) were regenerated from the manifest.

**Docs match the image.** `BRINGUP_SOP.md` §1a, T03 in the test matrix and
tester playbook, and `ESP-IDF_SETUP.md` now state that the flashed variant must
be consistent with the address observed on the bus (`band-bench` → 0x68,
`band-bench-ad0` → 0x69).

## Verification

`tools/check_contract.py`: 188 checks OK, including the new allowed-address and
build-profile mapping checks; generated artifacts in sync. The `firmware-band`
command sequence was replayed in `espressif/idf:v5.5.5` against a copy of the
tree: all six configurations built, with `sdkconfig` assertions for
`CONFIG_SYNAPSE_IMU_I2C_ADDR=0x69`, `CONFIG_SYNAPSE_BUILD_VARIANT="band-bench-ad0"`
and factory mode on; result **BAND_ALL_OK**. The Windows host cannot build the
ESP-IDF profiles with the available memory, so the server's `firmware-band`
step remains authoritative. No board or sensor was used.
