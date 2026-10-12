"""C <-> Python parity for the raw sample frame codec.

Compiles firmware/common/ble/synapse_raw_frame.c on the host and exercises it
through ctypes against the Python decoder in synapse24.hardware.raw_codec. The
C encoder is the authority; if the two implementations ever diverge the pod
and the host would disagree on the wire format, so this test is the contract
gate for the Synapse Research Service.
"""

from __future__ import annotations

import ctypes

import numpy as np
import pytest

from synapse24.hardware.raw_codec import (
    FRAME_VERSION,
    HEADER_SIZE,
    MAX_PAYLOAD,
    RawCodecError,
    RawFormat,
    RawStream,
    decode_frame,
    encode_frame,
    format_size,
    max_samples,
)
from tests.firmware_host import COMMON, build, try_load  # noqa: E402

RAW_SRC = COMMON / "ble" / "synapse_raw_frame.c"


class _CRawHeader(ctypes.Structure):
    """ctypes mirror of synapse_raw_header_t (12 bytes, naturally aligned)."""

    _fields_ = [
        ("version", ctypes.c_uint8),
        ("stream_id", ctypes.c_uint8),
        ("sequence", ctypes.c_uint16),
        ("timestamp_us_lo", ctypes.c_uint32),
        ("flags", ctypes.c_uint8),
        ("sample_count", ctypes.c_uint8),
        ("sample_format", ctypes.c_uint8),
        ("reserved", ctypes.c_uint8),
    ]


@pytest.fixture(scope="module")
def codec(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    lib = build(tmp_path_factory.mktemp("rawframe"), [RAW_SRC])
    d = try_load(lib)
    if d is None:
        pytest.skip(
            "OS policy blocked loading the freshly built library; the compile "
            "succeeded, which is what this gate proves here"
        )

    d.synapse_raw_frame_encode.restype = ctypes.c_int
    d.synapse_raw_frame_encode.argtypes = [
        ctypes.c_uint8,
        ctypes.c_uint16,
        ctypes.c_uint32,
        ctypes.c_uint8,
        ctypes.c_uint8,
        ctypes.c_void_p,
        ctypes.c_uint8,
        ctypes.c_char_p,
        ctypes.c_size_t,
    ]
    d.synapse_raw_frame_decode.restype = ctypes.c_int
    d.synapse_raw_frame_decode.argtypes = [
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.POINTER(_CRawHeader),
    ]
    d.synapse_raw_frame_get_sample.restype = ctypes.c_int
    d.synapse_raw_frame_get_sample.argtypes = [
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.c_size_t,
        ctypes.c_size_t,
        ctypes.c_void_p,
    ]
    d.synapse_raw_format_size.restype = ctypes.c_uint8
    d.synapse_raw_format_size.argtypes = [ctypes.c_uint8]
    d.synapse_raw_max_samples.restype = ctypes.c_uint16
    d.synapse_raw_max_samples.argtypes = [ctypes.c_uint8]
    return d


def _c_encode(
    d: ctypes.CDLL,
    stream: int,
    sequence: int,
    timestamp_us: int,
    flags: int,
    fmt: RawFormat,
    samples: np.ndarray,
) -> bytes:
    n = int(samples.size)
    out = ctypes.create_string_buffer(MAX_PAYLOAD)
    rc = d.synapse_raw_frame_encode(
        stream & 0xFF,
        sequence & 0xFFFF,
        timestamp_us & 0xFFFFFFFF,
        flags & 0xFF,
        int(fmt),
        samples.ctypes.data_as(ctypes.c_void_p),
        n & 0xFF,
        out,
        MAX_PAYLOAD,
    )
    assert rc == 0, f"C encoder rejected the frame (rc={rc})"
    size = format_size(fmt)
    return out.raw[: HEADER_SIZE + n * size]


def _c_decode_all(d: ctypes.CDLL, frame: bytes) -> tuple[_CRawHeader, list[int]]:
    header = _CRawHeader()
    buf = ctypes.create_string_buffer(bytes(frame), len(frame))
    payload = d.synapse_raw_frame_decode(buf, len(frame), ctypes.byref(header))
    assert payload >= 0, f"C decoder rejected the frame (rc={payload})"

    samples: list[int] = []
    for i in range(header.sample_count):
        value = ctypes.c_int32()
        rc = d.synapse_raw_frame_get_sample(buf, len(frame), payload, i, ctypes.byref(value))
        assert rc == 0, f"C get_sample failed at index {i} (rc={rc})"
        samples.append(int(value.value))
    return header, samples


@pytest.mark.parametrize("fmt", [RawFormat.S8, RawFormat.S16, RawFormat.S32])
def test_roundtrip_c_matches_python(codec: ctypes.CDLL, fmt: RawFormat) -> None:
    rng = np.random.default_rng(7)
    n = 17
    limit = 1 << (8 * format_size(fmt) - 1)
    samples = rng.integers(-limit, limit - 1, size=n).astype(
        {RawFormat.S8: np.int8, RawFormat.S16: np.int16, RawFormat.S32: np.int32}[fmt]
    )

    frame = _c_encode(codec, int(RawStream.PPG), 4242, 0xDEADBEEF, 0, fmt, samples)

    # The Python decoder must accept byte-identical output from the C encoder.
    header, decoded = decode_frame(frame)
    assert header.version == FRAME_VERSION
    assert header.stream == RawStream.PPG
    assert header.sequence == 4242
    assert header.timestamp_us == 0xDEADBEEF
    assert header.sample_count == n
    assert header.sample_format == fmt
    np.testing.assert_array_equal(decoded, samples.astype(np.int32))

    # And the C decoder must accept what the Python encoder produced.
    py_frame = encode_frame(RawStream.PPG, 4242, 0xDEADBEEF, 0, fmt, samples.astype(np.int64))
    assert py_frame == frame, "C and Python produced different bytes for the same input"

    c_header, c_samples = _c_decode_all(codec, py_frame)
    assert c_header.sample_count == n
    assert c_header.sequence == 4242
    assert c_header.timestamp_us_lo == 0xDEADBEEF
    assert c_samples == [int(v) for v in samples]


def test_sign_extension_is_identical(codec: ctypes.CDLL) -> None:
    for fmt, values in (
        (RawFormat.S8, [-128, -1, 0, 1, 127]),
        (RawFormat.S16, [-32768, -1, 0, 1, 32767]),
        (RawFormat.S32, [-2147483648, -1, 0, 1, 2147483647]),
    ):
        samples = np.array(values, dtype={1: np.int8, 2: np.int16, 4: np.int32}[format_size(fmt)])
        frame = _c_encode(codec, int(RawStream.ECG), 1, 0, 0, fmt, samples)
        _, decoded = decode_frame(frame)
        assert list(decoded) == values, f"sign extension differs for {fmt.name}"


def test_format_limits_match(codec: ctypes.CDLL) -> None:
    for fmt in RawFormat:
        assert int(codec.synapse_raw_format_size(int(fmt))) == format_size(fmt)
        assert int(codec.synapse_raw_max_samples(int(fmt))) == max_samples(fmt)


def test_encoder_rejects_oversized_frames(codec: ctypes.CDLL) -> None:
    fmt = RawFormat.S32
    n = max_samples(fmt)
    samples = np.zeros(n, dtype=np.int32)
    ok = _c_encode(codec, int(RawStream.PPG), 0, 0, 0, fmt, samples)
    assert len(ok) == HEADER_SIZE + n * 4

    out = ctypes.create_string_buffer(MAX_PAYLOAD)
    rc = codec.synapse_raw_frame_encode(
        int(RawStream.PPG),
        0,
        0,
        0,
        int(fmt),
        samples.ctypes.data_as(ctypes.c_void_p),
        (n + 1) & 0xFF,
        out,
        MAX_PAYLOAD,
    )
    assert rc != 0, "encoder accepted a frame that cannot fit a notification"


def test_decoder_rejects_truncated_and_bad_version() -> None:
    frame = encode_frame(RawStream.ACC, 5, 1000, 0, RawFormat.S16, [1, 2, 3])
    with pytest.raises(RawCodecError):
        decode_frame(frame[:-3])
    with pytest.raises(RawCodecError):
        decode_frame(b"\x99" + frame[1:])
    with pytest.raises(RawCodecError):
        decode_frame(frame[:6])


def test_max_samples_fits_the_mtu_budget() -> None:
    for fmt in RawFormat:
        assert HEADER_SIZE + max_samples(fmt) * format_size(fmt) <= MAX_PAYLOAD


def test_ppg_frame_fits_the_ble_energy_budget() -> None:
    """Two channels at 50 Hz must fit inside the 0.6 kB/s BLE budget."""
    n = max_samples(RawFormat.S32) // 2
    frame = encode_frame(RawStream.PPG, 0, 0, 0, RawFormat.S32, [1000, 2000] * n)
    notifications_per_second = 50 / n
    bytes_per_second = len(frame) * notifications_per_second
    assert bytes_per_second <= 600, f"PPG raw stream would use {bytes_per_second:.0f} B/s"
