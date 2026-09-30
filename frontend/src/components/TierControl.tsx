/** Tier Control Component - Acquisition Tier Management */

import { useState } from 'react';
import { useTierControl, useRecordingControl } from '../hooks/useData';
import type { SystemStatus, Tier } from '../types';

function formatDuration(seconds: number | null): string {
  if (seconds === null) return 'N/A';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
}

interface TierControlProps {
  status: SystemStatus | null;
}

export function TierControl({ status }: TierControlProps) {
  const { changeTier, loading: tierLoading, error: tierError } = useTierControl();
  const { startRecording, stopRecording, loading: recLoading, error: recError } = useRecordingControl();
  const [selectedTier, setSelectedTier] = useState<Tier>('T1');
  const [sessionName, setSessionName] = useState('');
  const [recordingDuration, setRecordingDuration] = useState(300); // 5 minutes default
  const [recording, setRecording] = useState(false);

  const currentTier = status?.controller.current_tier || 'T0';
  const tier1Duration = status?.controller.tier1_duration_s;
  const tier2Duration = status?.controller.tier2_duration_s;
  const transitionHistory = status?.controller.recent_transitions || [];
  const motionGate = status?.controller.motion_gate;

  const handleTierChange = async (tier: Tier) => {
    try {
      await changeTier(tier, 'user_requested');
      setSelectedTier(tier);
    } catch (err) {
      // Error handled by hook
    }
  };

  const handleStartRecording = async () => {
    try {
      await startRecording(sessionName || undefined, recordingDuration || undefined);
      setRecording(true);
      setSessionName('');
    } catch (err) {
      // Error handled by hook
    }
  };

  const handleStopRecording = async () => {
    try {
      await stopRecording(sessionName || undefined);
      setRecording(false);
    } catch (err) {
      // Error handled by hook
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold">Tier Control</h2>
        <CurrentTierBadge tier={currentTier} />
      </div>

      {/* Current Tier Display */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
          <TierStatusCard
            tier="T0"
            label="Continuous H24"
            description="PPG, IMU, ECG (forearm hub) + In-ear EEG"
            active={currentTier === 'T0'}
            color="green"
          />
          <TierStatusCard
            tier="T1"
            label="High-Density Rest/Sleep"
            description="8-ch EEG, fNIRS, ECG (head pod)"
            active={currentTier === 'T1'}
            color="blue"
            duration={tier1Duration}
          />
          <TierStatusCard
            tier="T2"
            label="On-Demand Sessions"
            description="Cognitive tasks, calibration"
            active={currentTier === 'T2'}
            color="purple"
            duration={tier2Duration}
          />
        </div>
      </div>

      {/* Manual Tier Override */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
        <h3 className="text-lg font-semibold mb-4">Manual Tier Override</h3>
        <p className="text-gray-400 text-sm mb-4">
          Force a tier transition. Normal operation uses automatic promotion/demotion
          based on immobility, night window, motion gate, and power budget.
        </p>
        
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-2">
            <label className="text-sm text-gray-400">Target Tier:</label>
            <select
              value={selectedTier}
              onChange={(e) => setSelectedTier(e.target.value as Tier)}
              className="bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-synapse-500"
              disabled={tierLoading}
            >
              <option value="T0">Tier 0 — Continuous</option>
              <option value="T1">Tier 1 — Rest/Sleep</option>
              <option value="T2">Tier 2 — On-Demand</option>
            </select>
          </div>

          <button
            onClick={() => handleTierChange(selectedTier)}
            disabled={tierLoading || selectedTier === currentTier}
            className="px-4 py-2 bg-synapse-600 hover:bg-synapse-500 rounded text-sm font-medium disabled:opacity-50"
          >
            {tierLoading ? 'Changing...' : `Force ${selectedTier}`}
          </button>

          {tierError && (
            <span className="text-red-400 text-sm">{tierError.message}</span>
          )}
        </div>

        {/* Transition Rules */}
        <div className="mt-6 pt-6 border-t border-gray-800">
          <h4 className="text-sm font-medium text-gray-400 mb-3">Automatic Transition Rules</h4>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            <TransitionRule 
              from="T0" to="T1" 
              conditions={['Immobility detected (30s window)', 'Motion gate armed (SQI ≥ 0.5, MAP ≤ 0.5)', 'Power budget allows ≥ 2h']}
              color="blue"
            />
            <TransitionRule 
              from="T1" to="T0" 
              conditions={['Motion detected (MAP > 0.5)', 'Night window ended', 'Power budget exceeded']}
              color="green"
            />
            <TransitionRule 
              from="T0/T1" to="T2" 
              conditions={['User request', 'Calibration session', 'Cognitive task']}
              color="purple"
            />
            <TransitionRule 
              from="T2" to="T0/T1" 
              conditions={['Session complete', 'Duration exceeded', 'User cancel']}
              color="gray"
            />
          </div>
        </div>
      </div>

      {/* Motion Gate Status */}
      {motionGate && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
          <h3 className="text-lg font-semibold mb-4">Motion Quality Gate (T0 → T1 Promotion)</h3>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-4">
            <GateMetric label="SQI Threshold" value={motionGate.sqi_min} />
            <GateMetric label="MAP Threshold" value={motionGate.map_max} />
            <GateMetric label="Consecutive Clean" value={`${motionGate.consecutive_clean}/${motionGate.required_consecutive_clean}`} />
            <GateMetric label="Staleness Max" value={`${motionGate.sqi_staleness_max_s}s`} />
            <GateMetric 
              label="Status" 
              value={motionGate.armed ? 'ARMED' : 'DISARMED'} 
              color={motionGate.armed ? 'green' : 'red'}
            />
          </div>
          
          {motionGate.latest_sqi !== null && (
            <div className="grid grid-cols-2 gap-4">
              <div className="bg-gray-800 rounded p-4">
                <p className="text-gray-400 text-sm">Latest SQI</p>
                <p className="text-2xl font-bold text-synapse-400">{motionGate.latest_sqi.toFixed(2)}</p>
                <p className="text-xs text-gray-500 mt-1">Age: {motionGate.latest_sqi_age_s?.toFixed(1) ?? 'N/A'}s</p>
              </div>
              <div className="bg-gray-800 rounded p-4">
                <p className="text-gray-400 text-sm">Latest MAP</p>
                <p className="text-2xl font-bold text-synapse-400">{motionGate.latest_map?.toFixed(2) ?? 'N/A'}</p>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Transition History */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
        <h3 className="text-lg font-semibold mb-4">Recent Transitions</h3>
        {transitionHistory.length === 0 ? (
          <p className="text-gray-500">No transitions recorded</p>
        ) : (
          <div className="space-y-2 max-h-64 overflow-y-auto">
            {transitionHistory.slice(-10).reverse().map((t, i) => (
              <TransitionRow key={i} transition={t} />
            ))}
          </div>
        )}
      </div>

      {/* Recording Control */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
        <h3 className="text-lg font-semibold mb-4">XDF Recording</h3>
        <p className="text-gray-400 text-sm mb-4">
          Record synchronized multi-modal data to XDF format with drift correction.
        </p>
        
        {!recording ? (
          <div className="flex flex-wrap items-center gap-4">
            <div className="flex items-center gap-2">
              <label className="text-sm text-gray-400">Session Name:</label>
              <input
                type="text"
                value={sessionName}
                onChange={(e) => setSessionName(e.target.value)}
                placeholder="auto-generated"
                className="bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm w-48 focus:outline-none focus:ring-2 focus:ring-synapse-500"
              />
            </div>
            <div className="flex items-center gap-2">
              <label className="text-sm text-gray-400">Duration (s):</label>
              <input
                type="number"
                value={recordingDuration}
                onChange={(e) => setRecordingDuration(Number(e.target.value))}
                min={10}
                max={3600}
                className="bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm w-24 focus:outline-none focus:ring-2 focus:ring-synapse-500"
              />
            </div>
            <button
              onClick={handleStartRecording}
              disabled={recLoading}
              className="px-4 py-2 bg-red-600 hover:bg-red-500 rounded text-sm font-medium disabled:opacity-50 flex items-center gap-2"
            >
              <span className="w-2 h-2 rounded-full bg-red-400 animate-pulse" />
              Start Recording
            </button>
          </div>
        ) : (
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-2">
              <span className="w-3 h-3 rounded-full bg-red-400 animate-pulse" />
              <span className="text-red-400 font-medium">Recording in progress...</span>
            </div>
            <span className="text-gray-400">Session: {sessionName || 'auto-generated'}</span>
            <button
              onClick={handleStopRecording}
              disabled={recLoading}
              className="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded text-sm font-medium disabled:opacity-50"
            >
              Stop Recording
            </button>
          </div>
        )}

        {recError && (
          <p className="text-red-400 text-sm mt-2">{recError.message}</p>
        )}
      </div>
    </div>
  );
}

function CurrentTierBadge({ tier }: { tier: Tier }) {
  const configs = {
    T0: { label: 'Continuous H24', color: 'green', bg: 'bg-green-900/30 text-green-400 border border-green-500/30' },
    T1: { label: 'Rest/Sleep', color: 'blue', bg: 'bg-blue-900/30 text-blue-400 border border-blue-500/30' },
    T2: { label: 'On-Demand', color: 'purple', bg: 'bg-purple-900/30 text-purple-400 border border-purple-500/30' },
  };
  const config = configs[tier] || configs.T0;

  return (
    <div className={`px-4 py-2 rounded-lg ${config.bg}`}>
      <div className="flex items-center gap-2">
        <span className="text-xs font-semibold">TIER {tier.slice(1)}</span>
        <span className="text-xs text-gray-400">{config.label}</span>
      </div>
    </div>
  );
}

function TierStatusCard({ 
  tier, 
  label, 
  description, 
  active, 
  color, 
  duration 
}: { 
  tier: Tier; 
  label: string; 
  description: string; 
  active: boolean; 
  color: 'green' | 'blue' | 'purple';
  duration?: number | null;
}) {
  return (
    <div className={`relative p-4 rounded-lg ${active ? `border-2 border-${color}-500 bg-${color}-900/20` : 'border border-gray-800 bg-gray-800/50'}`}>
      <div className="flex items-start justify-between">
        <div>
          <div className="flex items-center gap-2 mb-2">
            <span className={`px-2 py-1 rounded text-xs font-semibold bg-${color}-900 text-${color}-200`}>
              Tier {tier.slice(1)}
            </span>
            {active && (
              <span className={`px-2 py-1 rounded text-xs font-semibold bg-${color}-900 text-${color}-200 animate-pulse`}>
                ACTIVE
              </span>
            )}
          </div>
          <h4 className="font-semibold text-gray-200 mb-1">{label}</h4>
          <p className="text-sm text-gray-500">{description}</p>
        </div>
        {duration !== undefined && (
          <div className="text-right">
            <p className="text-xs text-gray-500">Duration</p>
            <p className="font-mono text-lg font-semibold text-synapse-400">
              {formatDuration(duration)}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

function TransitionRule({ from, to, conditions, color }: { from: string; to: string; conditions: string[]; color: string }) {
  return (
    <div className="bg-gray-800 rounded-lg p-4">
      <div className="flex items-center gap-2 mb-3">
        <span className={`px-2 py-1 rounded text-xs font-semibold bg-${color}-900 text-${color}-200`}>
          {from} → {to}
        </span>
      </div>
      <ul className="space-y-1 text-xs text-gray-400">
        {conditions.map((c, i) => (
          <li key={i} className="flex items-center gap-1">
            <span className={`text-${color}-400`}>•</span>
            {c}
          </li>
        ))}
      </ul>
    </div>
  );
}

function GateMetric({ label, value, color = 'gray' }: { label: string; value: string | number; color?: 'gray' | 'green' | 'red' }) {
  const colors = {
    gray: 'text-gray-300',
    green: 'text-green-400',
    red: 'text-red-400',
  };
  return (
    <div className="bg-gray-800 rounded p-4 text-center">
      <p className="text-xs text-gray-500 mb-1">{label}</p>
      <p className={`font-mono text-xl font-semibold ${colors[color]}`}>{value}</p>
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
    user_requested: 'text-synapse-400',
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
        {new Date(transition.timestamp * 1000).toLocaleString()}
      </span>
    </div>
  );
}