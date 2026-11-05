"""MVP-1.4 S3: the band knows what time it is, and what it lost.

* ``synapse_night_window.h`` and ``ppg_fifo_batch.h`` are pure C and are compiled and **run** here.
* The per-stream byte budgets of ``config/hardware.yaml`` are computed from the real stream table
  of the research service (they were exceeded and nothing checked them).
* Static guards for what only builds under ESP-IDF: the ``set_time`` chain, the PPG FIFO drain, the
  loss counters in the status record, and the rule that the NimBLE host task never clears ring
  state a producer may be using. They prove the fixes are in place, not that the band works.
"""

from __future__ import annotations

import ctypes
import re
from pathlib import Path

import pytest
import yaml

from tests.firmware_host import build, try_load

ROOT = Path(__file__).resolve().parents[1]
BAND = ROOT / "firmware" / "esp32_band"
COMMON = ROOT / "firmware" / "common"
GLUE = ROOT / "tests" / "firmware_host"
RESEARCH = BAND / "ble" / "ble_research_service.c"
GATT = BAND / "ble" / "ble_gatt_server.c"
APP_MAIN = BAND / "main" / "app_main.c"
SCHEDULER = COMMON / "scheduler" / "sensor_scheduler.cpp"
PPG = COMMON / "sensors" / "ppg_max30102.cpp"
HW = yaml.safe_load((ROOT / "config" / "hardware.yaml").read_text(encoding="utf-8"))


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _body(source: str, signature: str) -> str:
    start = source.index(signature)
    return source[start : source.index("\n}\n", start)]


def _lib(tmp: Path, glue: str, include: Path) -> ctypes.CDLL:
    lib = try_load(build(tmp, [GLUE / glue], extra_includes=[str(include)]))
    if lib is None:
        pytest.skip("OS policy blocked loading the freshly built library; the compile succeeded")
    return lib


# --- the night window needs a real clock ----------------------------------------------------------


@pytest.fixture(scope="module")
def nw(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    lib = _lib(tmp_path_factory.mktemp("night"), "host_glue_night_window.c", BAND / "acquisition")
    lib.host_clock_is_set.argtypes = [ctypes.c_int64]
    lib.host_night_active.argtypes = [ctypes.c_int64, ctypes.c_int, ctypes.c_int]
    return lib


def _epoch(hour: int, day: int = 0) -> int:
    return 1_798_848_000 + day * 86_400 + hour * 3600  # 2026-12-31T00:00:00Z + hours


def test_the_minimum_epoch_is_the_control_points(nw: ctypes.CDLL) -> None:
    minimum = HW["ble"]["consumer_v2"]["control_point"]["set_time_min_epoch_ms"] // 1000
    assert nw.host_clock_is_set(minimum) == 1
    assert nw.host_clock_is_set(minimum - 1) == 0


def test_without_a_clock_the_night_window_is_never_active(nw: ctypes.CDLL) -> None:
    """After a boot time() is the uptime from 1970: every hour of the first 7 used to be 'night'."""
    for uptime_s in (0, 60, 3600, 6 * 3600, 30 * 86_400):
        assert nw.host_night_active(uptime_s, 23, 7) == 0
        assert nw.host_night_active(uptime_s, 0, 24) == 0


@pytest.mark.parametrize(
    ("hour", "active"),
    [(22, 0), (23, 1), (0, 1), (3, 1), (6, 1), (7, 0), (12, 0)],
)
def test_the_window_wraps_midnight(nw: ctypes.CDLL, hour: int, active: int) -> None:
    assert nw.host_night_active(_epoch(hour), 23, 7) == active


@pytest.mark.parametrize(("hour", "active"), [(8, 0), (9, 1), (16, 1), (17, 0)])
def test_a_same_day_window(nw: ctypes.CDLL, hour: int, active: int) -> None:
    assert nw.host_night_active(_epoch(hour), 9, 17) == active


# --- the PPG FIFO burst ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def batch(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    lib = _lib(tmp_path_factory.mktemp("ppgbatch"), "host_glue_ppg_batch.c", COMMON / "sensors")
    lib.host_ppg_period_us.argtypes = [ctypes.c_uint8]
    lib.host_ppg_period_us.restype = ctypes.c_uint32
    lib.host_ppg_batch_ts.argtypes = [
        ctypes.c_int64,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    lib.host_ppg_batch_ts.restype = ctypes.c_int64
    lib.host_ppg_batch_max.restype = ctypes.c_uint32
    return lib


def test_the_period_table_is_the_datasheets_spo2_sr(batch: ctypes.CDLL) -> None:
    assert [batch.host_ppg_period_us(c) for c in range(8)] == [
        1_000_000 // hz for hz in (50, 100, 200, 400, 800, 1000, 1600, 3200)
    ]
    assert batch.host_ppg_period_us(8) == 0  # a bad code is not a rate
    # the configured default is 50 Hz, the rate the manifest declares
    assert batch.host_ppg_period_us(0) == 1_000_000 // HW["pods"][0]["sampling_rate"]["ppg"]


def test_a_burst_ends_at_the_poll_time_and_is_spaced_by_the_sensor_period(
    batch: ctypes.CDLL,
) -> None:
    poll, period, count = 5_000_000, 20_000, 4
    stamps = [batch.host_ppg_batch_ts(poll, count, i, period) for i in range(count)]
    assert stamps == [poll - 60_000, poll - 40_000, poll - 20_000, poll]
    assert all(b - a == period for a, b in zip(stamps, stamps[1:], strict=False))
    assert batch.host_ppg_batch_ts(poll, 1, 0, period) == poll  # the common case: one sample


def test_a_burst_the_size_of_the_fifo_stays_in_the_past(batch: ctypes.CDLL) -> None:
    n = batch.host_ppg_batch_max()
    assert n == 32  # the MAX30102 FIFO depth
    oldest = batch.host_ppg_batch_ts(10_000_000, n, 0, 20_000)
    assert oldest == 10_000_000 - 31 * 20_000
    assert batch.host_ppg_batch_ts(10_000_000, n, n, 20_000) == 10_000_000  # out of range index


# --- the PPG driver and the scheduler: nothing is thrown away silently ------------------------------


def test_the_ppg_driver_forwards_every_fifo_sample() -> None:
    code = _text(PPG)
    read = code[code.index("esp_err_t ppg_max30102_read(") :]
    read = read[: read.index("\n}\n")]
    assert "s_batch_red[i]" in read  # every sample is stored...
    assert "ppg_batch_timestamp_us(" in read  # ...with its own time
    assert "s_ctx.skipped_samples += (uint32_t)(avail - 1)" not in code  # the old 'keep the newest'
    assert "bool ppg_max30102_has_more(" in code
    # a burst that fails half way is counted, not hidden
    assert "s_ctx.skipped_samples += (uint32_t)(avail - i)" in read


def test_the_scheduler_keeps_reading_while_the_driver_has_more_and_counts_a_full_ring() -> None:
    code = _text(SCHEDULER)
    task = code[code.index("void sensor_task_fn(void* arg) {") :]
    assert "config->has_more(config->user_ctx)" in task
    assert "int budget = 32;" in task  # bounded
    assert "if (!sensor_ring_buffer_push(&scheduler->buffers[config->type], &sample))" in task
    assert "dropped_samples[config->type]++" in task
    assert ".has_more = ppg_max30102_has_more" in _text(BAND / "sensors" / "synapse_sensors.c")


def test_the_status_record_reports_the_losses_it_used_to_hide() -> None:
    main = _text(APP_MAIN)
    status = _body(main, "static void fill_status(")
    assert "synapse_sensors_dropped_samples()" in status
    assert "st->ppg_skipped = synapse_sensors_ppg_skipped();" in status
    assert "st->power_dropped = synapse_research_service_power_dropped();" in status
    assert "= 0;    // ponytail: not counted" not in status


# --- set_time reaches the clock --------------------------------------------------------------------------


def test_set_time_goes_from_the_control_point_to_settimeofday() -> None:
    gatt = _text(GATT)
    assert ".set_time = ctl_set_time" in gatt
    assert "s_time_handler(epoch_ms)" in gatt
    assert "uint8_t req[16];" in gatt, "a set_time write is 9 bytes: an 8-byte buffer rejects it"
    main = _text(APP_MAIN)
    assert "synapse_ble_gatt_set_time_handler(on_set_time);" in main
    handler = _body(main, "static void on_set_time(")
    assert "settimeofday(&tv, NULL)" in handler
    assert "epoch_ms / 1000ULL" in handler
    assert "epoch_ms % 1000ULL" in handler


def test_the_acquisition_night_window_uses_the_clock_rule() -> None:
    code = _text(BAND / "acquisition" / "synapse_acquisition.c")
    body = _body(code, "static void update_night_window(void) {")
    assert "synapse_night_window_active(" in body
    assert "gmtime" not in body, "the hour must not come from an unset clock"


# --- the NimBLE host task does not touch ring state --------------------------------------------------------


@pytest.mark.parametrize(
    "signature",
    [
        "esp_err_t synapse_research_service_set_transmitting(",
        "void synapse_research_service_on_connect(",
        "void synapse_research_service_on_disconnect(",
        "void synapse_research_service_set_stream_enabled(",
    ],
)
def test_the_host_task_callbacks_only_post_requests(signature: str) -> None:
    body = _body(_text(RESEARCH), signature)
    assert "request_reset(" in body
    for forbidden in (".pending = 0", "->pending = 0", ".sequence = 0", "dropped_pending = 0"):
        assert forbidden not in body, f"{signature} clears ring state itself ({forbidden})"


def test_the_producers_apply_the_requests_in_their_own_context() -> None:
    code = _text(RESEARCH)
    assert "apply_resets(&s_research);" in _body(code, "void synapse_research_service_submit(")
    assert "apply_resets(&s_research);" in _body(
        code, "void synapse_research_service_submit_power("
    )
    assert "__atomic_exchange_n(&s->reset_req" in code
    assert "__atomic_fetch_or(&s->reset_req" in code


# --- the byte budgets of the raw streams ---------------------------------------------------------------------


_FORMAT_BYTES = {"S8": 1, "S16": 2, "S32": 4}


def _stream_table() -> dict[str, dict[str, int]]:
    """{stream: {flush_at, channels, bytes}} read from init_stream_table()."""
    code = _text(RESEARCH)
    table = code[code.index("static void init_stream_table(") :]
    table = table[: table.index("\n}\n")]
    out: dict[str, dict[str, int]] = {}
    for m in re.finditer(
        r"streams\[STREAM_(\w+)\] = \(synapse_research_stream_t\)\{(.*?)\n    \};", table, re.S
    ):
        block = m.group(2)
        fmt = re.search(r"SYNAPSE_RAW_FORMAT_(S\d+)", block)
        ch = re.search(r"\.channels = (\d+)", block)
        flush = re.search(r"\.flush_at = (\d+)", block)
        assert fmt
        assert ch
        assert flush
        out[m.group(1).lower()] = {
            "flush_at": int(flush.group(1)),
            "channels": int(ch.group(1)),
            "bytes": _FORMAT_BYTES[fmt.group(1)],
        }
    return out


def _bytes_per_second(name: str, spec: dict[str, int]) -> float:
    rates = HW["pods"][0]["sampling_rate"]
    rate = {"ecg": rates["ecg"], "ppg": rates["ppg"], "power": 1}.get(name, rates["imu"])
    frame = HW["raw_frame"]["header_bytes"] + spec["flush_at"] * spec["channels"] * spec["bytes"]
    return rate / spec["flush_at"] * frame


def test_every_stream_stays_inside_its_byte_budget() -> None:
    budgets = HW["raw_frame"]["per_stream_bytes_per_second_budget"]
    table = _stream_table()
    assert set(budgets) <= set(table)
    total = 0.0
    for name, spec in table.items():
        bps = _bytes_per_second(name, spec)
        total += bps
        if name in budgets:
            assert bps <= budgets[name], f"{name}: {bps:.0f} B/s exceeds its {budgets[name]} budget"
    assert total <= HW["raw_frame"]["aggregate_bytes_per_second_budget"], total


def test_a_flush_never_needs_more_than_the_ring_holds() -> None:
    header = _text(BAND / "ble" / "ble_research_service.h")
    depth = int(re.search(r"SYNAPSE_RESEARCH_RING_DEPTH (\d+)u", header).group(1))  # type: ignore[union-attr]
    for name, spec in _stream_table().items():
        assert spec["flush_at"] * 2 <= depth, f"{name}: no slack left in the ring for a refusal"
    # the largest frame still fits one notification at the full MTU
    for name, spec in _stream_table().items():
        size = HW["raw_frame"]["header_bytes"] + spec["flush_at"] * spec["channels"] * spec["bytes"]
        assert size <= HW["raw_frame"]["max_payload_bytes"], name
