#!/usr/bin/env python3
"""Cross-check the SYNAPSE-24 hardware contract.

``config/hardware.yaml`` is the single source of truth for sample rates, I2C
pins, BLE UUIDs, stream ids and LSL stream names. This tool verifies that the
firmware, the host validator, the documentation and the codec all agree with
it. Every check that exists here corresponds to an incoherence that has already
cost time in this project.

Run:
    uv run python tools/check_contract.py
    uv run python tools/check_contract.py --verbose
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
HARDWARE_YAML = ROOT / "config" / "hardware.yaml"
BRINGUP_YAML = ROOT / "config" / "hardware_bringup.yaml"
FIRMWARE_DIR = ROOT / "firmware" / "esp32_band"
SENSORS_C = FIRMWARE_DIR / "sensors" / "synapse_sensors.c"
CONFIG_KCONFIG = FIRMWARE_DIR / "config" / "Kconfig"
GATT_H = FIRMWARE_DIR / "ble" / "ble_gatt_server.h"
RESEARCH_C = FIRMWARE_DIR / "ble" / "ble_research_service.c"
# The UUIDs of the contract are generated from hardware.yaml (see
# tools/generate_contracts.py): the firmware includes this header instead of
# carrying a second copy of the contract.
CONTRACT_H = FIRMWARE_DIR / "ble" / "synapse_contract_generated.h"
# Decision Q7: there is ONE codec implementation, and it lives in the device
# SDK; src/synapse24/hardware/raw_codec.py is a pure re-export of it. The
# contract must never read the shim again: a re-export defines nothing, so
# pointing here would let every constant drift silently.
RAW_CODEC_PY = ROOT / "sdk" / "python" / "synapse_device" / "codec.py"
BUILD_SCRIPT = ROOT / "scripts" / "build_and_flash_firmware.py"
BRINGUP_DOC = ROOT / "docs" / "hardware" / "BRINGUP_SOP.md"
# FIT developer-field contract and every source that mirrors it. The declared
# directory is scanned for its .c/.h sources; the rest are single files.
FIT_CONTRACT = ROOT / "contracts" / "fit_developer_fields.json"
FIT_WRITER_H = FIRMWARE_DIR / "fit" / "fit_writer.h"
FIT_SOURCES = (
    FIRMWARE_DIR / "fit",
    ROOT / "scripts" / "export_session_fit.py",
    ROOT / "worker" / "hrv_worker.py",
    ROOT / "sdk" / "python" / "synapse_device" / "fit.py",
)
# C source suffixes when a FIT source is a directory.
_FIT_SOURCE_SUFFIXES = {".c", ".h", ".cpp", ".hpp"}
# ICM-20948 AD0 selects the I2C address. 0x68 is the Kconfig default; every
# other allowed address needs a committed sdkconfig.defaults profile so the
# firmware-band CI step proves it compiles. None = the Kconfig default itself.
IMU_ADDR_PROFILE_FILES: dict[int, str | None] = {0x68: None, 0x69: "sdkconfig.defaults.ad0"}


# MAX30102 SPO2_SR code (SPO2_CONFIG bits 4:2) -> Hz, from the datasheet.
PPG_RATE_REGISTERS = {
    0x00: 50,
    0x01: 100,
    0x02: 200,
    0x03: 400,
    0x04: 800,
    0x05: 1000,
    0x06: 1600,
    0x07: 3200,
}


@dataclass
class Report:
    """Outcome of the contract check."""

    failures: list[str] = field(default_factory=list)
    checks_run: int = 0

    def check(self, ok: bool, message: str) -> bool:
        self.checks_run += 1
        if not ok:
            self.failures.append(message)
        return ok


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _pod(config: dict, pod_id: str) -> dict:
    for pod in config.get("pods", []):
        if pod.get("pod_id") == pod_id:
            return pod
    raise KeyError(f"pod '{pod_id}' not found in hardware.yaml")


def _as_int(value: object) -> int | None:
    """Parse a YAML scalar that may be written as an int or a "0x.." string."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text, 0)
    except ValueError:
        return None


def check_sampling_rates(hw: dict, bringup: dict, report: Report) -> None:
    """PPG/IMU/ECG rates must agree across manifest, Kconfig and host outlets.

    Checks the declared rate against the MAX30102 register value that implies
    it, and against the LSL outlet the host actually creates.
    """
    pod = _pod(hw, "forearm_hub")
    meta = pod.get("metadata", {})
    rates = pod.get("sampling_rate", {})

    ppg_reg = _as_int(meta.get("ppg_config", {}).get("sample_rate_reg"))
    implied_ppg = PPG_RATE_REGISTERS.get(ppg_reg) if ppg_reg is not None else None
    report.check(
        implied_ppg == rates.get("ppg"),
        f"PPG rate mismatch: sampling_rate.ppg={rates.get('ppg')} but "
        f"ppg_config.sample_rate_reg=0x{ppg_reg:02x} implies {implied_ppg} Hz",
    )

    report.check(
        meta.get("imu_config", {}).get("accel_poll_hz") == rates.get("imu"),
        f"IMU rate mismatch: sampling_rate.imu={rates.get('imu')} but "
        f"imu_config.accel_poll_hz={meta.get('imu_config', {}).get('accel_poll_hz')}",
    )
    report.check(
        meta.get("ppg_config", {}).get("effective_rate_hz") == implied_ppg,
        "ppg_config.effective_rate_hz must equal the rate implied by sample_rate_reg "
        f"(no FIFO averaging): {meta.get('ppg_config', {}).get('effective_rate_hz')} "
        f"vs {implied_ppg}",
    )

    outlets = {
        stream["name"]: stream
        for stream in bringup.get("bringup", {}).get("lsl", {}).get("streams", [])
    }
    expected = {
        "SYNAPSE_ECG_T0": rates.get("ecg"),
        "SYNAPSE_PPG_T0": rates.get("ppg"),
        "SYNAPSE_ACC_T0": rates.get("imu"),
        "SYNAPSE_GYRO_T0": rates.get("imu"),
        "SYNAPSE_MAG_T0": rates.get("imu"),
    }
    for name, rate in expected.items():
        outlet = outlets.get(name)
        report.check(
            outlet is not None,
            f"LSL outlet '{name}' declared in hardware.yaml is missing from hardware_bringup.yaml",
        )
        if outlet is not None:
            report.check(
                float(outlet.get("sampling_rate", -1)) == float(rate),
                f"LSL outlet '{name}' declares {outlet.get('sampling_rate')} Hz but "
                f"hardware.yaml declares {rate} Hz",
            )


def _kconfig_default(symbol: str) -> int | None:
    """The integer ``default`` of ``config <symbol>`` in the firmware Kconfig, or None."""
    text = CONFIG_KCONFIG.read_text(encoding="utf-8") if CONFIG_KCONFIG.is_file() else ""
    block = re.search(rf"^config {symbol}\b(.*?)(?=^config |^endmenu|\Z)", text, re.S | re.M)
    if block is None:
        return None
    default = re.search(r"^\s*default\s+(\S+)", block.group(1), re.M)
    return _as_int(default.group(1)) if default else None


def _kconfig_text(symbol: str) -> str | None:
    """The raw ``default`` expression of ``config <symbol>``, or None."""
    text = CONFIG_KCONFIG.read_text(encoding="utf-8") if CONFIG_KCONFIG.is_file() else ""
    block = re.search(
        rf"^config {symbol}\b(.*?)(?=^config |^choice |^endchoice|^endmenu|\Z)", text, re.S | re.M
    )
    if block is None:
        return None
    default = re.search(r"^\s*default\s+(.+?)\s*$", block.group(1), re.M)
    return default.group(1) if default else None


def _kconfig_bool(symbol: str) -> bool | None:
    """The boolean ``default`` of ``config <symbol>``, or None."""
    value = _kconfig_text(symbol)
    if value is None:
        return None
    head = value.strip().split()[0]
    return head in ("y", "1", "true")


def _active_pods(hw: dict) -> list[dict]:
    """Pods with firmware today; `planned` pods are manifest-only and skipped."""
    return [pod for pod in hw.get("pods", []) if pod.get("status", "active") == "active"]


def check_i2c_pins(hw: dict, report: Report) -> None:
    """The shared I2C bus pins must match the firmware and the bringup doc.

    ESP32-S3 has no usable GPIO 21-25 on octal-PSRAM modules; the classic ESP32
    21/22 pair must never appear. The pins are firmware configuration
    (CONFIG_SYNAPSE_I2C_SDA_GPIO / _SCL_GPIO, read by every sensor driver through
    synapse_config), so the manifest is checked against the Kconfig default, and
    synapse_sensors.c must take the pins from the configuration, not hard-code them.
    """
    i2c = _pod(hw, "forearm_hub").get("metadata", {}).get("i2c", {})
    sda, scl = i2c.get("sda_gpio"), i2c.get("scl_gpio")

    src = SENSORS_C.read_text(encoding="utf-8") if SENSORS_C.is_file() else ""
    for role, pin in (("SDA", sda), ("SCL", scl)):
        default = _kconfig_default(f"SYNAPSE_I2C_{role}_GPIO")
        report.check(
            pin is not None and default == pin,
            f"firmware Kconfig SYNAPSE_I2C_{role}_GPIO defaults to {default} "
            f"(hardware.yaml declares {pin})",
        )
        report.check(
            f"{role.lower()}_gpio_num = hw_cfg->i2c_{role.lower()}_gpio" in src
            and not re.search(rf"{role.lower()}_gpio_num\s*=\s*\d+", src),
            f"firmware synapse_sensors.c must take {role} from hw_cfg->i2c_{role.lower()}_gpio, "
            "not hard-code a pin",
        )
        report.check(
            isinstance(pin, int) and not 21 <= pin <= 25,
            f"GPIO {pin} is in the reserved 21-25 octal-PSRAM range on ESP32-S3",
        )

    doc = BRINGUP_DOC.read_text(encoding="utf-8") if BRINGUP_DOC.is_file() else ""
    report.check(
        f"GPIO {sda} (SDA)" in doc and f"{scl} (SCL)" in doc,
        f"docs/hardware/BRINGUP_SOP.md does not document I2C SDA on GPIO {sda} and SCL on GPIO {scl}",
    )


def _imu_divider(base_hz: int, poll_hz: int, oversample: int) -> int:
    """Mirror of firmware/common/sensors/imu_odr.h: floor, never below 2x poll."""
    if poll_hz <= 0:
        poll_hz = 50
    n = base_hz // (poll_hz * oversample)
    return n - 1 if n > 0 else 0


def check_sensor_config(hw: dict, report: Report) -> None:  # noqa: PLR0915 - one check per field
    """Every field of an active pod must match the Kconfig default it feeds.

    The old checker verified two rates and two pins, yet hardware.yaml called
    itself the single source of truth. A drift in gain, LED current, address or
    FSR was invisible until the bench. Each pair below is one of those fields.
    A `planned` pod has no firmware and is checked only for internal honesty.
    """
    active = _active_pods(hw)
    report.check(bool(active), "at least one pod must have status: active")
    for pod in active:
        meta = pod.get("metadata", {})
        ecg = meta.get("ecg_config", {})
        ppg = meta.get("ppg_config", {})
        imu = meta.get("imu_config", {})
        temp = meta.get("temp_config", {})
        gps = meta.get("gps_config", {})
        bat = meta.get("battery_config", {})
        ina = meta.get("ina219_config", {})
        sync = meta.get("sync_config", {})
        pid = pod.get("pod_id", "?")

        def eq(yaml_value: object, symbol: str, label: str) -> None:
            default = _kconfig_default(symbol)
            report.check(
                default is not None and yaml_value == default,
                f"{pid}: {label}={yaml_value!r} but Kconfig {symbol} defaults to {default!r}",
            )

        eq(ecg.get("adc_channel"), "SYNAPSE_ECG_ADC_CHANNEL", "ecg_config.adc_channel")
        eq(ecg.get("gpio_lead_off"), "SYNAPSE_ECG_GPIO_LEAD_OFF", "ecg_config.gpio_lead_off")
        eq(
            round(float(ecg.get("gain", -1)) * 100) if ecg.get("gain") else None,
            "SYNAPSE_ECG_GAIN",
            "ecg_config.gain (x100)",
        )
        # On the S3 ADC1_CHn is GPIO n+1. The GPIO field is documentation, so
        # it must agree with the channel or the bench wiring table lies.
        channel, gpio = ecg.get("adc_channel"), ecg.get("adc_gpio")
        report.check(
            isinstance(channel, int) and isinstance(gpio, int) and gpio == channel + 1,
            f"{pid}: ADC1_CH{channel} is GPIO{channel + 1} on the S3, manifest says GPIO{gpio}",
        )

        eq(ppg.get("i2c_addr"), "SYNAPSE_PPG_I2C_ADDR", "ppg_config.i2c_addr")
        eq(ppg.get("gpio_int"), "SYNAPSE_PPG_GPIO_INT", "ppg_config.gpio_int")
        eq(ppg.get("led_current_red"), "SYNAPSE_PPG_LED_CURRENT_RED", "ppg_config.led_current_red")
        eq(ppg.get("led_current_ir"), "SYNAPSE_PPG_LED_CURRENT_IR", "ppg_config.led_current_ir")
        eq(ppg.get("sample_rate_reg"), "SYNAPSE_PPG_SAMPLE_RATE", "ppg_config.sample_rate_reg")
        eq(ppg.get("pulse_width_reg"), "SYNAPSE_PPG_PULSE_WIDTH", "ppg_config.pulse_width_reg")
        eq(ppg.get("adc_range_reg"), "SYNAPSE_PPG_ADC_RANGE", "ppg_config.adc_range_reg")
        report.check(
            ppg.get("fifo_average") == 1, f"{pid}: FIFO averaging changes the effective rate"
        )

        eq(imu.get("i2c_addr"), "SYNAPSE_IMU_I2C_ADDR", "imu_config.i2c_addr")
        allowed_addrs = [_as_int(addr) for addr in (imu.get("allowed_i2c_addrs") or [])]
        report.check(
            bool(allowed_addrs) and all(addr is not None for addr in allowed_addrs),
            f"{pid}: imu_config.allowed_i2c_addrs must be a non-empty list of addresses",
        )
        report.check(
            _as_int(imu.get("i2c_addr")) in allowed_addrs,
            f"{pid}: imu_config.i2c_addr is not declared in imu_config.allowed_i2c_addrs",
        )
        for addr in allowed_addrs:
            if addr is None:
                continue
            profile = IMU_ADDR_PROFILE_FILES.get(addr, "UNMAPPED")
            report.check(
                profile != "UNMAPPED",
                f"{pid}: no firmware build profile is mapped to IMU address {addr:#x}",
            )
            if profile is None:
                report.check(
                    _kconfig_default("SYNAPSE_IMU_I2C_ADDR") == addr,
                    f"{pid}: Kconfig default of SYNAPSE_IMU_I2C_ADDR must be {addr:#x}",
                )
            else:
                path = FIRMWARE_DIR / str(profile)
                text = path.read_text(encoding="utf-8") if path.is_file() else ""
                report.check(
                    re.search(rf"^CONFIG_SYNAPSE_IMU_I2C_ADDR={addr:#04x}$", text, re.MULTILINE)
                    is not None,
                    f"{pid}: {profile} must set CONFIG_SYNAPSE_IMU_I2C_ADDR={addr:#04x}",
                )
        eq(imu.get("gpio_int"), "SYNAPSE_IMU_GPIO_INT", "imu_config.gpio_int")
        eq(imu.get("accel_range_g"), "SYNAPSE_IMU_ACCEL_FSR_G", "imu_config.accel_range_g")
        eq(imu.get("gyro_range_dps"), "SYNAPSE_IMU_GYRO_FSR_DPS", "imu_config.gyro_range_dps")
        eq(imu.get("accel_poll_hz"), "SYNAPSE_IMU_ACCEL_ODR_HZ", "imu_config.accel_poll_hz")
        eq(imu.get("gyro_poll_hz"), "SYNAPSE_IMU_GYRO_ODR_HZ", "imu_config.gyro_poll_hz")
        oversample = imu.get("sensor_oversample")
        report.check(oversample == 2, f"{pid}: imu_config.sensor_oversample must be 2")
        if isinstance(oversample, int) and isinstance(imu.get("accel_poll_hz"), int):
            accel_div = _imu_divider(1125, imu["accel_poll_hz"], oversample)
            gyro_div = _imu_divider(1100, imu["gyro_poll_hz"], oversample)
            report.check(
                imu.get("accel_sensor_odr_hz") == 1125 // (accel_div + 1),
                f"{pid}: accel_sensor_odr_hz must match divider {accel_div} "
                f"(1125/(1+div)), declared {imu.get('accel_sensor_odr_hz')}",
            )
            report.check(
                imu.get("gyro_sensor_odr_hz") == 1100 // (gyro_div + 1),
                f"{pid}: gyro_sensor_odr_hz must match divider {gyro_div} "
                f"(1100/(1+div)), declared {imu.get('gyro_sensor_odr_hz')}",
            )

        eq(temp.get("i2c_addr"), "SYNAPSE_TEMP_I2C_ADDR", "temp_config.i2c_addr")
        eq(temp.get("gpio_alert"), "SYNAPSE_TEMP_GPIO_ALERT", "temp_config.gpio_alert")
        eq(gps.get("uart_port"), "SYNAPSE_GPS_UART_PORT", "gps_config.uart_port")
        eq(gps.get("tx_gpio"), "SYNAPSE_GPS_UART_TX_GPIO", "gps_config.tx_gpio")
        eq(gps.get("rx_gpio"), "SYNAPSE_GPS_UART_RX_GPIO", "gps_config.rx_gpio")
        eq(gps.get("baudrate"), "SYNAPSE_GPS_BAUDRATE", "gps_config.baudrate")
        report.check(
            gps.get("use_ubx") is _kconfig_bool("SYNAPSE_GPS_USE_UBX"),
            f"{pid}: gps_config.use_ubx differs from Kconfig SYNAPSE_GPS_USE_UBX",
        )
        eq(bat.get("adc_channel"), "SYNAPSE_BAT_ADC_CHANNEL", "battery_config.adc_channel")
        eq(
            round(float(bat.get("voltage_divider", -1)) * 100)
            if bat.get("voltage_divider")
            else None,
            "SYNAPSE_BAT_VOLTAGE_DIVIDER",
            "battery_config.voltage_divider (x100)",
        )
        eq(ina.get("i2c_addr"), "SYNAPSE_INA219_I2C_ADDR", "ina219_config.i2c_addr")
        eq(ina.get("shunt_mohm"), "SYNAPSE_INA219_SHUNT_MOHM", "ina219_config.shunt_mohm")
        eq(
            ina.get("max_current_ma"),
            "SYNAPSE_INA219_MAX_CURRENT_MA",
            "ina219_config.max_current_ma",
        )
        report.check(
            sync.get("wired_gpio") == hw.get("sync", {}).get("wired_sync_gpio"),
            f"{pid}: sync_config.wired_gpio={sync.get('wired_gpio')} differs from "
            f"hardware.yaml sync.wired_sync_gpio ({hw.get('sync', {}).get('wired_sync_gpio')})",
        )

        i2c_default_port = _kconfig_default("SYNAPSE_PPG_I2C_PORT")
        for name, value in (
            ("imu", imu.get("i2c_port")),
            ("temp", temp.get("i2c_port")),
            ("ina219", None),
        ):
            if value is None:
                continue
            report.check(
                value == i2c_default_port,
                f"{pid}: {name} must sit on the one I2C port (manifest {value}, PPG {i2c_default_port})",
            )
        report.check(bat.get("adc_gpio") == 4, f"{pid}: battery divider is GPIO4 on the S3")

    for pod in hw.get("pods", []):
        if pod.get("status") == "planned":
            report.check(
                bool(pod.get("blocked_on")),
                f"pod '{pod.get('pod_id')}' is planned but declares no blocked_on reason",
            )

    pb = hw.get("power_budget", {})
    for tier in ("T0", "T1", "T2"):
        system = pb.get("tier_profiles", {}).get(tier, {}).get("avg_mw")
        kit = pb.get("kit_tier_profiles", {}).get(tier, {}).get("avg_mw")
        eq(system, f"SYNAPSE_{tier}_AVG_MW", f"power_budget.tier_profiles.{tier}.avg_mw")
        eq(kit, f"SYNAPSE_{tier}_KIT_AVG_MW", f"power_budget.kit_tier_profiles.{tier}.avg_mw")
    report.check(
        pb.get("tier_profiles", {}).get("T0", {}).get("avg_mw") == 5.0
        and pb.get("kit_tier_profiles", {}).get("T0", {}).get("avg_mw") == 425.0,
        "the system target and the kit envelope must stay two separate numbers",
    )


def check_research_service_uuids(hw: dict, report: Report) -> None:
    """The research service must exist in the firmware, separate from the consumer one.

    The research service is the dataset path and must never reuse the UUIDs of
    the consumer feature stream, or a researcher's client would silently
    subscribe to the wrong service.
    """
    meta = _pod(hw, "forearm_hub").get("metadata", {})
    service = meta.get("research_service_uuid")
    chars = meta.get("research_characteristics", {})
    feature = hw.get("ble", {}).get("feature_stream", {}).get("service_uuid")

    report.check(
        service is not None and bool(chars),
        "hardware.yaml must declare the research service and its characteristics",
    )
    report.check(
        service != feature,
        "the research service and the consumer feature stream must use different UUIDs",
    )

    # Both BLE sources are searched: the research service lives in its own
    # translation unit, separate from the consumer feature-stream server, and
    # the UUID bytes themselves come from the generated contract header.
    ble_sources = ""
    for path in (GATT_H, RESEARCH_C, CONTRACT_H):
        if path.is_file():
            ble_sources += path.read_text(encoding="utf-8") + "\n"
    for name, uuid in chars.items():
        report.check(
            uuid is not None and _uuid_bytes_in_source(ble_sources, uuid),
            f"the firmware BLE sources do not declare research characteristic '{name}' ({uuid})",
        )


def _uuid_bytes_in_source(source: str, uuid_str: str) -> bool:
    """True if the source contains the UUID as a little-endian byte array.

    NimBLE stores 128-bit UUIDs as the full reversal of the canonical
    big-endian form: UUID A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C5D appears as
    BLE_UUID128_INIT(0x5D, 0x4C, ..., 0xB2, 0xA1). This mirrors the existing
    convention in firmware/esp32_band/ble/ble_gatt_server.h.
    """
    parts = [int(p, 16) for p in uuid_str.split("-")]
    if len(parts) != 5:
        return False
    be = parts[0].to_bytes(4, "big") + parts[1].to_bytes(2, "big") + parts[2].to_bytes(2, "big")
    be += parts[3].to_bytes(2, "big") + parts[4].to_bytes(6, "big")
    hex_le = ", ".join(f"0x{b:02X}" for b in reversed(be))
    # The firmware wraps BLE_UUID128_INIT across two lines, sometimes with a
    # trailing backslash (header macros). Normalise whitespace and drop
    # line-continuation backslashes before matching.
    cleaned = source.replace("\\", "")
    flattened = re.sub(r",\s+", ", ", cleaned)
    return hex_le in flattened


def check_raw_frame_contract(hw: dict, report: Report) -> None:
    """raw_frame.* must mirror the codec constants everywhere they are declared.

    The contract reads the SDK's canonical codec (decision Q7); the repo's
    ``raw_codec.py`` shim re-exports it, so a drift between the two is caught
    here against the one implementation that actually writes the bytes.
    """
    raw = hw.get("raw_frame", {})
    codec = RAW_CODEC_PY.read_text(encoding="utf-8") if RAW_CODEC_PY.is_file() else ""
    if not codec:
        report.check(False, f"the canonical codec is missing: {RAW_CODEC_PY}")
        return

    report.check(
        f"MAX_PAYLOAD = {raw.get('max_payload_bytes')}" in codec,
        f"codec.MAX_PAYLOAD does not match raw_frame.max_payload_bytes="
        f"{raw.get('max_payload_bytes')}",
    )
    report.check(
        f"HEADER_SIZE = {raw.get('header_bytes')}" in codec,
        f"codec.HEADER_SIZE does not match raw_frame.header_bytes={raw.get('header_bytes')}",
    )

    for name, value in raw.get("stream_ids", {}).items():
        expected = f"{name.upper()} = 0X{int(value):02X}"
        report.check(
            expected in codec.upper(),
            f"the canonical codec does not define RawStream.{name.upper()} = 0x{int(value):02X}",
        )

    for name, value in raw.get("sample_formats", {}).items():
        expected = f"{name.upper()} = 0X{int(value):02X}"
        report.check(
            expected in codec.upper(),
            f"the canonical codec does not define RawFormat.{name.upper()} = 0x{int(value):02X}",
        )

    for name, value in raw.get("flag_bits", {}).items():
        flag_mask = 1 << int(value)  # the manifest declares a bit index, the codec a mask
        expected = f"FLAG_{name.upper()} = 0X{flag_mask:02X}"
        report.check(
            expected in codec.upper(),
            f"the canonical codec does not define FLAG_{name.upper()} = 0x{int(value):02X}",
        )


def check_consumer_service_contract(hw: dict, report: Report) -> None:
    """The consumer feature stream (Averyn integration) must be coherent.

    hardware.yaml declares the service/characteristic UUIDs, payload size and
    rate. The firmware must declare the same UUIDs (little-endian byte arrays)
    and a struct large enough to carry the payload. The deprecated 6-group UUID
    string from the strategy docs must not appear anywhere.
    """
    ble = hw.get("ble", {})
    fs = ble.get("feature_stream", {})
    service = fs.get("service_uuid")
    char = fs.get("characteristic_uuid")
    payload = fs.get("payload_bytes")
    rate = fs.get("rate_hz")
    rate_max = fs.get("rate_max_hz")

    report.check(
        service is not None and char is not None,
        "hardware.yaml ble.feature_stream must declare service_uuid and characteristic_uuid",
    )
    ble_sources = ""
    for path in (GATT_H, RESEARCH_C, CONTRACT_H):
        if path.is_file():
            ble_sources += path.read_text(encoding="utf-8") + "\n"
    report.check(
        service is not None and _uuid_bytes_in_source(ble_sources, service),
        f"firmware BLE sources do not declare consumer service UUID {service}",
    )
    report.check(
        char is not None and _uuid_bytes_in_source(ble_sources, char),
        f"firmware BLE sources do not declare consumer characteristic UUID {char}",
    )
    # The UUID bytes must live in the generated header only: a hardcoded copy
    # in a translation unit is exactly how the contract drifted before.
    for path in (GATT_H, RESEARCH_C):
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        hardcoded = [
            uuid for uuid in (service, char) if uuid and _uuid_bytes_in_source(source, uuid)
        ]
        report.check(
            not hardcoded,
            f"{path.name} hardcodes the UUID bytes {hardcoded}: use the macros of "
            "synapse_contract_generated.h (tools/generate_contracts.py)",
        )
    report.check(
        service != _pod(hw, "forearm_hub").get("metadata", {}).get("research_service_uuid"),
        "consumer feature-stream service and research service must not share a UUID",
    )
    report.check(
        isinstance(payload, int) and payload >= 20,
        f"consumer payload_bytes must be an int >= 20, got {payload!r}",
    )
    report.check(
        isinstance(rate, int) and isinstance(rate_max, int) and 0 < rate <= rate_max <= 10,
        f"consumer rate must be 1..rate_max<=10 Hz, got rate={rate!r} max={rate_max!r}",
    )


# The strategy input's malformed 6-group UUID ("SYNAPSE-BAND", ASCII) —
# written as e.g. 53594E41 followed by dash-separated hex groups — is not a
# valid UUID and UUID.fromString/Java throws on it. It must never reappear in
# a source that builds or parses a BLE filter; the contract's UUIDs come from
# config/hardware.yaml (via tools/generate_contracts.py) only.
_STALE_UUID_PREFIX = "53594E41"
_STALE_UUID_RE = re.compile(rf"\b{_STALE_UUID_PREFIX}(?:-[0-9A-Fa-f]{{2,12}}){{2,}}\b")
# The tree-wide scan skips archives and build/cache material. The strategy
# directory is exempt because it archives the original external input for
# cross-reference; re-entry anywhere that ships code is a violation.
_STALE_SCAN_SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", "SYNAPSE-AVERYN-STRATEGY"}
_STALE_SCAN_SKIP_HIDDEN = (".",)
_STALE_SCAN_SUFFIXES = {
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".cfg",
    ".cmake",
    ".conf",
    ".gradle",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".kt",
    ".kconfig",
    ".md",
    ".py",
    ".pyi",
    ".sh",
    ".toml",
    ".ts",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


def check_stale_uuid_gone_from_sources(scan_root: Path, report: Report) -> None:
    """Scan every text source under ``scan_root`` for the malformed UUID.

    The old check read a fixed list of four files, which is why the prefix
    could come back through a fifth. The scan now walks the whole tree. The
    literal ``0x53594E41 / 0x53594E41u`` application version ("SYNA") is not a
    UUID: only dash-separated multi-group forms match.
    """
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(scan_root):
        dirnames[:] = [
            name
            for name in dirnames
            if not name.startswith(_STALE_SCAN_SKIP_HIDDEN) and name not in _STALE_SCAN_SKIP_DIRS
        ]
        folder = Path(dirpath)
        for name in filenames:
            path = folder / name
            if path.suffix.lower() not in _STALE_SCAN_SUFFIXES:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if _STALE_UUID_RE.search(text):
                try:
                    hits.append(str(path.relative_to(ROOT)))
                except ValueError:
                    hits.append(str(path))
    report.check(
        not hits,
        f"deprecated 6-group UUID prefix {_STALE_UUID_PREFIX}-... still present in: "
        f"{', '.join(hits)} (the contract's UUIDs come from config/hardware.yaml; "
        "see tools/generate_contracts.py)",
    )


# Not a field name: the contract-id -> wire definition-number mapping macro
# that fit_writer.h derives from the ids (it is a function of the contract,
# never a third spelling of a field).
_DERIVED_FIELD_MACROS = {"SYNAPSE_DEV_FIELD_DEFINITION_NUM"}


def check_fit_developer_names(
    report: Report,
    contract: Path | None = None,
    writer: Path | None = None,
    sources: list[Path] | tuple[Path, ...] | None = None,
) -> None:
    """FIT ``SYNAPSE_DEV_FIELD_*`` literals must match the contract, both ways.

    ``contracts/fit_developer_fields.json`` is the single source of truth for
    the developer fields' ids and names (field IDs are immutable once a
    consumer imports files). Every literal in a FIT-relevant source must be
    one of the contract's ``c_name`` values, and every canonical name must be
    present in ``fit_writer.h`` because that header is the wiring both
    exporters mirror. A check this strict is the point: the field ids and
    names cannot be renegotiated by writing a third copy in code.
    """
    contract = contract if contract is not None else FIT_CONTRACT
    writer = writer if writer is not None else FIT_WRITER_H
    sources = sources if sources is not None else FIT_SOURCES

    try:
        descriptor = json.loads(contract.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        report.check(False, f"cannot read {contract} ({FIT_CONTRACT.name}): {exc}")
        return
    if not isinstance(descriptor, dict):
        report.check(False, f"{FIT_CONTRACT.name} must be a JSON object with a 'fields' list")
        return
    fields = descriptor.get("fields", [])
    canonical = {
        field_desc.get("c_name")
        for field_desc in fields
        if isinstance(field_desc, dict) and field_desc.get("c_name")
    }
    report.check(bool(canonical), f"{FIT_CONTRACT.name} declares no c_name values")
    declared_count = descriptor.get("num_fields")
    report.check(
        canonical and len(canonical) == declared_count,
        f"{FIT_CONTRACT.name} declares num_fields={declared_count!r} but "
        f"{len(canonical)} c_name values",
    )

    checked_paths: list[Path] = []
    for entry in sources:
        source_dir_or_file_path = Path(entry)
        if source_dir_or_file_path.is_dir():
            checked_paths.extend(
                path
                for path in sorted(source_dir_or_file_path.rglob("*"))
                if path.is_file() and path.suffix.lower() in _FIT_SOURCE_SUFFIXES
            )
        elif source_dir_or_file_path.is_file():
            checked_paths.append(source_dir_or_file_path)
        else:
            report.check(False, f"FIT source is missing: {source_dir_or_file_path}")

    for path in checked_paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.check(False, f"cannot read FIT source {path}: {exc}")
            continue
        used = set(re.findall(r"SYNAPSE_DEV_FIELD_\w+", text))
        unknown = sorted(used - canonical - _DERIVED_FIELD_MACROS)
        report.check(
            not unknown,
            f"{path.name} uses FIT fields not in {FIT_CONTRACT.name}: {', '.join(unknown)} "
            "(add them to the contract, named exactly once)",
        )

    if not writer.is_file():
        report.check(False, f"the FIT writer header is missing: {writer}")
        return
    header = writer.read_text(encoding="utf-8")
    missing = sorted(name for name in canonical if f"#define {name}" not in header)
    report.check(
        not missing,
        f"{writer.name} does not define: {', '.join(missing)} "
        f"(the contract {FIT_CONTRACT.name} and the header are one table)",
    )


def check_canonical_firmware_paths(report: Report) -> None:
    """Build tooling must target esp32_band, the canonical firmware."""
    script = BUILD_SCRIPT.read_text(encoding="utf-8") if BUILD_SCRIPT.is_file() else ""
    report.check(
        "esp32_band" in script,
        "scripts/build_and_flash_firmware.py must build firmware/esp32_band",
    )
    report.check(
        "firmware/esp32_tier0" not in script,
        "scripts/build_and_flash_firmware.py still targets the archived esp32_tier0",
    )
    # The build metadata must be derived from the manifest, not a second copy
    # that can drift while check_contract stays green.
    report.check(
        "hardware.yaml" in script and "yaml.safe_load" in script,
        "build metadata must be generated from config/hardware.yaml",
    )
    report.check(
        '"ecg_hz": 500' not in script and '"sda_gpio": 8' not in script,
        "scripts/build_and_flash_firmware.py hardcodes rates or I2C pins: derive them from hardware.yaml",
    )


def _defaults_value(path: Path, symbol: str) -> int | None:
    """The integer value of ``symbol`` in an sdkconfig.defaults file."""
    if not path.is_file():
        return None
    match = re.search(rf"^{symbol}=(\d+)\s*$", path.read_text(encoding="utf-8"), re.M)
    return int(match.group(1)) if match else None


def check_generated_hardware_values(hw: dict, report: Report) -> None:
    """The generated C constants must be the ones the firmware actually uses.

    Wired sync GPIO and the INA219 front-end used to be typed a second time in
    a firmware header and in the bringup YAML. They are now generated from the
    manifest; this check makes sure no stale copy creeps back and that the
    generated macros really are consumed by the drivers.
    """
    gpio = hw.get("sync", {}).get("wired_sync_gpio")
    header = CONTRACT_H.read_text(encoding="utf-8") if CONTRACT_H.is_file() else ""
    report.check(
        gpio is not None and f"#define SYNAPSE_CONTRACT_WIRED_SYNC_GPIO {gpio}" in header,
        f"the generated contract header does not carry sync.wired_sync_gpio={gpio}",
    )
    clock_sync = ROOT / "src" / "synapse24" / "acquisition" / "clock_sync.py"
    host_source = clock_sync.read_text(encoding="utf-8") if clock_sync.is_file() else ""
    report.check(
        "wired_sync_gpio: int = WIRED_SYNC_GPIO" in host_source
        and "from synapse24.contracts_generated import WIRED_SYNC_GPIO" in host_source,
        "clock_sync.py must take wired_sync_gpio from the generated contract, not a literal",
    )
    sync_h = ROOT / "firmware" / "common" / "sync" / "wired_sync_handler.h"
    sync_src = sync_h.read_text(encoding="utf-8") if sync_h.is_file() else ""
    report.check(
        "#define SYNAPSE_WIRED_SYNC_GPIO           SYNAPSE_CONTRACT_WIRED_SYNC_GPIO" in sync_src
        and not re.search(r"#define SYNAPSE_WIRED_SYNC_GPIO\s+\d+", sync_src),
        "wired_sync_handler.h must use SYNAPSE_CONTRACT_WIRED_SYNC_GPIO, not a literal pin",
    )

    ina = _pod(hw, "forearm_hub").get("metadata", {}).get("ina219_config", {})
    for key, macro in (
        ("i2c_addr", "SYNAPSE_CONTRACT_INA219_I2C_ADDR"),
        ("shunt_mohm", "SYNAPSE_CONTRACT_INA219_SHUNT_MOHM"),
        ("max_current_ma", "SYNAPSE_CONTRACT_INA219_MAX_CURRENT_MA"),
    ):
        value = ina.get(key)
        rendered = f"0x{int(value):02X}" if key == "i2c_addr" else str(value)
        report.check(
            f"#define {macro} {rendered}" in header,
            f"the generated contract header does not carry ina219_config.{key}={rendered}",
        )
    power_c = FIRMWARE_DIR / "power" / "synapse_power.c"
    power_src = power_c.read_text(encoding="utf-8") if power_c.is_file() else ""
    report.check(
        all(
            f"SYNAPSE_CONTRACT_{name}" in power_src
            for name in (
                "INA219_I2C_ADDR",
                "INA219_SHUNT_MOHM",
                "INA219_MAX_CURRENT_MA",
            )
        ),
        "synapse_power.c must initialise the INA219 from the generated contract macros",
    )


def check_config_readback(report: Report) -> None:
    """The flashed-configuration proof must exist on both sides of the link."""
    server = FIRMWARE_DIR / "ble" / "ble_gatt_server.c"
    src = server.read_text(encoding="utf-8") if server.is_file() else ""
    # One authoritative key set: every field the host gate compares must be
    # published by the firmware. A field dropped from either side used to make
    # CI green and G1 fail on the bench.
    keys = (
        "ecg_hz",
        "ppg_hz",
        "imu_hz",
        "ecg_gain_x100",
        "ppg_led_red",
        "ppg_led_ir",
        "battery_mah",
        "feature_rate_hz",
        "pending_reboot",
        "accel_bias_mg",
        "gyro_bias_mdps",
        "ecg_adc_ch",
        "ecg_lead_off",
        "ppg_addr",
        "ppg_sr",
        "ppg_pw",
        "ppg_adc",
        "imu_addr",
        "accel_g",
        "gyro_dps",
        "accel_hz",
        "gyro_hz",
        "i2c_port",
        "i2c_sda",
        "i2c_scl",
        "i2c_hz",
        "sync_gpio",
        "ina_addr",
        "ina_shunt",
        "power_profile",
        "cal_serial",
        "cal_state",
        "cal_date",
        "cal_operator",
        "cal_instrument",
        "cal_fw_version",
        "cal_uncertainty_pct_x100",
        "cal_noise_std_uv_x10",
    )
    report.check(
        "build_config_readback" in src and all(f'\\"{key}\\"' in src for key in keys),
        "the device-config characteristic must publish every field the host gate checks",
    )
    report.check(
        "synapse_calibration_load" in src,
        "the config readback must include the NVS calibration record, not only Kconfig",
    )
    validator = ROOT / "scripts" / "validate_phase1_entry.py"
    host = validator.read_text(encoding="utf-8") if validator.is_file() else ""
    report.check(
        'DEVICE_CONFIG_UUID = CONTRACT_UUIDS["CONSUMER_DEVICE_CONFIG"]' in host,
        "the host validator must read the device-config UUID from the generated contract",
    )
    # feature_rate_hz is session-only and i2c_hz is reported but not a contract
    # field; every other published key must be expected by the host gate.
    host_keys = tuple(key for key in keys if key not in ("feature_rate_hz", "i2c_hz"))
    report.check(
        all(f'"{key}"' in host for key in host_keys),
        "the host validator must expect every readback key the firmware publishes",
    )


def check_hub_battery_capacity(
    bringup: dict,
    report: Report,
    defaults_path: Path | None = None,
) -> None:
    """The G1 bringup cell capacity must match the band-core firmware value.

    The firmware divides the charge it consumed by
    ``CONFIG_SYNAPSE_HUB_BATTERY_MAH`` from ``sdkconfig.defaults.core``, while
    the host reads ``power_budget.hub_battery_mah`` from
    ``hardware_bringup.yaml``. A disagreement makes ``measured_battery_pct``
    wrong by the ratio, so a soak can end with a battery figure that is not the
    cell that was actually fitted. The optional path exists only to exercise a
    deliberate mismatch in a unit test; the CLI always uses the committed
    defaults file (or the Kconfig default if it is absent).
    """
    path = defaults_path if defaults_path is not None else FIRMWARE_DIR / "sdkconfig.defaults.core"
    firmware_capacity = _defaults_value(path, "CONFIG_SYNAPSE_HUB_BATTERY_MAH")
    if firmware_capacity is None:
        firmware_capacity = _kconfig_default("SYNAPSE_HUB_BATTERY_MAH")
    live_capacity = bringup.get("bringup", {}).get("power_budget", {}).get("hub_battery_mah")
    report.check(
        live_capacity is not None
        and firmware_capacity is not None
        and live_capacity == firmware_capacity,
        f"CONFIG_SYNAPSE_HUB_BATTERY_MAH in {path.name} ({firmware_capacity}) must equal "
        f"hardware_bringup.yaml bringup.power_budget.hub_battery_mah ({live_capacity}) "
        "and the fitted soak cell",
    )


def check_bringup_truth(hw: dict, bringup: dict, report: Report) -> None:
    """The live-run knobs must agree with the firmware and carry no dead copies."""
    bring = bringup.get("bringup", {})
    power = hw.get("power_budget", {})
    report.check(
        power.get("hub_battery_mah") == _kconfig_default("SYNAPSE_HUB_BATTERY_MAH"),
        "hardware.yaml power_budget.hub_battery_mah disagrees with the firmware Kconfig default "
        f"({power.get('hub_battery_mah')} vs {_kconfig_default('SYNAPSE_HUB_BATTERY_MAH')})",
    )
    check_hub_battery_capacity(bringup, report)
    report.check(
        "power_monitor" not in bring,
        "hardware_bringup.yaml still carries a dead power_monitor block; "
        "power_budget.hub_battery_mah is the live value",
    )

    streams = bring.get("lsl", {}).get("streams", [])
    report.check(
        bool(streams) and all("{serial}" in s.get("source_id", "") for s in streams),
        "every bringup LSL source_id must carry a {serial} template: two bands must not "
        "produce indistinguishable recordings",
    )
    validation = bring.get("validation", {})
    required = validation.get("required_gates", {})
    live_required = set(required.get("live") or [])
    report.check(
        {
            "hr_accuracy",
            "rmssd_accuracy",
            "ppg_sqi",
            "ppg_map",
            "power_telemetry_measured",
        }.issubset(live_required),
        "the G1 live gate set must require the cardiac measurements and a measured POWER stream",
    )


# Files that previously carried stale 64/100 Hz literals. Each is checked for
# both the literal and for the presence of the manifest-derived replacement,
# so the drift cannot silently return.
_RATE_LITERAL_TARGETS = [
    (
        ROOT / "src" / "synapse24" / "hardware" / "esp32_tier0.py",
        "host Tier 0 adapter",
        "forearm_hub_normative_rates",
    ),
    (
        ROOT / "src" / "synapse24" / "acquisition" / "live_lsl_sync.py",
        "live 2-pod LSL sync gate",
        "forearm_hub_normative_rates",
    ),
    (
        ROOT / "src" / "synapse24" / "acquisition" / "tier1_coordinator.py",
        "tier 1 pod coordinator",
        "forearm_hub_normative_rates",
    ),
    (
        ROOT / "scripts" / "validate_phase1_entry.py",
        "Phase 1 validator",
        "_nominal_rates",
    ),
    (
        ROOT / "scripts" / "validate_live_sync.py",
        "live sync validation script",
        "forearm_hub_normative_rates",
    ),
    (
        ROOT / "tests" / "test_tier0_live_loop.py",
        "Tier 0 live-loopback test",
        "forearm_hub_normative_rates",
    ),
    (
        ROOT / "tests" / "test_lsl_live_sync.py",
        "live 2-pod sync test",
        "forearm_hub_normative_rates",
    ),
    (
        ROOT / "tests" / "test_phase1_dry_run.py",
        "Phase 1 dry-run round-trip test",
        "forearm_hub_normative_rates",
    ),
]

_FORBIDDEN_RATE_LITERALS = [
    # A rate-valued field assigned a legacy literal, with or without a type
    # annotation: ppg_sampling_rate: int = 64, imu_fs=100, accel_sampling_rate=100.
    (
        r"(?:rate|fs)\w*\s*(?::\s*[A-Za-z_\[\]| ]+)?\s*=\s*(?:64|100)\b",
        "legacy rate literal",
    ),
    # A sampling_rate structure still carrying a legacy value: {"ppg": 64, ...}.
    # A percentage (== 100.0) is not a rate and must not match this.
    ("__SAMPLING_RATE_DICT__", "legacy rate literal in a sampling_rate structure"),
    (r"int\(64\s*\*", "hardcoded 64 Hz PPG expectation"),
    (r"abs\([a-z_]+_rate\s*-\s*\d+\)", "hardcoded nominal rate in a gate check"),
    # YAML/JSON-style stream specs: "sampling_rate": 64.0 (test_phase1_dry_run.py).
    (r"[\"']sampling_rate[\"']\s*:\s*64(?:\.0+)?\b", "legacy 64 Hz stream spec"),
]

# A line carrying this marker is exempt from the literal check. It exists for
# values the manifest genuinely does not declare (e.g. the Cerelog head-pod
# IMU rate), where an explicit parameter is the honest representation — the
# alternative is either a hidden literal or an invented manifest entry.
_CONTRACT_EXEMPT = "contract-exempt"


def _legacy_rate_reason(line: str) -> str | None:
    """Why this line carries a stale rate literal, or None if it does not.

    Percentages (``== 100.0``) and battery states are deliberately not rate
    literals: matching them would train the reader to ignore the checker.
    """
    if _CONTRACT_EXEMPT in line:
        return None
    if "sampling_rate" in line and re.search(r"\b(?:64|100)\b", line):
        return "legacy rate literal in a sampling_rate structure"
    for pattern, description in _FORBIDDEN_RATE_LITERALS:
        if pattern == "__SAMPLING_RATE_DICT__":
            continue
        if re.search(pattern, line):
            return description
    return None


def check_no_legacy_rate_literals(report: Report) -> None:
    """The stale 64/100 Hz rates must not reappear in host code or tests.

    hardware.yaml declares PPG 50 Hz / IMU 50 Hz. Those literals used to live
    in the Tier 0 adapter, the synthetic helper, the live-loopback test, the
    pod coordinator and the validator's own gate thresholds, so each of them
    asserted the wrong rates — and the validator would have failed a
    *conforming* pod on the bench. Every checked file must derive the rate
    from the manifest, except lines explicitly marked ``contract-exempt``.
    """
    for path, label, derivation_marker in _RATE_LITERAL_TARGETS:
        if not path.is_file():
            report.check(False, f"{label} is missing: {path}")
            continue
        text = path.read_text(encoding="utf-8")
        report.check(
            derivation_marker in text,
            f"{label} ({path.name}) must derive rates from the manifest via '{derivation_marker}'",
        )
        for pattern, description in _FORBIDDEN_RATE_LITERALS:
            hits = [
                f"{path.name}:{lineno}"
                for lineno, line in enumerate(text.splitlines(), 1)
                if _legacy_rate_reason(line) == description
            ]
            report.check(
                not hits,
                f"{description} found in {label} ({', '.join(hits)}); "
                f"rates come from config/hardware.yaml only",
            )
        # The dict-form check reports through the sampling_rate branch above;
        # surface any remaining sampling_rate lines carrying a stale value.
        dict_hits = [
            f"{path.name}:{lineno}"
            for lineno, line in enumerate(text.splitlines(), 1)
            if "sampling_rate" in line and re.search(r"\b(?:64|100)\b", line)
        ]
        report.check(
            not dict_hits,
            f"legacy rate literal in a sampling_rate structure found in {label} "
            f"({', '.join(dict_hits)}); rates come from config/hardware.yaml only",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--hardware-yaml",
        type=Path,
        default=HARDWARE_YAML,
        help="Path to the hardware manifest to check (default: config/hardware.yaml)",
    )
    parser.add_argument(
        "--bringup-yaml",
        type=Path,
        default=BRINGUP_YAML,
        help="Path to the bringup overrides (default: config/hardware_bringup.yaml)",
    )
    parser.add_argument(
        "--fit-contract",
        type=Path,
        default=None,
        help="Path to the FIT developer-fields contract (default: contracts/fit_developer_fields.json)",
    )
    parser.add_argument(
        "--fit-writer",
        type=Path,
        default=None,
        help="Path to the firmware FIT writer header (default: firmware/esp32_band/fit/fit_writer.h)",
    )
    parser.add_argument(
        "--fit-sources",
        type=Path,
        action="append",
        default=None,
        help="Files/directories holding SYNAPSE_DEV_FIELD_* literals; repeatable",
    )
    parser.add_argument(
        "--stale-scan-root",
        type=Path,
        default=None,
        help="Root of the tree scanned for the stale 6-group UUID (default: repo root)",
    )
    args = parser.parse_args()

    hw = _load(args.hardware_yaml)
    bringup = _load(args.bringup_yaml)
    report = Report()

    check_sampling_rates(hw, bringup, report)
    check_sensor_config(hw, report)
    check_i2c_pins(hw, report)
    check_research_service_uuids(hw, report)
    check_consumer_service_contract(hw, report)
    check_raw_frame_contract(hw, report)
    check_canonical_firmware_paths(report)
    check_generated_hardware_values(hw, report)
    check_config_readback(report)
    check_bringup_truth(hw, bringup, report)
    check_no_legacy_rate_literals(report)
    check_stale_uuid_gone_from_sources(args.stale_scan_root or ROOT, report)
    check_fit_developer_names(
        report,
        contract=args.fit_contract,
        writer=args.fit_writer,
        sources=args.fit_sources if args.fit_sources else None,
    )

    if args.verbose:
        print(f"ran {report.checks_run} contract checks")

    if report.failures:
        print(f"CONTRACT VIOLATIONS ({len(report.failures)}):")
        for failure in report.failures:
            print(f"  - {failure}")
        print()
        print("config/hardware.yaml is the single source of truth. Fix the")
        print("divergent source, or update hardware.yaml together with all")
        print("consumers, so the contract never drifts again.")
        return 1

    print(f"CONTRACT OK: {report.checks_run} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
