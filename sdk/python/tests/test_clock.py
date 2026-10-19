# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""Pod clock model: uncalibrated raises, calibrated maps with offset and wrap."""

from __future__ import annotations

import pytest

from synapse_device.clock import TS_WRAP_US, ClockExchange, PodClockModel


def test_uncalibrated_map_raises():
    model = PodClockModel()
    with pytest.raises(RuntimeError, match="not calibrated"):
        model.map(1000)


def test_single_exchange_offset():
    model = PodClockModel()
    model.add_exchange(
        ClockExchange(host_send_us=100, pod_recv_us=200, pod_send_us=210, host_recv_us=300)
    )
    assert model.calibrated
    # offset = ((200-100)+(210-300))//2 = 5
    assert model.map(200) == 205


def test_wrap_adds_full_cycle():
    model = PodClockModel()
    model.add_exchange(ClockExchange(host_send_us=0, pod_recv_us=0, pod_send_us=0, host_recv_us=0))
    before = model.map(TS_WRAP_US - 1)
    after = model.map(TS_WRAP_US - 1, wrapped=True)
    assert after == before + TS_WRAP_US
