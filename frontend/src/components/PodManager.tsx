/** Pod Manager Component - Device Configuration & Connection Management */

import { useState, useMemo } from 'react';
import { useHardwareConfig } from '../hooks/useData';
import type { SystemStatus, PodConfig } from '../types';

interface PodManagerProps {
  status: SystemStatus | null;
}

interface PodWithConfig {
  id: string;
  name: string;
  tier: 'T1' | 'T0';
  type: 'head' | 'forearm' | 'inear';
  connected: boolean;
  streaming: boolean;
  clock_offset_ms: number;
  error_count: number;
  last_data_age_s: number | null;
  battery_remaining_pct?: number;
  config?: PodConfig;
}

export function PodManager({ status }: PodManagerProps) {
  const [editingPod, setEditingPod] = useState<string | null>(null);
  const [newPod, setNewPod] = useState(false);
  const { data: hardwareConfig, loading: configLoading } = useHardwareConfig();

  // Collect all pods from status
  const allPods = useMemo((): PodWithConfig[] => {
    if (!status) return [];
    const pods: PodWithConfig[] = [];
    
    Object.entries(status.tier1_pods).forEach(([id, pod]) => {
      pods.push({ id, ...pod, tier: 'T1', type: 'head' });
    });
    Object.entries(status.tier0_pods).forEach(([id, pod]) => {
      pods.push({ id, ...pod, tier: 'T0', type: 'forearm' });
    });
    Object.entries(status.inear_pods).forEach(([id, pod]) => {
      pods.push({ id, ...pod, tier: 'T0', type: 'inear' });
    });
    
    return pods;
  }, [status]);

  // Merge with hardware config for full pod info
  const podsWithConfig = useMemo(() => {
    if (!hardwareConfig) return allPods;
    
    return allPods.map(pod => {
      const config = hardwareConfig.pods.find(p => p.pod_id === pod.id);
      return { ...pod, config };
    });
  }, [allPods, hardwareConfig]);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h2 className="text-2xl font-bold">Pod Manager</h2>
          <p className="text-gray-400 text-sm">
            Configure and monitor sensor pods
          </p>
        </div>
        <button
          onClick={() => setNewPod(true)}
          className="px-4 py-2 bg-synapse-600 hover:bg-synapse-500 rounded text-sm font-medium"
        >
          Add Pod
        </button>
      </div>

      {/* Pod List */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
        {podsWithConfig.length === 0 ? (
          <div className="p-12 text-center">
            <svg className="w-16 h-16 mx-auto text-gray-600 mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
            </svg>
            <p className="text-gray-500 text-lg">No pods configured</p>
            <p className="text-gray-600 text-sm mt-1">Click "Add Pod" to register a new sensor pod</p>
          </div>
        ) : (
          <div className="divide-y divide-gray-800">
            {podsWithConfig.map((pod, index) => (
              <PodRow 
                key={pod.id} 
                pod={pod} 
                index={index}
                isEditing={editingPod === pod.id}
                onEdit={() => setEditingPod(pod.id)}
                onCancel={() => setEditingPod(null)}
              />
            ))}
          </div>
        )}
      </div>

      {/* Pod Configuration from hardware.yaml */}
      {hardwareConfig && !configLoading && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
          <h3 className="text-lg font-semibold mb-4">Hardware Configuration (hardware.yaml)</h3>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-700">
                  <th className="text-left p-3 font-medium text-gray-400">Pod ID</th>
                  <th className="text-left p-3 font-medium text-gray-400">Name</th>
                  <th className="text-center p-3 font-medium text-gray-400">Tier</th>
                  <th className="text-left p-3 font-medium text-gray-400">Modalities</th>
                  <th className="text-left p-3 font-medium text-gray-400">Sampling Rates</th>
                  <th className="text-left p-3 font-medium text-gray-400">BLE / Serial</th>
                </tr>
              </thead>
              <tbody>
                {hardwareConfig.pods.map((pod: PodConfig) => (
                  <tr key={pod.pod_id} className="border-b border-gray-800 hover:bg-gray-800/50">
                    <td className="p-3 font-mono text-sm">{pod.pod_id}</td>
                    <td className="p-3">{pod.name}</td>
                    <td className="p-3 text-center">
                      <span className={`px-2 py-1 rounded text-xs font-semibold ${
                        pod.tier === 1 ? 'bg-blue-900 text-blue-200' : 'bg-green-900 text-green-200'
                      }`}>
                        Tier {pod.tier}
                      </span>
                    </td>
                    <td className="p-3">
                      <div className="flex flex-wrap gap-1">
                        {pod.modalities.map((m: string) => (
                          <span key={m} className="px-2 py-0.5 bg-gray-700 rounded text-xs text-gray-300">{m}</span>
                        ))}
                      </div>
                    </td>
                    <td className="p-3">
                      <div className="text-xs text-gray-400 space-y-1">
                        {Object.entries(pod.sampling_rate).map(([mod, rate]) => (
                          <div key={mod}>{mod}: {rate} Hz</div>
                        ))}
                      </div>
                    </td>
                    <td className="p-3 text-xs text-gray-400">
                      <div>BLE: {pod.ble_address || 'N/A'}</div>
                      <div>Serial: {pod.serial_port || 'N/A'}</div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Environment Variables Help */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
        <h3 className="text-lg font-semibold mb-4">Environment Variables (config/.env)</h3>
        <p className="text-gray-400 text-sm mb-4">
          BLE MAC addresses and serial ports are configured via environment variables.
          Create a <code className="bg-gray-800 px-1.5 py-0.5 rounded font-mono text-xs">.env</code> file from <code className="bg-gray-800 px-1.5 py-0.5 rounded font-mono text-xs">.env.example</code>:
        </p>
        <div className="bg-gray-950 border border-gray-700 rounded p-4 font-mono text-xs text-gray-300 overflow-x-auto">
          <pre>{`# SYNAPSE-24 Environment Variables
# Copy to .env and fill in your device details

# Forearm Hub (Tier 0)
SYNAPSE_FOREARM_BLE=AA:BB:CC:DD:EE:FF
SYNAPSE_FOREARM_SERIAL=/dev/ttyUSB0

# Head Pod (Tier 1)
SYNAPSE_HEAD_BLE=11:22:33:44:55:66
SYNAPSE_HEAD_SERIAL=/dev/ttyUSB1

# In-Ear Satellite (Tier 0)
SYNAPSE_EAR_BLE=77:88:99:AA:BB:CC

# LSL Settings
SYNAPSE_LSL_NETWORK=local
SYNAPSE_CLOCK_DOMAIN=local_clock`}</pre>
        </div>
      </div>

      {/* Add/Edit Pod Modal */}
      {(editingPod || newPod) && (
        <PodModal
          pod={editingPod ? podsWithConfig.find(p => p.id === editingPod) ?? null : null}
          onClose={() => { setEditingPod(null); setNewPod(false); }}
        />
      )}
    </div>
  );
}

function PodRow({ 
  pod, 
  index, 
  isEditing, 
  onEdit, 
  onCancel 
}: { 
  pod: PodWithConfig; 
  index: number;
  isEditing: boolean;
  onEdit: () => void;
  onCancel: () => void;
}) {
  const tierColors = { T1: 'blue', T0: 'green' };
  const color = tierColors[pod.tier] || 'gray';

  if (isEditing) {
    return (
      <PodEditRow onCancel={onCancel} />
    );
  }

  return (
    <div className="p-4 hover:bg-gray-800/50 transition-colors">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div className="flex items-center gap-4">
          <span className="text-gray-500 w-8 text-center">{index + 1}</span>
          
          <div className="flex items-center gap-3">
            <div className={`w-3 h-3 rounded-full ${pod.connected ? 'bg-green-400' : 'bg-red-400'}`} />
            <div>
              <p className="font-medium">{pod.name || pod.id}</p>
              <p className="text-xs text-gray-500">{pod.id} • {pod.type}</p>
            </div>
          </div>

          <span className={`px-2 py-1 rounded text-xs font-semibold bg-${color}-900 text-${color}-200`}>
            Tier {pod.tier.slice(1)}
          </span>
        </div>

        <div className="flex items-center gap-4 text-sm">
          <span className={`flex items-center gap-1 ${pod.connected ? 'text-green-400' : 'text-red-400'}`}>
            <span className="w-2 h-2 rounded-full bg-current" />
            {pod.connected ? 'Connected' : 'Disconnected'}
          </span>
          
          <span className={`flex items-center gap-1 ${pod.streaming ? 'text-synapse-400' : 'text-gray-500'}`}>
            <span className={`w-2 h-2 rounded-full ${pod.streaming ? 'bg-synapse-400 animate-pulse' : 'bg-gray-500'}`} />
            {pod.streaming ? 'Streaming' : 'Idle'}
          </span>

          {pod.last_data_age_s !== null && (
            <span className={`text-xs ${pod.last_data_age_s < 1 ? 'text-green-400' : pod.last_data_age_s < 5 ? 'text-yellow-400' : 'text-red-400'}`}>
              {pod.last_data_age_s.toFixed(1)}s ago
            </span>
          )}

          {pod.clock_offset_ms !== undefined && (
            <span className="text-xs text-gray-400 font-mono">
              Offset: {pod.clock_offset_ms >= 0 ? '+' : ''}{pod.clock_offset_ms.toFixed(1)}ms
            </span>
          )}

          {pod.battery_remaining_pct !== undefined && (
            <span className="text-xs text-gray-400">
              Battery: {pod.battery_remaining_pct.toFixed(0)}%
            </span>
          )}

          <div className="flex items-center gap-2 ml-2">
            <button
              onClick={onEdit}
              className="p-2 text-gray-500 hover:text-white hover:bg-gray-800 rounded transition-colors"
              title="Edit configuration"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
              </svg>
            </button>
            <button
              className="p-2 text-gray-500 hover:text-red-400 hover:bg-gray-800 rounded transition-colors"
              title="Remove pod"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
              </svg>
            </button>
          </div>
        </div>
      </div>

      {/* Pod Details Expansion */}
      <div className="mt-4 pt-4 border-t border-gray-800 grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
        <DetailItem label="Modalities" value={pod.config?.modalities.join(', ') || 'N/A'} />
        <DetailItem label="Channels" value={pod.config ? Object.entries(pod.config.channels || {}).map(([k,v]) => `${k}:${v}`).join(', ') : 'N/A'} />
        <DetailItem label="Placement" value={pod.config?.placement || 'N/A'} />
        <DetailItem label="Electrodes" value={pod.config?.electrode_type || 'N/A'} />
      </div>
    </div>
  );
}

function PodEditRow({ onCancel }: { onCancel: () => void }) {
  return (
    <div className="p-4 bg-gray-800/50 border-t border-gray-700">
      <p className="text-gray-400 text-sm mb-4">Editing not yet implemented. Configure via hardware.yaml and .env</p>
      <div className="flex gap-2">
        <button onClick={onCancel} className="px-3 py-1 bg-gray-700 hover:bg-gray-600 rounded text-xs">Cancel</button>
      </div>
    </div>
  );
}

function DetailItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-gray-800/50 rounded p-3">
      <p className="text-xs text-gray-500 mb-1">{label}</p>
      <p className="font-mono text-sm text-gray-300 truncate">{value}</p>
    </div>
  );
}

function PodModal({ 
  pod, 
  onClose 
}: { 
  pod: PodWithConfig | null; 
  onClose: () => void;
}) {
  const isEditing = !!pod;

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-gray-900 border border-gray-800 rounded-xl max-w-2xl w-full max-h-[90vh] overflow-y-auto">
        <div className="p-6 border-b border-gray-800 flex items-center justify-between">
          <h3 className="text-xl font-bold">{isEditing ? 'Edit Pod' : 'Add New Pod'}</h3>
          <button onClick={onClose} className="p-2 text-gray-500 hover:text-white">
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="p-6 space-y-4">
          <p className="text-gray-400 text-sm">
            Pod configuration is managed via <code className="bg-gray-800 px-1.5 py-0.5 rounded font-mono">hardware.yaml</code> 
            and environment variables in <code className="bg-gray-800 px-1.5 py-0.5 rounded font-mono">.env</code>.
            This UI shows current configuration for reference.
          </p>

          {pod && (
            <div className="space-y-4">
              <h4 className="font-medium text-gray-300">Current Configuration</h4>
              <div className="bg-gray-800 rounded p-4 font-mono text-xs text-gray-300 max-h-64 overflow-y-auto">
                <pre>{JSON.stringify(pod.config, null, 2)}</pre>
              </div>
            </div>
          )}

          <div className="flex justify-end gap-3 pt-4 border-t border-gray-800">
            <button onClick={onClose} className="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded text-sm font-medium">
              Close
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}