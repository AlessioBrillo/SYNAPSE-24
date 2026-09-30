/** Signal Quality Component - Detailed Quality Metrics */

import { useMemo } from 'react';
import { useSignalQuality } from '../hooks/useData';
import type { SystemStatus, PodSignalQuality, SensorStatus } from '../types';

interface SignalQualityProps {
  status: SystemStatus | null;
}

const MODALITY_LABELS: Record<string, string> = {
  eeg: 'EEG',
  ecg: 'ECG',
  ppg: 'PPG',
  acc: 'ACC',
  gyro: 'GYRO',
  mag: 'MAG',
  fnirs: 'fNIRS',
  eeg_ear: 'EEG (Ear)',
};

const METRIC_CONFIG: Record<string, { label: string; unit: string; goodRange?: [number, number] }[]> = {
  eeg: [
    { key: 'snr_db', label: 'SNR', unit: 'dB', goodRange: [20, Infinity] },
    { key: 'impedance_kohm', label: 'Impedance', unit: 'kΩ', goodRange: [0, 10] },
  ],
  ecg: [
    { key: 'hr_bpm', label: 'Heart Rate', unit: 'BPM', goodRange: [50, 100] },
    { key: 'rmssd_ms', label: 'RMSSD', unit: 'ms', goodRange: [20, Infinity] },
    { key: 'snr_db', label: 'SNR', unit: 'dB', goodRange: [15, Infinity] },
    { key: 'r_peak_sensitivity', label: 'R-Peak Sens.', unit: '', goodRange: [0.9, 1] },
    { key: 'r_peak_ppv', label: 'R-Peak PPV', unit: '', goodRange: [0.9, 1] },
  ],
  ppg: [
    { key: 'hr_bpm', label: 'Heart Rate', unit: 'BPM', goodRange: [50, 100] },
    { key: 'sqi', label: 'SQI', unit: '', goodRange: [0.7, 1] },
    { key: 'map', label: 'MAP', unit: '', goodRange: [0, 0.3] },
  ],
  acc: [
    { key: 'motion_intensity', label: 'Motion Intensity', unit: 'g', goodRange: [0, 0.05] },
    { key: 'posture', label: 'Posture', unit: '' },
  ],
  gyro: [
    { key: 'motion_intensity', label: 'Angular Velocity', unit: '°/s' },
  ],
  fnirs: [
    { key: 'snr_db', label: 'SNR', unit: 'dB', goodRange: [10, Infinity] },
  ],
  eeg_ear: [
    { key: 'snr_db', label: 'SNR', unit: 'dB', goodRange: [15, Infinity] },
    { key: 'impedance_kohm', label: 'Impedance', unit: 'kΩ', goodRange: [0, 20] },
  ],
};

function getMetricStatus(value: number | undefined, goodRange?: [number, number]): SensorStatus {
  if (value === undefined) return 'disconnected';
  if (!goodRange) return 'good';
  const [min, max] = goodRange;
  if (value >= min && (max === Infinity || value <= max)) return 'good';
  if (value >= min * 0.7 && (max === Infinity || value <= max * 1.3)) return 'warning';
  return 'poor';
}

function formatValue(value: number | number[] | undefined): string {
  if (value === undefined) return 'N/A';
  if (Array.isArray(value)) {
    return value.map(v => v.toFixed(1)).join(', ');
  }
  return value.toFixed(2);
}

export function SignalQuality({ status }: SignalQualityProps) {
  // Collect all pod IDs
  const podIds = useMemo(() => {
    if (!status) return [];
    return [
      ...Object.keys(status.tier1_pods),
      ...Object.keys(status.tier0_pods),
      ...Object.keys(status.inear_pods),
    ];
  }, [status});

  // Fetch quality for each pod
  const qualityHooks = podIds.map(podId => 
    useSignalQuality(podId, 3000)
  );

  const allLoading = qualityHooks.some(h => h.loading);
  const anyError = qualityHooks.some(h => h.error);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold">Signal Quality</h2>
        <div className="text-sm text-gray-400">
          {allLoading ? 'Updating...' : 'Last updated: ' + new Date().toLocaleTimeString()}
        </div>
      </div>

      {podIds.length === 0 ? (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-8 text-center">
          <p className="text-gray-500">No pods registered</p>
        </div>
      ) : (
        <div className="space-y-6">
          {podIds.map((podId, index) => {
            const hook = qualityHooks[index];
            const podQuality = hook.data;
            const podLoading = hook.loading;
            const podError = hook.error;

            // Determine pod info from status
            const podInfo = status?.tier1_pods[podId] 
              || status?.tier0_pods[podId] 
              || status?.inear_pods[podId];
            
            if (!podInfo) return null;

            return (
              <PodQualityCard
                key={podId}
                podId={podId}
                podName={podInfo.name}
                quality={podQuality}
                loading={podLoading}
                error={podError}
                connected={podInfo.connected}
                streaming={podInfo.streaming}
              />
            );
          })}
        </div>
      )}

      {/* Overall Quality Matrix */}
      {podIds.length > 0 && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
          <h3 className="text-lg font-semibold mb-4">Quality Matrix</h3>
          <QualityMatrix podIds={podIds} qualityHooks={qualityHooks} />
        </div>
      )}
    </div>
  );
}

function PodQualityCard({
  podId,
  podName,
  quality,
  loading,
  error,
  connected,
  streaming,
}: {
  podId: string;
  podName: string;
  quality: PodSignalQuality | null;
  loading: boolean;
  error: Error | null;
  connected: boolean;
  streaming: boolean;
}) {
  if (!connected) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold">{podName}</h3>
          <span className="px-3 py-1 bg-red-900/30 text-red-400 rounded-full text-xs font-medium">
            Disconnected
          </span>
        </div>
        <p className="text-gray-500">Pod is not connected. Check BLE/Serial connection.</p>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold">{podName}</h3>
          <span className="px-3 py-1 bg-yellow-900/30 text-yellow-400 rounded-full text-xs font-medium">
            Loading...
          </span>
        </div>
        <div className="flex items-center justify-center h-32">
          <div className="animate-spin rounded-full h-8 w-8 border-4 border-synapse-500 border-t-transparent" />
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold">{podName}</h3>
          <span className="px-3 py-1 bg-red-900/30 text-red-400 rounded-full text-xs font-medium">
            Error
          </span>
        </div>
        <p className="text-red-400">Failed to load quality metrics: {error.message}</p>
      </div>
    );
  }

  if (!quality) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold">{podName}</h3>
          <span className="px-3 py-1 bg-gray-700 text-gray-400 rounded-full text-xs font-medium">
            No Data
          </span>
        </div>
        <p className="text-gray-500">No quality metrics available for this pod.</p>
      </div>
    );
  }

  const modalities = Object.keys(quality);

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-lg font-semibold">{podName}</h3>
        <div className="flex items-center gap-2">
          <span className={`w-2 h-2 rounded-full ${streaming ? 'bg-green-400' : 'bg-gray-500'}`} />
          <span className="text-xs text-gray-400">{streaming ? 'Streaming' : 'Idle'}</span>
        </div>
      </div>

      {modalities.length === 0 ? (
        <p className="text-gray-500">No modalities available</p>
      ) : (
        <div className="space-y-4">
          {modalities.map(modality => {
            const metrics = quality[modality];
            const metricConfig = METRIC_CONFIG[modality] || [];
            const label = MODALITY_LABELS[modality] || modality.toUpperCase();

            return (
              <div key={modality} className="bg-gray-800 rounded-lg p-4">
                <div className="flex items-center justify-between mb-3">
                  <h4 className="font-medium text-gray-300">{label}</h4>
                  <span className={`px-2 py-1 rounded text-xs font-semibold ${
                    metrics.quality === 'good' ? 'bg-green-900 text-green-200' :
                    metrics.quality === 'warning' ? 'bg-yellow-900 text-yellow-200' :
                    metrics.quality === 'poor' ? 'bg-red-900 text-red-200' :
                    'bg-gray-700 text-gray-300'
                  }`}>
                    {metrics.quality.toUpperCase()}
                  </span>
                </div>

                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                  {metricConfig.map(({ key, label: metricLabel, unit, goodRange }) => {
                    const value = metrics[key as keyof typeof metrics];
                    const status = getMetricStatus(value as number, goodRange);
                    const statusColors = {
                      good: 'text-green-400',
                      warning: 'text-yellow-400',
                      poor: 'text-red-400',
                      disconnected: 'text-gray-500',
                    };

                    return (
                      <div key={key} className="p-3 bg-gray-900 rounded">
                        <p className="text-xs text-gray-500 mb-1">{metricLabel}</p>
                        <p className={`font-mono text-lg font-semibold ${statusColors[status]}`}>
                          {formatValue(value)}{unit ? ` ${unit}` : ''}
                        </p>
                        {goodRange && (
                          <p className="text-xs text-gray-600 mt-1">
                            Target: {goodRange[0]}{goodRange[1] !== Infinity ? ` - ${goodRange[1]}` : '+'} {unit}
                          </p>
                        )}
                      </div>
                    );
                  })}

                  {/* Show any additional metrics not in config */}
                  {Object.keys(metrics).filter(k => !metricConfig.some(m => m.key === k)).map(key => (
                    <div key={key} className="p-3 bg-gray-900 rounded">
                      <p className="text-xs text-gray-500 mb-1">{key}</p>
                      <p className="font-mono text-lg font-semibold text-gray-300">
                        {formatValue(metrics[key as keyof typeof metrics])}
                      </p>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function QualityMatrix({
  podIds,
  qualityHooks,
}: {
  podIds: string[];
  qualityHooks: ReturnType<typeof useSignalQuality>[];
}) {
  // Collect all unique modalities across pods
  const allModalities = useMemo(() => {
    const set = new Set<string>();
    qualityHooks.forEach(hook => {
      if (hook.data) {
        Object.keys(hook.data).forEach(m => set.add(m));
      }
    });
    return Array.from(set);
  }, [qualityHooks]);

  if (allModalities.length === 0) {
    return <p className="text-gray-500 text-center py-4">No quality data available</p>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-gray-700">
            <th className="text-left py-2 px-3 text-gray-400">Pod / Modality</th>
            {allModalities.map(modality => (
              <th key={modality} className="text-center py-2 px-3 text-gray-400">
                {MODALITY_LABELS[modality] || modality.toUpperCase()}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-800">
          {podIds.map((podId, podIndex) => {
            const hook = qualityHooks[podIndex];
            const quality = hook.data;
            const podInfo = status?.tier1_pods[podId] 
              || status?.tier0_pods[podId] 
              || status?.inear_pods[podId];

            return (
              <tr key={podId} className="hover:bg-gray-800/50">
                <td className="py-3 px-3 font-medium">
                  {podInfo?.name || podId}
                </td>
                {allModalities.map(modality => {
                  const metrics = quality?.[modality];
                  if (!metrics) {
                    return (
                      <td key={modality} className="text-center py-3 px-3 text-gray-600">—</td>
                    );
                  }

                  const status = metrics.quality;
                  const statusConfig = {
                    good: { bg: 'bg-green-900/30', text: 'text-green-400', label: 'GOOD' },
                    warning: { bg: 'bg-yellow-900/30', text: 'text-yellow-400', label: 'WARN' },
                    poor: { bg: 'bg-red-900/30', text: 'text-red-400', label: 'POOR' },
                    disconnected: { bg: 'bg-gray-700', text: 'text-gray-500', label: 'N/A' },
                  };
                  const config = statusConfig[status] || statusConfig.disconnected;

                  return (
                    <td key={modality} className="text-center py-3 px-3">
                      <span className={`px-2 py-1 rounded text-xs font-semibold ${config.bg} ${config.text}`}>
                        {config.label}
                      </span>
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// Need to access status in QualityMatrix - pass it as prop
function QualityMatrixWrapper({ podIds, qualityHooks, status }: { podIds: string[]; qualityHooks: ReturnType<typeof useSignalQuality>[]; status: SystemStatus | null }) {
  return <QualityMatrix podIds={podIds} qualityHooks={qualityHooks} />;
}