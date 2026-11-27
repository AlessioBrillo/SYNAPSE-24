"""Canonical host keys for the per-unit calibration record.

The band's USB dump uses descriptive names (``battery_capacity_mah``,
``instrument``, ...); the host manufacturing record and the BLE readback use
short, stable names. This module is the ONE place that knows the difference,
so a factory write, a BLE write and the validator cannot disagree about what
``data/calibration/<serial>.json`` means.
"""

from __future__ import annotations

from typing import Any

PROFILE_KEYS = (
    "ecg_gain_x100",
    "ppg_led_red",
    "ppg_led_ir",
    "accel_bias_mg",
    "gyro_bias_mdps",
    "battery_mah",
)
PROVENANCE_KEYS = (
    "cal_instrument",
    "cal_fw_version",
    "cal_uncertainty_pct_x100",
    "cal_noise_std_uv_x10",
)
# band dump key -> canonical host key
HOST_KEY_MAP = {
    "battery_capacity_mah": "battery_mah",
    "instrument": "cal_instrument",
    "fw_version": "cal_fw_version",
    "gain_uncertainty_pct_x100": "cal_uncertainty_pct_x100",
    "noise_std_uv_x10": "cal_noise_std_uv_x10",
}


def canonical_record(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a host record with canonical keys, keeping already-canonical values."""
    out: dict[str, Any] = {}
    for key, value in raw.items():
        target = HOST_KEY_MAP.get(key, key)
        if target not in out or out[target] in (None, "", 0, [], ()):
            out[target] = value
    return out
