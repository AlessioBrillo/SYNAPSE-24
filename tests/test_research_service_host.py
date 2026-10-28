"""Host-side syntax and logic gate for the Synapse Research Service.

firmware/esp32_band/ble/ble_research_service.c can only be fully built by
ESP-IDF, but leaving it unverified until the first `idf.py build` means the
toolchain installation and the first syntax check happen at the same moment,
with no way to tell a driver bug from a typo.

This test compiles the file against the NimBLE/ESP-IDF stubs in
tests/firmware_host/stubs with `-Wall -Wextra -Werror`, and links it with the
raw-frame codec. It has already earned its keep: it caught an implicit
int -> uint8_t truncation of the unit-scale constants (1000 -> 232) that would
have silently corrupted every quantised sample, and a missing <stdbool.h>.

Skips (rather than fails) when no C compiler is available locally; on CI the
absence of a compiler is a failure, so the gate cannot silently disappear.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess

import numpy as np
import pytest

from synapse24.hardware.raw_codec import (  # noqa: E402
    RawFormat,
    RawHeader,
    RawStream,
)
from tests.firmware_host import COMMON, ROOT, run_compiler, try_load  # noqa: E402

BAND_BLE = ROOT / "firmware" / "esp32_band" / "ble"
GLUE = ROOT / "tests" / "firmware_host" / "host_glue_research.c"
STUBS = ROOT / "tests" / "firmware_host" / "stubs"

# Compiled with -Wall -Wextra -Werror, so a warning in this translation unit
# is a test failure.
SOURCES = [
    BAND_BLE / "ble_research_service.c",
    COMMON / "ble" / "synapse_raw_frame.c",
    GLUE,
]

INCLUDES = [
    "-Itests/firmware_host/stubs",
    "-Ifirmware/esp32_band/ble",
    "-Ifirmware/common/ble",
]

REQUIRED_STUB_HEADERS = [
    "esp_err.h",
    "esp_log.h",
    "esp_timer.h",
    "sensor_scheduler.h",
    "host/ble_hs.h",
    "host/ble_gatt.h",
    "host/ble_uuid.h",
    "host/ble_hs_mbuf.h",
    "services/gap/ble_svc_gap.h",
    "services/gatt/ble_svc_gatt.h",
]

# Header set the research service includes directly. A stub that is not in this
# list is dead weight, and an include without a stub is a silently skipped gate.
SERVICE_INCLUDES = {
    "esp_err.h",
    "esp_log.h",
    "esp_timer.h",
    "sensor_scheduler.h",
    "host/ble_hs.h",
    "host/ble_gatt.h",
    "host/ble_uuid.h",
    "host/ble_hs_mbuf.h",
    "services/gap/ble_svc_gap.h",
    "services/gatt/ble_svc_gatt.h",
    "ble_research_service.h",
    "synapse_raw_frame.h",
}


def _compiler() -> list[str]:
    """Locate a C compiler as a command list, or fail the gate.

    The gate must not silently disappear, so this never returns None: the
    fallback is `python -m ziglang cc` for an interpreter that has ziglang
    installed (`uv sync --group dev`). A compile gate that skips is not a gate.
    """
    from tests.firmware_host import _find

    found = _find("gcc", "cc", "clang")
    if found is None:
        pytest.fail("no C compiler available (install gcc/clang, or `uv sync --group dev`)")
        return []
    return found


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory):
    cc = _compiler()
    assert cc, "no C compiler available"

    out = tmp_path_factory.mktemp("research_svc")
    objs = []
    for idx, src in enumerate(SOURCES):
        obj = out / f"{idx}_{src.stem}.o"
        run_compiler(
            [
                *cc,
                "-std=c99",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-fPIC",
                *INCLUDES,
                "-c",
                str(src),
                "-o",
                str(obj),
            ]
        )
        objs.append(str(obj))

    lib = out / ("research.dll" if os.name == "nt" else "research.so")
    run_compiler([*cc, "-shared", "-o", str(lib), *objs])
    assert lib.is_file(), f"linker produced no library at {lib}"
    lib_handle = try_load(lib)
    if lib_handle is None:
        pytest.skip(
            "OS policy blocked loading the freshly built library; the compile "
            "succeeded, which is what this gate proves here"
        )
    return lib_handle


def test_research_service_compiles_against_stubs(service: ctypes.CDLL) -> None:
    service.synapse_host_smoke_main.restype = ctypes.c_int
    service.synapse_host_smoke_main.argtypes = []
    assert service.synapse_host_smoke_main() == 0


def test_research_service_uses_the_shared_codec(service: ctypes.CDLL) -> None:
    """The service must not carry a second, diverging frame implementation."""
    service.synapse_raw_max_samples.restype = ctypes.c_uint16
    service.synapse_raw_max_samples.argtypes = [ctypes.c_uint8]
    assert int(service.synapse_raw_max_samples(0x02)) == (244 - 12) // 2


def test_submit_before_init_is_safe(service: ctypes.CDLL) -> None:
    """The API is called from a FreeRTOS task; NULL must be a no-op."""
    service.synapse_research_service_submit.restype = None
    service.synapse_research_service_submit.argtypes = [ctypes.c_void_p]
    service.synapse_research_service_submit(None)


def test_stubs_cover_the_includes_the_service_uses() -> None:
    """Every include must have a stub, and every stub must still be needed."""
    source = (BAND_BLE / "ble_research_service.c").read_text(encoding="utf-8")
    header = (BAND_BLE / "ble_research_service.h").read_text(encoding="utf-8")

    for include in SERVICE_INCLUDES:
        if include in ("ble_research_service.h", "synapse_raw_frame.h"):
            continue
        included = f'"{include}"' in source or f'"{include}"' in header
        assert included, f"the research service no longer includes {include}"

    for stub in REQUIRED_STUB_HEADERS:
        assert (STUBS / stub).is_file(), f"stub missing: {stub}"

    # Guards against editing the header and forgetting the stub it needs.
    assert "sensor_sample_t" in header, "the header must forward the sample type"


def test_unit_scale_constants_fit_their_field() -> None:
    """unit_scale is uint16_t because the scale reaches 1000.

    Regression guard: this was uint8_t, which truncated 1000 to 232 and
    silently corrupted every quantised ECG/IMU sample.
    """
    header = (BAND_BLE / "ble_research_service.h").read_text(encoding="utf-8")
    assert "uint16_t unit_scale" in header
    source = (BAND_BLE / "ble_research_service.c").read_text(encoding="utf-8")
    assert "synapse_research_unit_t" in header  # enum must exist on both sides
    for constant in (
        "SYNAPSE_RESEARCH_UNIT_UV",
        "SYNAPSE_RESEARCH_UNIT_MG",
        "SYNAPSE_RESEARCH_UNIT_MRAD_S",
        "SYNAPSE_RESEARCH_UNIT_CUT",
    ):
        assert constant in header, f"{constant} must be declared"
        assert constant in source, f"{constant} must be used by the stream table"


# ---------------------------------------------------------------------------
# Behavioural bench: the service must TRANSMIT, and only under all three
# preconditions (transmitting, connected, subscribed).
#
# Before the pre-hardware closure increment all three were permanently false
# on the real firmware: the research service registered its characteristics
# and emitted nothing, and no test noticed because every existing gate proved
# compilation, idempotence and NULL-safety — never transmission. These tests
# drive the public API through the glue and decode the captured notifications
# with the host codec, so a regression to "compiles but silent" now fails CI.
# ---------------------------------------------------------------------------

_MODE_TRANSMITTING = 0x01
_MODE_CONNECTED = 0x02
_MODE_BIO_ENABLED = 0x04
_MODE_POWER_ENABLED = 0x08
_MODE_SUBSCRIBE_PATH = 0x10


def _bind_bench(service: ctypes.CDLL) -> None:
    service.synapse_host_bench_reset.restype = ctypes.c_int
    service.synapse_host_bench_reset.argtypes = []
    service.synapse_host_bench_run.restype = ctypes.c_int
    service.synapse_host_bench_run.argtypes = [ctypes.c_int, ctypes.c_int]
    service.synapse_host_bench_run_subscribe_ecg.restype = ctypes.c_int
    service.synapse_host_bench_run_subscribe_ecg.argtypes = []
    service.synapse_host_bench_frame_count.restype = ctypes.c_int
    service.synapse_host_bench_frame_count.argtypes = []
    service.synapse_host_bench_frame_len.restype = ctypes.c_int
    service.synapse_host_bench_frame_len.argtypes = [ctypes.c_int]
    service.synapse_host_bench_copy_frame.restype = ctypes.c_int
    service.synapse_host_bench_copy_frame.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    service.synapse_host_bench_dropped_frames.restype = ctypes.c_uint32
    service.synapse_host_bench_dropped_frames.argtypes = []
    service.synapse_host_bench_ring_overflow.restype = ctypes.c_uint32
    service.synapse_host_bench_ring_overflow.argtypes = []
    service.synapse_host_bench_power_dropped.restype = ctypes.c_uint32
    service.synapse_host_bench_power_dropped.argtypes = []
    service.synapse_host_bench_set_mtu.restype = None
    service.synapse_host_bench_set_mtu.argtypes = [ctypes.c_uint16]
    service.synapse_host_bench_set_stack_mtu.restype = None
    service.synapse_host_bench_set_stack_mtu.argtypes = [ctypes.c_uint16]
    service.synapse_host_bench_set_notify_fail_every.restype = None
    service.synapse_host_bench_set_notify_fail_every.argtypes = [ctypes.c_int]
    service.synapse_host_bench_set_exhaust_mbuf.restype = None
    service.synapse_host_bench_set_exhaust_mbuf.argtypes = [ctypes.c_int]
    service.synapse_host_bench_double_free.restype = ctypes.c_uint32
    service.synapse_host_bench_double_free.argtypes = []
    service.synapse_host_bench_notify_rejected.restype = ctypes.c_uint32
    service.synapse_host_bench_notify_rejected.argtypes = []


def _stream_values(frames: list[tuple[RawHeader, np.ndarray]]) -> dict[int, int]:
    """Delivered sample VALUES (channel-minor) per stream id."""
    values: dict[int, int] = {}
    for header, samples in frames:
        values[header.stream] = values.get(header.stream, 0) + samples.size
    return values


def _decoded_frames(service: ctypes.CDLL) -> list[tuple[RawHeader, np.ndarray]]:
    """Capture every notification and decode it with the host codec."""
    from synapse24.hardware.raw_codec import decode_frame

    count = service.synapse_host_bench_frame_count()
    frames = []
    for i in range(count):
        length = service.synapse_host_bench_frame_len(i)
        assert length > 0, f"frame {i} has no bytes"
        buf = ctypes.create_string_buffer(length)
        copied = service.synapse_host_bench_copy_frame(i, buf, length)
        assert copied == length
        frames.append(decode_frame(bytes(buf.raw[:length])))
    return frames


def test_service_transmits_when_fully_open(service: ctypes.CDLL) -> None:
    """All preconditions met: the pod must emit decodable raw frames."""
    _bind_bench(service)
    service.synapse_host_bench_reset()
    sent = service.synapse_host_bench_run(
        _MODE_TRANSMITTING | _MODE_CONNECTED | _MODE_BIO_ENABLED | _MODE_POWER_ENABLED, 60
    )
    assert sent > 0, "no notifications captured: the research service is silent"

    frames = _decoded_frames(service)
    assert len(frames) == sent

    # Per-stream expectations from init_stream_table(): flush thresholds
    # ECG 50, PPG 8, ACC/GYRO/MAG 4, POWER 1; the bench submits 60/20/20.
    # sample_count counts VALUES (channel-minor): PPG red+IR doubles the
    # count, the IMU sub-streams triple it, POWER quadruples it.
    by_stream: dict[int, list[int]] = {}
    for header, _samples in frames:
        by_stream.setdefault(header.stream, []).append(header.sample_count)

    assert set(by_stream) == {
        RawStream.ECG,
        RawStream.PPG,
        RawStream.ACC,
        RawStream.GYRO,
        RawStream.MAG,
        RawStream.POWER,
    }
    # One frame per threshold hit: ECG 1x50, PPG 2x(8x2), ACC/GYRO/MAG 5x(4x3),
    # POWER 2x(1x4).
    values_per_stream = {stream: sum(v) for stream, v in by_stream.items()}
    assert values_per_stream == {
        RawStream.ECG: 50,
        RawStream.PPG: 32,
        RawStream.ACC: 60,
        RawStream.GYRO: 60,
        RawStream.MAG: 60,
        RawStream.POWER: 8,
    }

    # Wire format per stream must match the contract.
    formats = {int(header.sample_format) for header, _ in frames if header.stream == RawStream.ECG}
    assert formats == {int(RawFormat.S16)}
    formats = {int(header.sample_format) for header, _ in frames if header.stream == RawStream.PPG}
    assert formats == {int(RawFormat.S32)}
    formats = {
        int(header.sample_format) for header, _ in frames if header.stream == RawStream.POWER
    }
    assert formats == {int(RawFormat.S32)}

    # The power frame's first value is the voltage in mV (the glue submits
    # 3700.0f mV; the codec must reproduce exactly what was submitted).
    power_samples = [s for header, s in frames if header.stream == RawStream.POWER]
    assert power_samples[0][0] == 3700
    assert power_samples[0][1] == 120

    # Nothing was lost or silently dropped while streaming.
    assert service.synapse_host_bench_dropped_frames() == 0
    assert service.synapse_host_bench_ring_overflow() == 0


def test_no_transmitting_no_frames(service: ctypes.CDLL) -> None:
    """transmitting=false must silence the service completely."""
    _bind_bench(service)
    service.synapse_host_bench_reset()
    sent = service.synapse_host_bench_run(_MODE_CONNECTED | _MODE_BIO_ENABLED, 60)
    assert sent == 0, f"transmitted {sent} frames while transmitting was off"


def test_not_connected_no_frames(service: ctypes.CDLL) -> None:
    """Without a GAP link nothing may leave the pod."""
    _bind_bench(service)
    service.synapse_host_bench_reset()
    sent = service.synapse_host_bench_run(_MODE_TRANSMITTING | _MODE_BIO_ENABLED, 60)
    assert sent == 0, f"transmitted {sent} frames without a connection"


def test_not_subscribed_no_frames(service: ctypes.CDLL) -> None:
    """A stream nobody subscribed to is not streamed."""
    _bind_bench(service)
    service.synapse_host_bench_reset()
    sent = service.synapse_host_bench_run(_MODE_TRANSMITTING | _MODE_CONNECTED, 60)
    assert sent == 0, f"transmitted {sent} frames with no stream subscribed"


def test_power_stream_transmits_when_subscribed(service: ctypes.CDLL) -> None:
    """The INA219 telemetry path streams only after subscription."""
    _bind_bench(service)
    service.synapse_host_bench_reset()
    sent = service.synapse_host_bench_run(
        _MODE_TRANSMITTING | _MODE_CONNECTED | _MODE_POWER_ENABLED, 60
    )
    assert sent == 2, f"expected two 1 Hz power frames, got {sent}"
    frames = _decoded_frames(service)
    streams_seen = {header.stream for header, _ in frames}
    assert streams_seen == {RawStream.POWER}, f"unexpected streams transmitted: {streams_seen}"
    assert all(samples.size == 4 for _, samples in frames)


def test_subscribe_handler_enables_the_stream(service: ctypes.CDLL) -> None:
    """The CCCD route (handle_subscribe) is what the real stack calls."""
    _bind_bench(service)
    service.synapse_host_bench_reset()
    sent = service.synapse_host_bench_run_subscribe_ecg()
    assert sent == 1, f"subscription path produced {sent} frames, expected the ECG frame only"
    frames = _decoded_frames(service)
    assert frames[0][0].stream == RawStream.ECG
    assert frames[0][0].sample_count == 50


def test_ring_overflow_is_counted_when_the_link_stalls(service: ctypes.CDLL) -> None:
    """Samples lost to a full ring must be visible, not silently discarded."""
    _bind_bench(service)
    service.synapse_host_bench_reset()
    # transmitting + subscribed, but no connection: flush_stream cannot emit,
    # the 64-sample ring fills, and the extra ECG samples must be counted.
    # The host's sequence-gap check cannot see this loss, so the counter is
    # the only witness.
    service.synapse_host_bench_run(_MODE_TRANSMITTING | _MODE_BIO_ENABLED, 200)
    overflow = service.synapse_host_bench_ring_overflow()
    assert overflow > 0, "ring overflow not counted: 200 samples were fed with no link"


# ---------------------------------------------------------------------------
# MTU negotiation: the frame size belongs to the link, not to the 244-byte
# assumption, and every failure path must leave the data queued (or counted),
# never silently gone and never double-freed.
# ---------------------------------------------------------------------------

_MODE_OPEN = _MODE_TRANSMITTING | _MODE_CONNECTED | _MODE_BIO_ENABLED | _MODE_POWER_ENABLED


def test_frames_adapt_to_the_negotiated_mtu(service: ctypes.CDLL) -> None:
    """The negotiated MTU sizes every notification.

    At MTU 23 an ECG frame carries 4 S16 values, at MTU 115 it carries 50, and
    at MTU 247 it caps at the wire limit (116). Whatever the MTU: no
    notification may exceed MTU-3 bytes, no data may be lost, and the total
    delivered payload must not depend on the negotiated value.
    """
    _bind_bench(service)
    totals: dict[int, dict[int, int]] = {}
    for mtu in (23, 115, 247):
        service.synapse_host_bench_reset()
        service.synapse_host_bench_set_mtu(mtu)
        sent = service.synapse_host_bench_run(_MODE_OPEN, 60)
        frames = _decoded_frames(service)
        assert len(frames) == sent
        lengths = [service.synapse_host_bench_frame_len(i) for i in range(sent)]
        assert max(lengths) <= mtu - 3, f"MTU {mtu}: a frame exceeded MTU-3 ({max(lengths)} B)"
        assert service.synapse_host_bench_dropped_frames() == 0, f"MTU {mtu}: frames dropped"
        assert service.synapse_host_bench_ring_overflow() == 0, f"MTU {mtu}: ring overflow"
        assert service.synapse_host_bench_double_free() == 0, f"MTU {mtu}: double free"
        assert service.synapse_host_bench_notify_rejected() == 0, f"MTU {mtu}: stack refused"
        totals[mtu] = _stream_values(frames)

    # Same payload flow at every MTU. POWER is the floor exception: its
    # 4-channel int32 sample cannot fit below MTU 31, so at MTU 23 the samples
    # are countered instead of framed
    # (test_power_stream_is_refused_below_its_mtu_floor).
    assert (
        totals[115]
        == totals[247]
        == {
            RawStream.ECG: 50,
            RawStream.PPG: 32,
            RawStream.ACC: 60,
            RawStream.GYRO: 60,
            RawStream.MAG: 60,
            RawStream.POWER: 8,
        }
    )
    assert {s: v for s, v in totals[23].items() if s != RawStream.POWER} == {
        RawStream.ECG: 50,
        RawStream.PPG: 32,
        RawStream.ACC: 60,
        RawStream.GYRO: 60,
        RawStream.MAG: 60,
    }
    assert RawStream.POWER not in totals[23]

    # A refused notification would leave a sequence hole; this happy path
    # must stream every stream without holes.
    sequences: dict[int, list[int]] = {}
    for header, _ in _decoded_frames(service):
        sequences.setdefault(header.stream, []).append(header.sequence)
    for stream, numbers in sequences.items():
        assert all(b == (a + 1) % 65536 for a, b in zip(numbers, numbers[1:])), (
            f"stream {stream}: sequence hole on the happy path: {numbers}"
        )


def test_power_stream_is_refused_below_its_mtu_floor(service: ctypes.CDLL) -> None:
    """POWER needs MTU 31 (12 header + 3 ATT + 16 for one 4-channel S32 sample).

    Below the floor the stream cannot emit a frame: it must stay queued/countered
    (visible in power_dropped), not crash, and not emit a frame sized for a
    larger link.
    """
    _bind_bench(service)
    service.synapse_host_bench_reset()
    service.synapse_host_bench_set_mtu(23)
    sent = service.synapse_host_bench_run(
        _MODE_TRANSMITTING | _MODE_CONNECTED | _MODE_POWER_ENABLED, 60
    )
    assert sent == 0, f"MTU 23 emitted {sent} frames that cannot physically fit"
    assert service.synapse_host_bench_power_dropped() > 0, "blocked POWER samples not counted"
    assert service.synapse_host_bench_double_free() == 0


def test_frame_timestamps_travel_with_the_samples(service: ctypes.CDLL) -> None:
    """Stamp the next frame with its own oldest sample, never a reconstruction.

    A partial flush must carry the timestamp of its first sample from the
    ts ring. Never multiply the sample period back: the pod clock is the only
    source of truth for spacing across queueing jitter.
    """
    _bind_bench(service)
    service.synapse_host_bench_reset()
    service.synapse_host_bench_set_mtu(23)  # 4 ECG values per frame: 13 frames
    service.synapse_host_bench_run(_MODE_TRANSMITTING | _MODE_CONNECTED | _MODE_BIO_ENABLED, 60)
    ecg = [h for h, _s in _decoded_frames(service) if h.stream == RawStream.ECG]
    assert len(ecg) == 13, "expected 50 samples split over 4-value frames (12x4+2)"
    prev = ecg[0]
    assert prev.timestamp_us > 0
    for header in ecg[1:]:
        # The glue advances the mock clock 2000 us per submitted sample and
        # ECG carries 1 sample value per channel, so the stamp moves by
        # exactly frame_sample_count * 2000 µs.
        expected = prev.timestamp_us + 2000 * prev.sample_count
        assert header.timestamp_us == expected, (
            f"frame stamp {header.timestamp_us} != previous stamp "
            f"{prev.timestamp_us} + {prev.sample_count} samples x 2000 us"
        )
        prev = header


def test_refused_notifications_stay_queued_and_never_double_free(
    service: ctypes.CDLL,
) -> None:
    """The stack refuses every notification (saturated peer queue).

    The service must score a dropped frame, keep the samples queued for the
    next tick (nothing is truly lost), and above all never free the mbuf the
    stack already consumed: that was a heap corruption on hardware.
    """
    _bind_bench(service)
    service.synapse_host_bench_reset()
    service.synapse_host_bench_set_notify_fail_every(1)
    sent = service.synapse_host_bench_run(_MODE_OPEN, 60)
    assert sent == 0, f"captured {sent} frames while the stack refused everything"
    assert service.synapse_host_bench_dropped_frames() > 0, "refusals not counted"
    assert service.synapse_host_bench_ring_overflow() == 0, (
        "samples should be queued in the ring, not overflowed"
    )
    assert service.synapse_host_bench_double_free() == 0, "the service freed a stack mbuf"


def test_mbuf_exhaustion_counts_drops_and_never_double_frees(service: ctypes.CDLL) -> None:
    """No mbuf available for a notification is the ENOMEM path.

    Nothing may be transmitted, the attempt must be counted, the samples stay
    queued, and the mbuf pool must not be freed twice.
    """
    _bind_bench(service)
    service.synapse_host_bench_reset()
    service.synapse_host_bench_set_exhaust_mbuf(1)
    service.synapse_host_bench_run(_MODE_OPEN, 60)
    assert service.synapse_host_bench_frame_count() == 0, "frames left without an mbuf"
    assert service.synapse_host_bench_dropped_frames() > 0, "ENOMEM not counted"
    assert service.synapse_host_bench_ring_overflow() == 0
    assert service.synapse_host_bench_double_free() == 0


def test_the_stack_rejects_oversized_notifications(service: ctypes.CDLL) -> None:
    """Belt and suspenders: an honest stack refuses oversized notifications.

    If the service is told an MTU larger than the link's, every notification
    that exceeds MTU-3 is refused; visibility stays truthful (rejected
    counter, no crash, no double free).
    """
    _bind_bench(service)
    service.synapse_host_bench_reset()
    service.synapse_host_bench_set_mtu(247)  # what the service believes
    service.synapse_host_bench_set_stack_mtu(50)  # what the link carries
    sent = service.synapse_host_bench_run(_MODE_OPEN, 60)
    assert sent > 0, "not even a within-budget frame got through"
    for i in range(sent):
        length = service.synapse_host_bench_frame_len(i)
        assert length <= 50 - 3, f"the link carried a {length} B frame past MTU 50"
    assert service.synapse_host_bench_notify_rejected() > 0, "oversized frames not counted"
    assert service.synapse_host_bench_dropped_frames() > 0
    assert service.synapse_host_bench_double_free() == 0
