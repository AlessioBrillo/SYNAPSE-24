"""The Woodpecker `firmware-band` step must build three core-derived firmwares.

The audit found the second build was a copy of the first: the variable was spelled
``SDKCONFIG_DEFAULT`` (ESP-IDF reads ``SDKCONFIG_DEFAULTS``), and ``idf.py fullclean`` leaves
``sdkconfig`` in place, which makes ESP-IDF ignore the defaults files altogether. The gate called a
"band-core" green that had never been configured.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".woodpecker" / "ci.yml"


def _commands() -> list[str]:
    doc = yaml.safe_load(CI.read_text(encoding="utf-8"))
    step = next(s for s in doc["steps"] if s["name"] == "firmware-band")
    return [str(c) for c in step["commands"]]


def test_the_core_build_uses_the_variable_esp_idf_reads() -> None:
    commands = _commands()
    core = [c for c in commands if "sdkconfig.defaults.core" in c and "idf.py build" in c]
    assert len(core) == 3, "core, bench (core + factory shell) and bench-ad0 (IMU at 0x69)"
    assert any(
        c.strip().startswith('SDKCONFIG_DEFAULTS="sdkconfig.defaults.core;sdkconfig.defaults"')
        for c in core
    )
    assert any("sdkconfig.defaults.bench" in c for c in core)
    assert any("sdkconfig.defaults.ad0" in c for c in core)
    assert not any("SDKCONFIG_DEFAULT=" in c for c in commands), "the misspelt variable is back"


def test_ad0_variant_is_flashable() -> None:
    from scripts.build_and_flash_firmware import FirmwareBuilder

    defaults = FirmwareBuilder.VARIANT_DEFAULTS["band-bench-ad0"]
    assert "sdkconfig.defaults.core" in defaults
    assert "sdkconfig.defaults.bench" in defaults
    assert "sdkconfig.defaults.ad0" in defaults


def test_ad0_build_proves_the_address_and_the_variant() -> None:
    commands = _commands()
    ad0 = next(c for c in commands if "sdkconfig.defaults.ad0" in c)
    assert "idf.py build" in ad0
    assert any("CONFIG_SYNAPSE_IMU_I2C_ADDR=0x69" in c for c in commands)
    assert any("band-bench-ad0" in c for c in commands if "CONFIG_SYNAPSE_BUILD_VARIANT" in c)
    assert any("CONFIG_SYNAPSE_FACTORY_MODE=y" in c for c in commands)


def test_sdkconfig_is_removed_before_the_core_build() -> None:
    commands = _commands()
    core_at = next(i for i, c in enumerate(commands) if "sdkconfig.defaults.core" in c)
    full_at = next(
        i for i, c in enumerate(commands) if c.strip() == "idf.py build" or "band-full" in c
    )
    removal = [i for i, c in enumerate(commands) if c.strip() == "rm -f sdkconfig"]
    assert removal, (
        "without it the defaults files are ignored and the core build is band-full again"
    )
    assert full_at < removal[0] < core_at


def test_the_core_build_proves_it_is_the_core_profile() -> None:
    commands = _commands()
    core_at = next(i for i, c in enumerate(commands) if "sdkconfig.defaults.core" in c)
    after = commands[core_at + 1 :]
    assert any("CONFIG_SYNAPSE_FEATURE_GPS is not set" in c for c in after)
    assert any("CONFIG_SYNAPSE_FEATURE_FIT_STORAGE is not set" in c for c in after)


def test_the_core_profile_really_turns_those_features_off() -> None:
    core = (ROOT / "firmware" / "esp32_band" / "sdkconfig.defaults.core").read_text(
        encoding="utf-8"
    )
    assert "CONFIG_SYNAPSE_FEATURE_GPS=n" in core
    assert "CONFIG_SYNAPSE_FEATURE_FIT_STORAGE=n" in core


def test_the_product_build_disables_the_raw_path() -> None:
    commands = _commands()
    product = [c for c in commands if "sdkconfig.defaults.product" in c and "idf.py build" in c]
    assert len(product) == 1
    assert any("CONFIG_SYNAPSE_PRODUCT_MODE=y" in c for c in commands)
    assert any("CONFIG_SYNAPSE_FACTORY_MODE is not set" in c for c in commands)
    profile = (ROOT / "firmware" / "esp32_band" / "sdkconfig.defaults.product").read_text(
        encoding="utf-8"
    )
    assert "CONFIG_SYNAPSE_PRODUCT_MODE=y" in profile
    assert "CONFIG_SYNAPSE_FACTORY_MODE=n" in profile


def test_the_flagged_rr_code_is_built_too() -> None:
    """CONFIG_SYNAPSE_RR_ENABLE is off in the product: without a build with it on, it could rot."""
    commands = _commands()
    rr = [c for c in commands if "sdkconfig.rr" in c and "idf.py build" in c]
    assert len(rr) == 1
    assert "SDKCONFIG_DEFAULTS=" in rr[0]
    assert any("CONFIG_SYNAPSE_RR_ENABLE=y" in c for c in commands)
    flags = (ROOT / "firmware" / "esp32_band" / "sdkconfig.rr").read_text(encoding="utf-8")
    assert "CONFIG_SYNAPSE_RR_ENABLE=y" in flags
