"""MVP-2.3: the calibration record belongs to exactly one band.

The USB factory shell used to start from an empty record and never copied the
device serial into it, so ``factory_calibrate.py --dump`` failed on a virgin
band and a record from another unit could be applied silently. These static
guards pin the three changes that close that path.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BAND = ROOT / "firmware" / "esp32_band"
CAL = (BAND / "config" / "synapse_calibration.c").read_text(encoding="utf-8")
CLI = (BAND / "main" / "synapse_factory_cli.c").read_text(encoding="utf-8")
APP = (BAND / "main" / "app_main.c").read_text(encoding="utf-8")


def test_load_refuses_a_record_for_another_serial() -> None:
    assert "synapse_identity.h" in CAL
    assert "synapse_identity_serial()" in CAL
    assert "calibration serial mismatch" in CAL
    assert "ESP_ERR_INVALID_STATE" in CAL


def test_factory_shell_owns_the_serial_from_identity() -> None:
    assert "synapse_identity.h" in CLI
    assert "snprintf(s_cal.serial" in CLI
    assert 'strcmp(cmd, "serial")' not in CLI
    assert "cal serial <" not in CLI


def test_identity_is_initialised_before_the_factory_shell() -> None:
    identity = APP.index("synapse_identity_init(serial)")
    shell = APP.index("synapse_factory_cli_start()")
    assert identity < shell
