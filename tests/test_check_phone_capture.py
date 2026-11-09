"""The capture checker: it accepts what a working phone records and refuses what a broken one does.

The captures are built from a virtual band session, never from adb: the band log says what was
delivered, and the "phone" is that same delivery turned into sidecars, then damaged in one way per
test. The checker must accept the intact capture and name the damage in the others.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "sdk" / "python"))

import check_phone_capture as cpc  # noqa: E402
import virtual_band_core as vb  # noqa: E402
from synapse_device import consumer  # noqa: E402

T0 = 1_000
SET_TIME = consumer.encode_control("set_time", 1_798_848_000_123)


def session_log(scenario: str = "clean", *, mtu: int = 247, raw: bool = True) -> list[dict]:
    """A whole virtual-band session as the band logs it, with a well-behaved phone."""
    log: list[dict] = []
    session = vb.BandSession(scenario, log=log.append)
    session.connect(T0)
    session.write_control(T0, SET_TIME)
    if scenario == "cp_error":
        session.write_control(T0 + 100, SET_TIME)  # the phone retries
    log.append({"event": "mtu", "char": "feature", "mtu": [mtu]})
    wanted = ("feature", "status", *(cpc.RAW_NAMES if raw else ()))
    for c in vb.build_table():
        if c.name in wanted:
            session.subscribe(T0 + 200, c.uuid, True)
    for t in range(0, 61_000, 50):
        session.poll(T0 + 200 + t)
    return log


def sidecar(frames: list[bytes]) -> bytes:
    return b"".join(cpc.RECORD.pack(len(f), i * 1_000_000) + f for i, f in enumerate(frames))


def phone(log: list[dict]) -> tuple[list[bytes], list[bytes]]:
    """The frames an intact phone stores: features+status, and raw in arrival order."""
    l1, raw = cpc.delivered_frames(log)
    return l1, [f for name in cpc.RAW_NAMES for f in raw[name]]


def errors(findings: list[cpc.Finding]) -> list[str]:
    return [f.text for f in findings if f.level == "error"]


def run(log: list[dict], features: list[bytes], raw: list[bytes], **consents: bool) -> list[str]:
    return errors(
        cpc.check_conversation(log, raw_signals=consents.get("raw_signals", True))
        + cpc.check_sidecars(log, sidecar(features), sidecar(raw), **consents)
    )


# --- an intact capture is accepted -----------------------------------------------------------


def test_an_intact_capture_of_every_scenario_passes() -> None:
    for scenario in vb.SCENARIOS:
        log = session_log(scenario)
        features, raw = phone(log)
        assert run(log, features, raw) == [], scenario


def test_a_phone_that_started_late_passes_with_a_note() -> None:
    log = session_log()
    features, raw = phone(log)
    findings = cpc.check_sidecars(log, sidecar(features[10:]), sidecar(raw))
    assert errors(findings) == []
    assert any("missed the first 10" in f.text for f in findings)


# --- damage is named -------------------------------------------------------------------------


def test_a_frame_lost_inside_the_capture_is_an_error() -> None:
    log = session_log()
    features, raw = phone(log)
    damaged = features[:20] + features[21:]
    assert any("features" in e for e in run(log, damaged, raw))


def test_frames_out_of_order_are_an_error() -> None:
    log = session_log()
    features, raw = phone(log)
    features[5], features[6] = features[6], features[5]
    assert any("features" in e for e in run(log, features, raw))


def test_a_corrupted_raw_frame_is_an_error() -> None:
    log = session_log()
    features, raw = phone(log)
    raw[100] = raw[100][:-1] + bytes([raw[100][-1] ^ 1])
    assert any("raw/" in e for e in run(log, features, raw))


def test_an_empty_capture_is_an_error() -> None:
    log = session_log()
    assert any("recorded nothing" in e for e in run(log, [], []))


def test_a_truncated_sidecar_and_a_backwards_clock_are_errors() -> None:
    log = session_log()
    features, raw = phone(log)
    cut = sidecar(features)[:-3]
    assert any("truncated" in f.text for f in cpc.check_sidecars(log, cut, sidecar(raw)))
    back = cpc.RECORD.pack(len(features[0]), 5) + features[0] + cpc.RECORD.pack(len(features[1]), 4)
    _, bad = cpc.read_sidecar(back + features[1])
    assert any("backwards" in f.text for f in bad)


# --- consents --------------------------------------------------------------------------------


def test_a_missing_consent_must_leave_its_sidecar_empty() -> None:
    log = session_log()
    features, raw = phone(log)
    assert any("raw_signals" in e for e in run(log, features, raw, raw_signals=False))
    assert any("signal_quality" in e for e in run(log, features, raw, signal_quality=False))


def test_consent_masked_fields_are_accepted_and_reported() -> None:
    withrr = consumer.encode_feature({"flags": 4, "sequence": 1, "rr_last": 800, "rmssd_ms": 40})
    log = [{"event": "notify", "char": "feature", "hex": withrr.hex()}]
    phone_frame = cpc.mask_consent_fields(withrr)
    assert phone_frame != withrr
    findings = cpc.compare_slice("features", [withrr], [phone_frame])
    assert errors(findings) == []
    assert any("consent masking" in f.text for f in findings)
    assert cpc.delivered_frames(log)[0] == [withrr]


# --- the conversation ------------------------------------------------------------------------


def test_a_connection_without_set_time_is_an_error() -> None:
    log = [r for r in session_log() if r.get("op") != "set_time"]
    assert any("no set_time" in e for e in errors(cpc.check_conversation(log)))


def test_a_phone_that_never_connected_is_an_error() -> None:
    assert errors(cpc.check_conversation([])) == ["the phone never connected"]


def test_an_mtu_that_cannot_carry_a_feature_frame_is_an_error() -> None:
    assert any("MTU 23" in e for e in errors(cpc.check_conversation(session_log(mtu=23))))


def test_subscribing_to_raw_without_consent_is_an_error() -> None:
    log = session_log()
    assert any(
        "without the raw_signals" in e
        for e in errors(cpc.check_conversation(log, raw_signals=False))
    )


def test_a_refused_set_time_that_the_phone_retries_is_noted_not_failed() -> None:
    findings = cpc.check_conversation(session_log("cp_error"))
    assert errors(findings) == []
    assert any("retried" in f.text for f in findings)


# --- adb -------------------------------------------------------------------------------------


def test_the_newest_activity_is_the_first_with_a_features_sidecar() -> None:
    listing = "z.json\nabc.synapse-raw.bin\nabc.synapse.bin\nold.synapse.bin\n"
    assert cpc.newest_activity(listing) == "abc"
    assert cpc.newest_activity("nothing.json") is None
