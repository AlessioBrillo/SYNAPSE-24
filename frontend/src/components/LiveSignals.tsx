/** Live Signals Component - Real-time Signal Visualization */

import { useEffect, useRef, useState, useCallback, useMemo } from 'react';
import Plot from 'react-plotly.js';
import { useLiveData } from '../hooks/useData';
import type { SystemStatus, LiveDataPoint } from '../types';

interface LiveSignalsProps {
  status: SystemStatus | null;
}

// Modality configurations for each pod type
const MODALITY_CONFIG: Record<string, { name: string; color: string; unit: string; channels: string[] }[]> = {
  head_pod: [
    { name: 'EEG', color: '#ef4444', unit: 'µV', channels: ['EEG1', 'EEG2', 'EEG3', 'EEG4', 'EEG5', 'EEG6', 'EEG7', 'EEG8'] },
    { name: 'ECG', color: '#22c55e', unit: 'µV', channels: ['ECG'] },
  ],
  forearm_hub: [
    { name: 'PPG', color: '#0ea5e9', unit: 'a.u.', channels: ['PPG Red', 'PPG IR'] },
    { name: 'ECG', color: '#ef4444', unit: 'µV', channels: ['ECG'] },
    { name: 'ACC', color: '#a855f7', unit: 'g', channels: ['ACC X', 'ACC Y', 'ACC Z'] },
    { name: 'GYRO', color: '#f97316', unit: '°/s', channels: ['GYRO X', 'GYRO Y', 'GYRO Z'] },
  ],
  in_ear_satellite: [
    { name: 'EEG (Ear)', color: '#ec4899', unit: 'µV', channels: ['EEG L', 'EEG R'] },
    { name: 'ACC', color: '#a855f7', unit: 'g', channels: ['ACC X', 'ACC Y', 'ACC Z'] },
  ],
};

const WINDOW_DURATION = 10; // seconds
const UPDATE_INTERVAL = 500; // ms

export function LiveSignals({ status }: LiveSignalsProps) {
  const [selectedPod, setSelectedPod] = useState<string>('head_pod');
  const [selectedModality, setSelectedModality] = useState<string>('EEG');
  const [timeWindow, setTimeWindow] = useState(WINDOW_DURATION);
  const [paused, setPaused] = useState(false);
  const plotRef = useRef<Plot>(null);

  // Get available pods from status
  const availablePods = useMemo(() => {
    if (!status) return [];
    const pods: string[] = [];
    if (Object.keys(status.tier1_pods).length > 0) pods.push('head_pod');
    if (Object.keys(status.tier0_pods).length > 0) pods.push('forearm_hub');
    if (Object.keys(status.inear_pods).length > 0) pods.push('in_ear_satellite');
    return pods;
  }, [status]);

  // Auto-select first available pod
  useEffect(() => {
    if (availablePods.length > 0 && !availablePods.includes(selectedPod)) {
      setSelectedPod(availablePods[0]);
      setSelectedModality(MODALITY_CONFIG[availablePods[0]]?.[0]?.name || '');
    }
  }, [availablePods, selectedPod]);

  // Fetch live data
  const { data: liveData, loading, error, refresh } = useLiveData(
    selectedPod,
    timeWindow,
    paused ? 0 : UPDATE_INTERVAL
  );

  // Reset modality when pod changes
  useEffect(() => {
    const modalities = MODALITY_CONFIG[selectedPod];
    if (modalities && modalities.length > 0) {
      setSelectedModality(modalities[0].name);
    }
  }, [selectedPod]);

  // Process data for plotting
  const plotData = useMemo(() => {
    if (!liveData || !liveData.data) return [];

    const modalities = MODALITY_CONFIG[selectedPod];
    const modalityConfig = modalities?.find(m => m.name === selectedModality);
    if (!modalityConfig) return [];

    const timestamps = liveData.timestamps;
    if (!timestamps || timestamps.length === 0) return [];

    // Handle different data formats
    let channelData: number[][];
    if (Array.isArray(liveData.data)) {
      channelData = liveData.data;
    } else if (typeof liveData.data === 'object' && liveData.data !== null) {
      // Object format like { ppg: [...], acc: [...] }
      const key = selectedModality.toLowerCase().replace(' ', '_');
      const data = (liveData.data as Record<string, number[]>)[key];
      if (data) {
        channelData = [data];
      } else {
        channelData = Object.values(liveData.data as Record<string, number[]>);
      }
    } else {
      return [];
    }

    // Create traces for each channel
    const traces = channelData.map((channelValues, i) => {
      const channelName = modalityConfig.channels[i] || `Ch ${i + 1}`;
      return {
        x: timestamps,
        y: channelValues,
        type: 'scattergl' as const,
        mode: 'lines' as const,
        name: channelName,
        line: {
          color: modalityConfig.color,
          width: 1,
        },
        opacity: channelData.length > 1 ? 0.8 : 1,
      };
    });

    return traces;
  }, [liveData, selectedPod, selectedModality]);

  // Layout configuration
  const layout = useMemo(() => ({
    plot_bgcolor: '#050505',
    paper_bgcolor: '#050505',
    font: { color: '#d1d5db', family: 'Inter, sans-serif' },
    margin: { l: 60, r: 30, t: 40, b: 50 },
    hovermode: 'x unified' as const,
    xaxis: {
      showgrid: true,
      gridcolor: '#374151',
      title: { text: 'Time (s)', font: { size: 12 } },
      range: liveData?.timestamps?.length
        ? [liveData.timestamps[liveData.timestamps.length - 1] - timeWindow, liveData.timestamps[liveData.timestamps.length - 1]]
        : [0, timeWindow],
      fixedrange: false,
    },
    yaxis: {
      showgrid: true,
      gridcolor: '#374151',
      title: { text: MODALITY_CONFIG[selectedPod]?.find(m => m.name === selectedModality)?.unit || '', font: { size: 12 } },
      fixedrange: false,
    },
    height: 350,
    dragmode: 'pan' as const,
    uirevision: 'live-signals', // Preserve zoom/pan on updates
  }), [selectedPod, selectedModality, timeWindow, liveData?.timestamps?.length]);

  // Handle plot ref for imperative updates
  const handlePlotRef = useCallback((ref: Plot | null) => {
    plotRef.current = ref;
  }, []);

  return (
    <div className="space-y-6">
      {/* Header & Controls */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h2 className="text-2xl font-bold">Live Signals</h2>
          <p className="text-gray-400 text-sm">
            Real-time data from {selectedPod.replace('_', ' ')} • {selectedModality}
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          {/* Pod Selector */}
          <select
            value={selectedPod}
            onChange={(e) => setSelectedPod(e.target.value)}
            className="bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-synapse-500"
            disabled={availablePods.length === 0}
          >
            {availablePods.map(pod => (
              <option key={pod} value={pod}>
                {pod.replace('_', ' ')}
              </option>
            ))}
            {availablePods.length === 0 && <option value="">No pods available</option>}
          </select>

          {/* Modality Selector */}
          <select
            value={selectedModality}
            onChange={(e) => setSelectedModality(e.target.value)}
            className="bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-synapse-500"
          >
            {MODALITY_CONFIG[selectedPod]?.map(m => (
              <option key={m.name} value={m.name}>{m.name}</option>
            ))}
          </select>

          {/* Time Window */}
          <div className="flex items-center gap-2">
            <label className="text-sm text-gray-400">Window:</label>
            <select
              value={timeWindow}
              onChange={(e) => setTimeWindow(Number(e.target.value))}
              className="bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-synapse-500 w-28"
            >
              <option value={5}>5s</option>
              <option value={10}>10s</option>
              <option value={30}>30s</option>
              <option value={60}>60s</option>
            </select>
          </div>

          {/* Pause/Resume */}
          <button
            onClick={() => setPaused(!paused)}
            className={`px-4 py-2 rounded text-sm font-medium transition-colors ${
              paused
                ? 'bg-green-700 hover:bg-green-600'
                : 'bg-yellow-700 hover:bg-yellow-600'
            }`}
          >
            {paused ? 'Resume' : 'Pause'}
          </button>

          {/* Refresh */}
          <button
            onClick={() => refresh()}
            disabled={loading}
            className="px-4 py-2 bg-blue-700 hover:bg-blue-600 rounded text-sm font-medium disabled:opacity-50"
          >
            Refresh
          </button>
        </div>
      </div>

      {/* Status Indicators */}
      <div className="flex flex-wrap items-center gap-4 text-sm">
        <StatusBadge label="Pod" value={selectedPod} />
        <StatusBadge label="Modality" value={selectedModality} />
        <StatusBadge label="Sampling" value={`${liveData?.sampling_rate ?? 'N/A'} Hz`} />
        <StatusBadge label="Samples" value={liveData?.data ? (Array.isArray(liveData.data) ? liveData.data[0]?.length : 'N/A') : '0'} />
        <StatusBadge label="Status" value={loading ? 'Loading...' : error ? 'Error' : 'Live'} color={loading ? 'yellow' : error ? 'red' : 'green'} />
      </div>

      {/* Plot Area */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
        {plotData.length > 0 ? (
          <Plot
            ref={handlePlotRef}
            data={plotData}
            layout={layout}
            config={{
              responsive: true,
              displayModeBar: true,
              displaylogo: false,
              modeBarButtonsToRemove: ['lasso2d', 'select2d', 'autoScale2d'],
              toImageButtonOptions: {
                format: 'png',
                filename: `synapse24-${selectedPod}-${selectedModality}-${Date.now()}`,
                height: 600,
                width: 1200,
                scale: 2,
              },
            }}
            useResizeHandler
            style={{ width: '100%', height: '400px' }}
          />
        ) : (
          <div className="flex items-center justify-center h-[400px] bg-gray-950">
            <div className="text-center">
              <svg className="w-16 h-16 mx-auto text-gray-600 mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
              </svg>
              <p className="text-gray-500 text-lg">No signal data</p>
              <p className="text-gray-600 text-sm mt-1">
                {error ? `Error: ${error.message}` : 'Waiting for live data...'}
              </p>
            </div>
          </div>
        )}
      </div>

      {/* Channel Legend */}
      {plotData.length > 0 && (
        <div className="flex flex-wrap gap-4 text-sm">
          {plotData.map((trace, i) => (
            <span key={i} className="flex items-center gap-2">
              <span className="w-6 h-1 rounded" style={{ backgroundColor: trace.line?.color }} />
              <span className="text-gray-300">{trace.name}</span>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function StatusBadge({ label, value, color = 'gray' }: { label: string; value: string | number; color?: 'gray' | 'green' | 'yellow' | 'red' }) {
  const colors = {
    gray: 'bg-gray-800 text-gray-300',
    green: 'bg-green-900/30 text-green-400 border border-green-500/30',
    yellow: 'bg-yellow-900/30 text-yellow-400 border border-yellow-500/30',
    red: 'bg-red-900/30 text-red-400 border border-red-500/30',
  };

  return (
    <span className={`px-3 py-1 rounded-full text-xs font-medium ${colors[color]}`}>
      {label}: {value}
    </span>
  );
}