/** Sync Monitor Component - Clock Synchronization Visualization */

import { useSyncStatus } from '../hooks/useData';
import type { SystemStatus, PodSyncInfo } from '../types';

interface SyncMonitorProps {
  status: SystemStatus | null;
}

export function SyncMonitor({ status }: SyncMonitorProps) {
  const { data: syncStatus, loading, error, refresh } = useSyncStatus(5000);

  // Use passed status or fetched sync status
  const effectiveSync = status?.sync || syncStatus;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h2 className="text-2xl font-bold">Clock Synchronization</h2>
          <p className="text-gray-400 text-sm">
            Multi-pod clock drift monitoring with per-tier budgets
          </p>
        </div>
        <div className="flex items-center gap-3">
          <TierBudgetDisplay />
          <button
            onClick={() => refresh()}
            disabled={loading}
            className="px-4 py-2 bg-blue-700 hover:bg-blue-600 rounded text-sm font-medium disabled:opacity-50"
          >
            {loading ? 'Refreshing...' : 'Refresh'}
          </button>
        </div>
      </div>

      {/* Error State */}
      {error && (
        <div className="bg-red-900/30 border border-red-500/30 rounded-lg p-4">
          <p className="text-red-400">Failed to load sync status: {error.message}</p>
        </div>
      )}

      {/* Sync Overview Cards */}
      {effectiveSync && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <SyncOverviewCard sync={effectiveSync} />
          <TierBudgetCard />
          <DriftToleranceCard sync={effectiveSync} />
        </div>
      )}

      {/* Pod Sync Table */}
      {effectiveSync && effectiveSync.pods && Object.keys(effectiveSync.pods).length > 0 ? (
        <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
          <div className="p-4 border-b border-gray-800">
            <h3 className="text-lg font-semibold">Pod Synchronization Status</h3>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="bg-gray-800 border-b border-gray-700">
                  <th className="text-left p-4 font-medium text-gray-400">Pod</th>
                  <th className="text-center p-4 font-medium text-gray-400">Offset (ms)</th>
                  <th className="text-center p-4 font-medium text-gray-400">Drift (ppm)</th>
                  <th className="text-center p-4 font-medium text-gray-400">Tier</th>
                  <th className="text-center p-4 font-medium text-gray-400">Tolerance</th>
                  <th className="text-center p-4 font-medium text-gray-400">Status</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(effectiveSync.pods).map(([podId, info]) => (
                  <SyncRow key={podId} podId={podId} info={info} />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-8 text-center">
          <p className="text-gray-500">No synchronization data available</p>
        </div>
      )}

      {/* Sync Configuration */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
        <h3 className="text-lg font-semibold mb-4">Sync Configuration (from hardware.yaml)</h3>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <ConfigItem label="T0 Max Drift" value="10.0 ms" />
          <ConfigItem label="T0 Sync Interval" value="60 s" />
          <ConfigItem label="T1 Max Drift" value="1.0 ms" />
          <ConfigItem label="T1 Sync Interval" value="10 s" />
          <ConfigItem label="ACC Correlation Window" value="30 s" />
          <ConfigItem label="Min ACC Correlation" value="0.7" />
          <ConfigItem label="Wired Sync GPIO" value="27" />
          <ConfigItem label="Marker Stream" value="SYNAPSE_Markers" />
        </div>
      </div>
    </div>
  );
}

function SyncOverviewCard({ sync }: { sync: { pods: Record<string, PodSyncInfo> } }) {
  const pods = Object.values(sync.pods);
  const total = pods.length;
  const withinTolerance = pods.filter(p => p.within_tolerance).length;
  const maxOffset = Math.max(...pods.map(p => Math.abs(p.offset_ms)), 0);
  const avgDrift = pods.reduce((sum, p) => sum + Math.abs(p.drift_ppm), 0) / total || 0;

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
      <h3 className="text-sm font-medium text-gray-400 mb-4">Sync Overview</h3>
      <div className="space-y-3">
        <StatRow label="Pods Synced" value={`${withinTolerance}/${total}`} 
          status={withinTolerance === total ? 'good' : withinTolerance > 0 ? 'warning' : 'poor'} />
        <StatRow label="Max Offset" value={`${maxOffset.toFixed(2)} ms`} 
          status={maxOffset < 1 ? 'good' : maxOffset < 10 ? 'warning' : 'poor'} />
        <StatRow label="Avg Drift" value={`${avgDrift.toFixed(2)} ppm`} 
          status={avgDrift < 0.5 ? 'good' : avgDrift < 2 ? 'warning' : 'poor'} />
      </div>
    </div>
  );
}

function TierBudgetCard() {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
      <h3 className="text-sm font-medium text-gray-400 mb-4">Tier Budgets</h3>
      <div className="space-y-3">
        <TierBudgetRow tier="T0" maxDrift="10 ms" interval="60 s" color="green" />
        <TierBudgetRow tier="T1" maxDrift="1 ms" interval="10 s" color="blue" />
      </div>
    </div>
  );
}

function TierBudgetRow({ tier, maxDrift, interval, color }: { tier: string; maxDrift: string; interval: string; color: 'green' | 'blue' }) {
  return (
    <div className="flex items-center justify-between p-3 bg-gray-800 rounded">
      <span className={`px-2 py-1 rounded text-xs font-semibold bg-${color}-900 text-${color}-200`}>
        Tier {tier.slice(1)}
      </span>
      <div className="text-right">
        <p className="text-sm font-medium text-gray-300">Max Drift: {maxDrift}</p>
        <p className="text-xs text-gray-500">Interval: {interval}</p>
      </div>
    </div>
  );
}

function DriftToleranceCard({ sync }: { sync: { pods: Record<string, PodSyncInfo> } }) {
  const pods = Object.values(sync.pods);
  const tier1Pods = pods.filter(p => p.tier_evaluated === 'T1');
  const tier0Pods = pods.filter(p => p.tier_evaluated === 'T0');

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
      <h3 className="text-sm font-medium text-gray-400 mb-4">Tolerance Check</h3>
      <div className="space-y-3">
        <ToleranceRow 
          label="Tier 1 (1ms)" 
          pods={tier1Pods} 
          threshold={1.0} 
        />
        <ToleranceRow 
          label="Tier 0 (10ms)" 
          pods={tier0Pods} 
          threshold={10.0} 
        />
      </div>
    </div>
  );
}

function ToleranceRow({ label, pods, threshold }: { label: string; pods: PodSyncInfo[]; threshold: number }) {
  if (pods.length === 0) {
    return (
      <div className="p-3 bg-gray-800 rounded">
        <p className="text-sm text-gray-500">{label}: No pods</p>
      </div>
    );
  }

  const withinCount = pods.filter(p => Math.abs(p.offset_ms) <= threshold).length;
  const allWithin = withinCount === pods.length;

  return (
    <div className="p-3 bg-gray-800 rounded">
      <div className="flex items-center justify-between mb-2">
        <span className="text-sm text-gray-400">{label}</span>
        <span className={`px-2 py-1 rounded text-xs font-semibold ${allWithin ? 'bg-green-900 text-green-200' : 'bg-red-900 text-red-200'}`}>
          {withinCount}/{pods.length}
        </span>
      </div>
      <div className="h-2 bg-gray-900 rounded overflow-hidden">
        <div 
          className={`h-full transition-all ${allWithin ? 'bg-green-500' : 'bg-red-500'}`}
          style={{ width: `${(withinCount / pods.length) * 100}%` }}
        />
      </div>
    </div>
  );
}

function SyncRow({ podId, info }: { podId: string; info: PodSyncInfo }) {
  const offsetColor = Math.abs(info.offset_ms) < (info.tier_evaluated === 'T1' ? 1 : 10) ? 'text-green-400' : 'text-red-400';
  const driftColor = Math.abs(info.drift_ppm) < 1 ? 'text-green-400' : Math.abs(info.drift_ppm) < 5 ? 'text-yellow-400' : 'text-red-400';

  return (
    <tr className="border-b border-gray-800 hover:bg-gray-800/50">
      <td className="p-4 font-mono text-sm">{podId}</td>
      <td className={`p-4 text-center font-mono text-sm ${offsetColor}`}>
        {info.offset_ms >= 0 ? '+' : ''}{info.offset_ms.toFixed(2)}
      </td>
      <td className={`p-4 text-center font-mono text-sm ${driftColor}`}>
        {info.drift_ppm.toFixed(2)}
      </td>
      <td className="p-4 text-center">
        <span className={`px-2 py-1 rounded text-xs font-semibold ${
          info.tier_evaluated === 'T1' ? 'bg-blue-900 text-blue-200' : 'bg-green-900 text-green-200'
        }`}>
          {info.tier_evaluated}
        </span>
      </td>
      <td className="p-4 text-center font-mono text-sm text-gray-400">
        {info.tier_evaluated === 'T1' ? '1.0 ms' : '10.0 ms'}
      </td>
      <td className="p-4 text-center">
        <span className={`px-3 py-1 rounded-full text-xs font-semibold ${
          info.within_tolerance ? 'bg-green-900/30 text-green-400' : 'bg-red-900/30 text-red-400'
        }`}>
          {info.within_tolerance ? '✓ Within' : '✗ Exceeded'}
        </span>
      </td>
    </tr>
  );
}

function StatRow({ label, value, status }: { label: string; value: string; status: 'good' | 'warning' | 'poor' }) {
  const colors = {
    good: 'text-green-400',
    warning: 'text-yellow-400',
    poor: 'text-red-400',
  };
  return (
    <div className="flex items-center justify-between">
      <span className="text-sm text-gray-400">{label}</span>
      <span className={`font-mono text-lg font-semibold ${colors[status]}`}>{value}</span>
    </div>
  );
}

function ConfigItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="p-3 bg-gray-800 rounded">
      <p className="text-xs text-gray-500">{label}</p>
      <p className="font-mono text-sm text-gray-300">{value}</p>
    </div>
  );
}

function TierBudgetDisplay() {
  // In real app, this would come from status.controller.current_tier
  const currentTier = 'T1'; // Mock
  const colors = { T0: 'green', T1: 'blue', T2: 'purple' };
  const color = colors[currentTier as keyof typeof colors] || 'gray';

  return (
    <span className={`px-3 py-1 rounded-full text-xs font-semibold bg-${color}-900 text-${color}-200`}>
      Active Budget: Tier {currentTier.slice(1)}
    </span>
  );
}