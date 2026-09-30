/** Overview Dashboard Component */

import { useMemo } from 'react';
import type { SystemStatus, Tier1PodStatus, Tier0PodStatus, InEarPodStatus } from '../types';

interface OverviewProps {
  status: SystemStatus | null;
}

export function Overview({ status }: OverviewProps) {
  const pods = useMemo(() => {
    if (!status) return { tier1: [], tier0: [], inear: [] };
    return {
      tier1: Object.entries(status.tier1_pods).map(([id, pod]) => ({ id, ...pod })),
      tier0: Object.entries(status.tier0_pods).map(([id, pod]) => ({ id, ...pod })),
      inear: Object.entries(status.inear_pods).map(([id, pod]) => ({ id, ...pod })),
    };
  }, [status]);

  const allPods = [...pods.tier1, ...pods.tier0, ...pods.inear];
  const connectedPods = allPods.filter(p => p.connected).length;
  const streamingPods = allPods.filter(p => p.streaming).length;
  const totalPods = allPods.length;

  const avgQuality = useMemo(() => {
    const withQuality = allPods.filter(p => 'quality_passed' in p && p.quality_passed !== undefined);
    if (withQuality.length === 0) return 0;
    return withQuality.filter(p => p.quality_passed).length / withQuality.length;
  }, [allPods]);

  const tier1Duration = status?.controller.tier1_duration_s;
  const tier2Duration = status?.controller.tier2_duration_s;

  const formatDuration = (seconds: number | null) => {
    if (seconds === null) return 'N/A';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold">Overview</h2>
        <div className="text-sm text-gray-400">
          Last updated: {new Date().toLocaleTimeString()}
        </div>
      </div>

      {/* Key Metrics Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <MetricCard
          title="System Tier"
          value={status?.current_tier ?? 'T0'}
          subtitle="Acquisition tier"
          icon={
            <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
            </svg>
          }
          color="blue"
        />
        <MetricCard
          title="Pods Connected"
          value={`${connectedPods}/${totalPods}`}
          subtitle={streamingPods > 0 ? `${streamingPods} streaming` : 'None streaming'}
          icon={
            <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
            </svg>
          }
          color={connectedPods === totalPods ? 'green' : connectedPods > 0 ? 'yellow' : 'red'}
        />
        <MetricCard
          title="Signal Quality"
          value={`${Math.round(avgQuality * 100)}%`}
          subtitle="Average across pods"
          icon={
            <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
          }
          color={avgQuality >= 0.8 ? 'green' : avgQuality >= 0.5 ? 'yellow' : 'red'}
        />
        <MetricCard
          title="Sync Status"
          value={getSyncStatusLabel(status?.sync)}
          subtitle="Clock synchronization"
          icon={
            <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
          }
          color={getSyncStatusColor(status?.sync)}
        />
      </div>

      {/* Tier Status Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        <PodGroupCard
          title="Tier 1 — Head Pod(s)"
          subtitle="High-density rest/sleep (EEG, fNIRS, ECG)"
          pods={pods.tier1}
          tier="T1"
        />
        <PodGroupCard
          title="Tier 0 — Forearm Hub(s)"
          subtitle="Continuous H24 (PPG, IMU, ECG)"
          pods={pods.tier0}
          tier="T0"
        />
        <PodGroupCard
          title="Tier 0 — In-Ear Satellite(s)"
          subtitle="Continuous EEG (H24 coverage)"
          pods={pods.inear}
          tier="T0"
        />
      </div>

      {/* Recent Transitions */}
      {status && status.controller.recent_transitions.length > 0 && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
          <h3 className="text-lg font-semibold mb-4">Recent Tier Transitions</h3>
          <div className="space-y-2">
            {status.controller.recent_transitions.slice(-5).reverse().map((t, i) => (
              <TransitionRow key={i} transition={t} />
            ))}
          </div>
        </div>
      )}

      {/* Motion Gate Status */}
      {status && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
          <h3 className="text-lg font-semibold mb-4">Motion Quality Gate (T0 → T1 Promotion)</h3>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div className="bg-gray-800 rounded p-4">
              <p className="text-gray-400 text-sm">SQI Threshold</p>
              <p className="text-2xl font-bold text-synapse-400">{status.controller.motion_gate.sqi_min}</p>
            </div>
            <div className="bg-gray-800 rounded p-4">
              <p className="text-gray-400 text-sm">MAP Threshold</p>
              <p className="text-2xl font-bold text-synapse-400">{status.controller.motion_gate.map_max}</p>
            </div>
            <div className="bg-gray-800 rounded p-4">
              <p className="text-gray-400 text-sm">Consecutive Clean</p>
              <p className="text-2xl font-bold text-synapse-400">
                {status.controller.motion_gate.consecutive_clean}/{status.controller.motion_gate.required_consecutive_clean}
              </p>
            </div>
            <div className="bg-gray-800 rounded p-4">
              <p className="text-gray-400 text-sm">Gate Status</p>
              <p className={`text-2xl font-bold ${status.controller.motion_gate.armed ? 'text-green-400' : 'text-red-400'}`}>
                {status.controller.motion_gate.armed ? 'ARMED' : 'DISARMED'}
              </p>
            </div>
          </div>
          {status.controller.motion_gate.latest_sqi !== null && (
            <div className="mt-4 grid grid-cols-2 gap-4">
              <div className="bg-gray-800 rounded p-4">
                <p className="text-gray-400 text-sm">Latest SQI</p>
                <p className="text-2xl font-bold">{status.controller.motion_gate.latest_sqi.toFixed(2)}</p>
              </div>
              <div className="bg-gray-800 rounded p-4">
                <p className="text-gray-400 text-sm">Latest MAP</p>
                <p className="text-2xl font-bold">{status.controller.motion_gate.latest_map?.toFixed(2) ?? 'N/A'}</p>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Power Budget */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
        <h3 className="text-lg font-semibold mb-4">Power Budget</h3>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
          <PowerTierCard tier="T0" avgMw={5} peakMw={10} maxDuration="∞" />
          <PowerTierCard tier="T1" avgMw={50} peakMw={100} maxDuration="10h" />
          <PowerTierCard tier="T2" avgMw={100} peakMw={200} maxDuration="0.5h" />
        </div>
      </div>
    </div>
  );
}

function MetricCard({
  title,
  value,
  subtitle,
  icon,
  color,
}: {
  title: string;
  value: string;
  subtitle: string;
  icon: React.ReactNode;
  color: 'blue' | 'green' | 'yellow' | 'red';
}) {
  const colors = {
    blue: 'bg-blue-900/30 border-blue-500/30 text-blue-400',
    green: 'bg-green-900/30 border-green-500/30 text-green-400',
    yellow: 'bg-yellow-900/30 border-yellow-500/30 text-yellow-400',
    red: 'bg-red-900/30 border-red-500/30 text-red-400',
  };

  return (
    <div className={`bg-gray-900 border rounded-xl p-5 ${colors[color]}`}>
      <div className="flex items-start justify-between">
        <div>
          <p className="text-gray-400 text-sm mb-1">{title}</p>
          <p className="text-3xl font-bold">{value}</p>
          <p className="text-gray-500 text-sm mt-1">{subtitle}</p>
        </div>
        <div className="p-2 rounded-lg bg-white/5">{icon}</div>
      </div>
    </div>
  );
}

function PodGroupCard({
  title,
  subtitle,
  pods,
  tier,
}: {
  title: string;
  subtitle: string;
  pods: Array<Tier1PodStatus | Tier0PodStatus | InEarPodStatus & { id: string }>;
  tier: 'T0' | 'T1';
}) {
  const tierColors = { T0: 'green', T1: 'blue' };
  const color = tierColors[tier];

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h3 className="text-lg font-semibold">{title}</h3>
          <p className="text-gray-400 text-sm">{subtitle}</p>
        </div>
        <span className={`px-2 py-1 rounded text-xs font-semibold bg-${color}-900 text-${color}-200`}>
          Tier {tier.slice(1)}
        </span>
      </div>

      {pods.length === 0 ? (
        <p className="text-gray-500 text-sm">No pods registered</p>
      ) : (
        <div className="space-y-3">
          {pods.map((pod) => (
            <div key={pod.id} className="flex items-center justify-between p-3 bg-gray-800 rounded-lg">
              <div className="flex items-center gap-3">
                <div className={`w-3 h-3 rounded-full ${pod.connected ? 'bg-green-400' : 'bg-red-400'}`} />
                <div>
                  <p className="font-medium">{pod.name}</p>
                  <p className="text-xs text-gray-400">
                    {pod.streaming ? 'Streaming' : 'Idle'} • {pod.error_count} errors
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-4 text-sm">
                <span className="text-gray-400">Offset: {pod.clock_offset_ms.toFixed(1)}ms</span>
                {pod.last_data_age_s !== null && (
                  <span className={`text-xs ${pod.last_data_age_s < 1 ? 'text-green-400' : 'text-yellow-400'}`}>
                    {pod.last_data_age_s.toFixed(1)}s ago
                  </span>
                )}
                {'quality_passed' in pod && (
                  <span className={pod.quality_passed ? 'text-green-400' : 'text-red-400'}>
                    {pod.quality_passed ? 'Quality OK' : 'Quality Fail'}
                  </span>
                )}
                {'battery_remaining_pct' in pod && (
                  <span className="text-gray-400">Battery: {pod.battery_remaining_pct.toFixed(0)}%</span>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function TransitionRow({ transition }: { transition: { from: string; to: string; reason: string; timestamp: number } }) {
  const reasonColors: Record<string, string> = {
    immobility_detected: 'text-blue-400',
    night_window_start: 'text-purple-400',
    night_window_end: 'text-purple-400',
    movement_detected: 'text-yellow-400',
    power_budget_exceeded: 'text-red-400',
    user_requested: 'text-gray-400',
  };

  return (
    <div className="flex items-center justify-between p-3 bg-gray-800 rounded-lg">
      <div className="flex items-center gap-3">
        <span className={`px-2 py-1 rounded text-xs font-medium bg-gray-700 ${reasonColors[transition.reason] || 'text-gray-400'}`}>
          {transition.reason.replace(/_/g, ' ')}
        </span>
        <span className="text-gray-400 text-sm">
          {transition.from} → {transition.to}
        </span>
      </div>
      <span className="text-gray-500 text-xs">
        {new Date(transition.timestamp * 1000).toLocaleTimeString()}
      </span>
    </div>
  );
}

function PowerTierCard({ tier, avgMw, peakMw, maxDuration }: { tier: string; avgMw: number; peakMw: number; maxDuration: string }) {
  const colors = { T0: 'green', T1: 'blue', T2: 'purple' };
  const color = colors[tier as keyof typeof colors] || 'gray';

  return (
    <div className={`bg-gray-800 border border-${color}-500/30 rounded-lg p-4`}>
      <div className="flex items-center justify-between mb-3">
        <span className={`px-2 py-1 rounded text-xs font-semibold bg-${color}-900 text-${color}-200`}>
          Tier {tier.slice(1)}
        </span>
        <span className="text-xs text-gray-400">Max: {maxDuration}</span>
      </div>
      <div className="space-y-2 text-sm">
        <div className="flex justify-between">
          <span className="text-gray-400">Avg Power</span>
          <span className="font-medium">{avgMw} mW</span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-400">Peak Power</span>
          <span className="font-medium">{peakMw} mW</span>
        </div>
      </div>
    </div>
  );
}

function getSyncStatusLabel(sync: SystemStatus['sync'] | undefined): string {
  if (!sync) return 'Unknown';
  const allGood = Object.values(sync.pods).every(p => p.within_tolerance);
  return allGood ? 'Synced' : 'Drift Detected';
}

function getSyncStatusColor(sync: SystemStatus['sync'] | undefined): 'green' | 'yellow' | 'red' {
  if (!sync) return 'red';
  const allGood = Object.values(sync.pods).every(p => p.within_tolerance);
  return allGood ? 'green' : 'yellow';
}