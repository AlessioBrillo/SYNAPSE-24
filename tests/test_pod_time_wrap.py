"""Tests for the pod→host time base (P0-3).

Il contatore µs del pod è 32 bit e wrappa ogni ~71,6 min.  I frame portano
solo i 32 bit bassi; la exchange di sync rivela il valore completo a 64 bit
(esp_timer_get_time).  Dopo un wrap, l'orologio dei frame deve essere
riancorato alla wrap corretta, altrimenti i timestamp saltano di
multipli di 2^32 µs.
"""

from __future__ import annotations

from synapse24.hardware.raw_codec import RawStream
from synapse24.hardware.research_client import DeviceClock, ResearchStreamDecoder


def test_device_clock_wrap_detected_without_seed():
    """Senza seed, un wrap viene rilevato dal calo numerico e gestito."""
    clk = DeviceClock()
    # Prima del wrap: vicino al limite dei 32 bit.
    t1 = clk.extend(0xFFFFFFF0)  # wraps=0
    # Dopo il wrap: valore basso → wrap flag → wraps diventa 1.
    t2 = clk.extend(0x00000010)
    assert t2 > t1
    assert clk.wraps == 1


def test_device_clock_seed_from_sync_anchors_correct_epoch():
    """Dopo una exchange di sync, set_wrap_base allinea l'epoca reale."""
    clk = DeviceClock()
    # Il pod è attivo da 2 ore: esp_timer_get_time() = ~7.2e9 µs.
    pod_full_us = 7_200_000_000  # wraps=1, low=0x6FC23AC0
    wraps_expected = pod_full_us >> 32
    ref_low_expected = pod_full_us & 0xFFFFFFFF
    clk.set_wrap_base(wraps_expected, ref_low_expected)
    assert clk.wraps == wraps_expected
    # Un frame subito dopo con low simile deve cadere nella stessa epoca.
    t = clk.extend(ref_low_expected + 1000)
    assert t == (wraps_expected << 32) | (ref_low_expected + 1000)


def test_decoder_seed_from_sync_anchors_all_streams():
    """seed_from_sync allinea ogni orologio per-stream alla stessa wrap base."""
    dec = ResearchStreamDecoder()
    # Simula frame visti prima del sync (ogni stream ha il suo last_low).
    dec.clocks.setdefault(RawStream.ECG, DeviceClock()).extend(0xFFFFFFF0)  # wraps=0
    dec.clocks.setdefault(RawStream.PPG, DeviceClock()).extend(0x80000000)  # low a metà
    # Sync rivela che il pod ha wrappato già una volta (2 ore di uptime).
    pod_full_us = 7_200_000_000
    dec.seed_from_sync(pod_full_us)
    wraps_expected = pod_full_us >> 32
    for clk in dec.clocks.values():
        assert clk.wraps == wraps_expected


def test_decoder_seed_from_sync_prevents_wrap_shift():
    """Dopo il seed, un frame con low basso non viene interpretato come nuovo wrap."""
    dec = ResearchStreamDecoder()
    pod_full_us = 7_200_000_000  # wraps già = 1
    dec.seed_from_sync(pod_full_us)
    # Frame successivo con low che è numericamente inferiore a ref_low:
    # senza seed verrebbe letto come wrap #2 (errato); con seed resta wraps=1.
    ref_low = pod_full_us & 0xFFFFFFFF
    t = dec.clocks[RawStream.ECG].extend(ref_low - 100)
    assert dec.clocks[RawStream.ECG].wraps == (pod_full_us >> 32)
    assert t == ((pod_full_us >> 32) << 32) | (ref_low - 100)


def test_delayed_delivery_is_not_a_wrap():
    """A low stamp that regresses by the delivery backlog is staleness, not a roll (N5).

    Every sync exchange re-anchors the clocks to the pod's *present* time, so a
    frame delivered a beat behind its anchor regresses by the backlog — tens of
    microseconds on a healthy link. Reading that as a roll added 2^32 µs
    (~71.6 minutes) of phantom pod time per frame; the reconstruction must stay
    capture-ordered and clamp, never jolt.
    """
    dec = ResearchStreamDecoder()
    pod_full_us = 5_500_000_000  # wraps = 1
    dec.seed_from_sync(pod_full_us)
    clock = dec.clocks[RawStream.ECG]
    ref_low = pod_full_us & 0xFFFFFFFF
    t1 = clock.extend(ref_low + 1_000)  # the first frame right after the anchor
    t2 = clock.extend(ref_low - 50_000)  # a frame delivered 50 ms behind it
    assert clock.wraps == (pod_full_us >> 32), "a small backward step is not a roll"
    assert t2 == t1 + 1, "the stale frame lands one microsecond after, not 71.6 min later"


def test_capture_cannot_claim_beyond_the_pod_present():
    """A frame whose capture lagged across a region boundary keeps its own time (N5).

    The exchange already announced the next region; the frame it delivered was
    captured just *before* the roll. Its claimed region is one above the pod's
    present — impossible by construction — so the reconstruction drops it back
    instead of placing the capture 71.6 minutes into the future.
    """
    clock = DeviceClock()
    clock.extend(0xFFFF0000)  # near the top of region 0
    clock.set_wrap_base(1, 1_000)  # the anchor's pod-now: region 1
    t = clock.extend(0xFFFF8000)  # a straddling frame captured pre-roll
    assert t == 0xFFFF8000, "the capture predates the roll: region 0, not region 2"
    assert clock.wraps == 1
