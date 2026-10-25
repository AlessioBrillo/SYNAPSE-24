"""Tests for the pod→host time base (P0-3).

Il contatore µs del pod è 32 bit e wrappa ogni ~71,6 min.  I frame portano
solo i 32 bit bassi; la exchange di sync rivela il valore completo a 64 bit
(esp_timer_get_time).  Dopo un wrap, l'orologio dei frame deve essere
riancorato alla wrap corretta, altrimenti i timestamp saltano di
multipli di 2^32 µs.
"""

from __future__ import annotations

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
    dec.clocks["ECG"].extend(0xFFFFFFF0)  # wraps=0, last_low vicino al limite
    dec.clocks["PPG"].extend(0x80000000)  # wraps=0, low a metà
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
    t = dec.clocks["ECG"].extend(ref_low - 100)
    assert dec.clocks["ECG"].wraps == (pod_full_us >> 32)
    assert t == ((pod_full_us >> 32) << 32) | (ref_low - 100)
