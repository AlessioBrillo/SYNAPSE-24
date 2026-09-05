"""Per-tier sync broadcast regression tests.

Architecture.md §92 + TierSyncBudget: T0 broadcast every 60s (10ms tolerance),
T1 broadcast every 10s (1ms tolerance). At 100ppm, 60s drift = 6ms > 1ms T1
budget, so the live broadcast path must be tier-aware.

RED: SyncMarkerManager.should_broadcast() ignores tier (global 60s only);
SyncMarkerStream.should_broadcast() has no interval override.
"""

from __future__ import annotations

from synapse24.acquisition.clock_sync import SyncConfig, SyncMarkerManager
from synapse24.acquisition.sync_marker_stream import SyncMarkerStream, SyncStreamConfig
from synapse24.signal_quality import Tier


class TestPerTierBroadcastManager:
    def test_t1_fires_at_11s(self):
        mgr = SyncMarkerManager(SyncConfig())
        mgr.broadcast_sync(1000.0)
        assert mgr.should_broadcast(1011.0, tier=Tier.T1) is True

    def test_t0_does_not_fire_at_11s(self):
        mgr = SyncMarkerManager(SyncConfig())
        mgr.broadcast_sync(1000.0)
        assert mgr.should_broadcast(1011.0, tier=Tier.T0) is False

    def test_t0_fires_at_61s(self):
        mgr = SyncMarkerManager(SyncConfig())
        mgr.broadcast_sync(1000.0)
        assert mgr.should_broadcast(1061.0, tier=Tier.T0) is True

    def test_legacy_no_tier_keeps_60s(self):
        mgr = SyncMarkerManager(SyncConfig())
        mgr.broadcast_sync(1000.0)
        assert mgr.should_broadcast(1011.0) is False
        assert mgr.should_broadcast(1061.0) is True

    def test_interval_lookup_matches_budget(self):
        cfg = SyncConfig()
        assert cfg.get_budget_for_tier(Tier.T1)[1] == 10.0
        assert cfg.get_budget_for_tier(Tier.T0)[1] == 60.0


class TestSyncMarkerStreamIntervalOverride:
    def test_stream_accepts_interval_override(self):
        stream = SyncMarkerStream(SyncStreamConfig(sync_interval_s=60.0))
        # T1 10s interval override must be honoured without mutating config.
        assert stream.should_broadcast_for_interval(11.0, interval_s=10.0) is True
        assert stream.should_broadcast_for_interval(5.0, interval_s=10.0) is False
