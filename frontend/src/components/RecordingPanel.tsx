/** Recording Panel Component - XDF Session Recording & Management */

import { useState, useEffect, useMemo } from 'react';
import { useRecordingControl } from '../hooks/useData';
import type { SystemStatus } from '../types';

interface RecordingPanelProps {
  status: SystemStatus | null;
}

export function RecordingPanel({ status }: RecordingPanelProps) {
  const { startRecording, stopRecording, loading: recLoading, error: recError } = useRecordingControl();
  const [recording, setRecording] = useState(false);
  const [sessionName, setSessionName] = useState('');
  const [recordingDuration, setRecordingDuration] = useState(300);
  const [autoStop, setAutoStop] = useState(false);
  const [sessions, setSessions] = useState<RecordingSession[]>([]);
  const [elapsedTime, setElapsedTime] = useState(0);
  const [selectedPods, setSelectedPods] = useState<string[]>([]);

  // Get available pods from status
  const availablePods = useMemo(() => {
    if (!status) return [];
    const pods: string[] = [];
    Object.keys(status.tier1_pods).forEach(id => pods.push(id));
    Object.keys(status.tier0_pods).forEach(id => pods.push(id));
    Object.keys(status.inear_pods).forEach(id => pods.push(id));
    return pods;
  }, [status]);

  // Initialize selected pods to all available
  useEffect(() => {
    setSelectedPods(availablePods);
  }, [availablePods]);

  // Timer for recording elapsed time
  useEffect(() => {
    let interval: ReturnType<typeof setInterval>;
    if (recording) {
      setElapsedTime(0);
      interval = setInterval(() => setElapsedTime(t => t + 1), 1000);
    }
    return () => { if (interval) clearInterval(interval); };
  }, [recording]);

  // Auto-stop when duration reached
  useEffect(() => {
    if (recording && autoStop && elapsedTime >= recordingDuration) {
      handleStopRecording();
    }
  }, [recording, autoStop, elapsedTime, recordingDuration]);

  const handleStartRecording = async () => {
    try {
      const result = await startRecording(sessionName || undefined, autoStop ? recordingDuration : undefined);
      setRecording(true);
      setElapsedTime(0);
      // Add to local sessions list
      setSessions(prev => [{
        id: result.session_name,
        name: sessionName || result.session_name,
        startTime: Date.now(),
        duration: autoStop ? recordingDuration : null,
        pods: [...selectedPods],
        status: 'recording',
        path: null,
      }, ...prev]);
    } catch (err) {
      // Error handled by hook
    }
  };

  const handleStopRecording = async () => {
    try {
      await stopRecording(sessionName || undefined);
      setRecording(false);
      // Update session status
      setSessions(prev => prev.map(s => 
        s.status === 'recording' ? { ...s, status: 'completed', endTime: Date.now() } : s
      ));
    } catch (err) {
      // Error handled by hook
    }
  };

  const handleDeleteSession = (sessionId: string) => {
    setSessions(prev => prev.filter(s => s.id !== sessionId));
  };

  const formatDuration = (seconds: number) => {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
  };

  const formatTimestamp = (ts: number) => {
    return new Date(ts).toLocaleString();
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h2 className="text-2xl font-bold">Recording</h2>
          <p className="text-gray-400 text-sm">
            Record synchronized multi-modal sessions to XDF with drift correction
          </p>
        </div>
        <RecordingStatusIndicator recording={recording} elapsed={elapsedTime} />
      </div>

      {/* Recording Controls */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
        <h3 className="text-lg font-semibold mb-4">Session Configuration</h3>
        
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-6">
          <div>
            <label className="block text-sm text-gray-400 mb-2">Session Name</label>
            <input
              type="text"
              value={sessionName}
              onChange={(e) => setSessionName(e.target.value)}
              placeholder="auto-generated (e.g., session_1700000000)"
              className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-synapse-500"
              disabled={recording}
            />
            <p className="text-xs text-gray-500 mt-1">Leave empty for auto-generated name</p>
          </div>

          <div>
            <label className="block text-sm text-gray-400 mb-2">Max Duration (seconds)</label>
            <input
              type="number"
              value={recordingDuration}
              onChange={(e) => setRecordingDuration(Math.max(10, Math.min(3600, Number(e.target.value))))}
              min={10}
              max={3600}
              className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-synapse-500"
              disabled={recording || !autoStop}
            />
            <label className="flex items-center gap-2 mt-2 text-sm text-gray-400 cursor-pointer">
              <input
                type="checkbox"
                checked={autoStop}
                onChange={(e) => setAutoStop(e.target.checked)}
                disabled={recording}
                className="rounded border-gray-700 text-synapse-500 focus:ring-synapse-500"
              />
              Auto-stop after duration
            </label>
          </div>

          <div>
            <label className="block text-sm text-gray-400 mb-2">Pods to Record</label>
            <div className="bg-gray-800 rounded p-3 max-h-40 overflow-y-auto">
              {availablePods.length === 0 ? (
                <p className="text-gray-500 text-sm">No pods available</p>
              ) : (
                availablePods.map(podId => (
                  <label key={podId} className="flex items-center gap-2 text-sm cursor-pointer">
                    <input
                      type="checkbox"
                      checked={selectedPods.includes(podId)}
                      onChange={(e) => {
                        if (e.target.checked) {
                          setSelectedPods(prev => [...prev, podId]);
                        } else {
                          setSelectedPods(prev => prev.filter(p => p !== podId));
                        }
                      }}
                      disabled={recording}
                      className="rounded border-gray-700 text-synapse-500 focus:ring-synapse-500"
                    />
                    <span className="text-gray-300">{podId}</span>
                    <span className="text-xs text-gray-500 ml-auto">
                      {status?.tier1_pods[podId]?.name || status?.tier0_pods[podId]?.name || status?.inear_pods[podId]?.name}
                    </span>
                  </label>
                ))
              )}
            </div>
          </div>
        </div>

        {/* Action Buttons */}
        <div className="flex flex-wrap items-center gap-4 pt-4 border-t border-gray-800">
          {!recording ? (
            <button
              onClick={handleStartRecording}
              disabled={recLoading || selectedPods.length === 0}
              className="px-6 py-3 bg-red-600 hover:bg-red-500 rounded text-sm font-medium disabled:opacity-50 flex items-center gap-2"
            >
              <span className="w-3 h-3 rounded-full bg-red-400 animate-pulse" />
              Start Recording
            </button>
          ) : (
            <div className="flex items-center gap-4">
              <span className="flex items-center gap-2 text-red-400 font-medium">
                <span className="w-3 h-3 rounded-full bg-red-400 animate-pulse" />
                Recording... {formatDuration(elapsedTime)}
              </span>
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
            <span className="text-red-400 text-sm flex-1">{recError.message}</span>
          )}
        </div>
      </div>

      {/* Recording Info & Tips */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
          <h3 className="text-lg font-semibold mb-4">Recording Features</h3>
          <ul className="space-y-3 text-sm text-gray-300">
            <li className="flex items-center gap-2">
              <span className="text-green-400">✓</span>
              Multi-pod synchronized XDF export
            </li>
            <li className="flex items-center gap-2">
              <span className="text-green-400">✓</span>
              Clock drift correction (per-tier budgets)
            </li>
            <li className="flex items-center gap-2">
              <span className="text-green-400">✓</span>
              Sync markers embedded in XDF
            </li>
            <li className="flex items-center gap-2">
              <span className="text-green-400">✓</span>
              Per-stream quality metrics included
            </li>
            <li className="flex items-center gap-2">
              <span className="text-green-400">✓</span>
              Tier transition events logged
            </li>
            <li className="flex items-center gap-2">
              <span className="text-green-400">✓</span>
              Impedance reports (Tier 1 EEG)
            </li>
          </ul>
        </div>

        <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
          <h3 className="text-lg font-semibold mb-4">XDF Structure</h3>
          <div className="bg-gray-950 border border-gray-700 rounded p-4 font-mono text-xs text-gray-300 overflow-x-auto">
            <pre>{`XDF File Structure:
├── header (version, timestamp)
├── streams[]:
│   ├── SYNAPSE_EEG_T1 (8-ch @ 500Hz)
│   ├── SYNAPSE_FNIRS_T1 (8-ch @ 10Hz)
│   ├── SYNAPSE_ECG_T1 (1-ch @ 500Hz)
│   ├── SYNAPSE_ECG_T0 (1-ch @ 500Hz)
│   ├── SYNAPSE_PPG_T0 (2-ch @ 64Hz)
│   ├── SYNAPSE_ACC_T0 (3-ch @ 100Hz)
│   ├── SYNAPSE_GYRO_T0 (3-ch @ 100Hz)
│   ├── SYNAPSE_MAG_T0 (3-ch @ 100Hz)
│   ├── SYNAPSE_EEG_T0_EAR (2-ch @ 250Hz)
│   └── SYNAPSE_Markers (irregular)
└── footer`}</pre>
          </div>
        </div>
      </div>

      {/* Session History */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
        <h3 className="text-lg font-semibold mb-4">Recent Sessions</h3>
        {sessions.length === 0 ? (
          <p className="text-gray-500 text-center py-8">No sessions recorded yet</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-700">
                  <th className="text-left p-3 font-medium text-gray-400">Session</th>
                  <th className="text-left p-3 font-medium text-gray-400">Start Time</th>
                  <th className="text-center p-3 font-medium text-gray-400">Duration</th>
                  <th className="text-left p-3 font-medium text-gray-400">Pods</th>
                  <th className="text-center p-3 font-medium text-gray-400">Status</th>
                  <th className="text-center p-3 font-medium text-gray-400">Actions</th>
                </tr>
              </thead>
              <tbody>
                {sessions.map((session) => (
                  <tr key={session.id} className="border-b border-gray-800 hover:bg-gray-800/50">
                    <td className="p-3 font-mono text-sm">{session.name}</td>
                    <td className="p-3 text-gray-300">{formatTimestamp(session.startTime)}</td>
                    <td className="p-3 text-center font-mono">
                      {session.status === 'recording' 
                        ? formatDuration(Math.floor((Date.now() - session.startTime) / 1000))
                        : session.duration 
                          ? formatDuration(session.duration)
                          : formatDuration(Math.floor((session.endTime! - session.startTime) / 1000))
                      }
                    </td>
                    <td className="p-3">
                      <div className="flex flex-wrap gap-1">
                        {session.pods.map(pod => (
                          <span key={pod} className="px-1.5 py-0.5 bg-gray-700 rounded text-xs text-gray-300">{pod}</span>
                        ))}
                      </div>
                    </td>
                    <td className="p-3 text-center">
                      <span className={`px-3 py-1 rounded-full text-xs font-semibold ${
                        session.status === 'recording' ? 'bg-red-900/30 text-red-400 animate-pulse' :
                        session.status === 'completed' ? 'bg-green-900/30 text-green-400' :
                        'bg-yellow-900/30 text-yellow-400'
                      }`}>
                        {session.status}
                      </span>
                    </td>
                    <td className="p-3 text-center">
                      <button
                        onClick={() => handleDeleteSession(session.id)}
                        disabled={session.status === 'recording'}
                        className="p-1.5 text-gray-500 hover:text-red-400 hover:bg-gray-800 rounded disabled:opacity-50"
                        title="Delete session"
                      >
                        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                        </svg>
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Export / Validation */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
        <h3 className="text-lg font-semibold mb-4">Post-Recording Validation</h3>
        <p className="text-gray-400 text-sm mb-4">
          After recording, use these tools to validate data quality:
        </p>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <ValidationTool 
            title="XDF Validation" 
            description="Check stream alignment, timestamps, and drift correction"
            command="python -m synapse24.scripts.validate_sync session.xdf"
          />
          <ValidationTool 
            title="Baseline Validation" 
            description="Verify signal quality against Phase 0/1 gates"
            command="python -m synapse24.scripts.validate_baseline session.xdf"
          />
          <ValidationTool 
            title="Tier Promotion Test" 
            description="Validate T0→T1 promotion criteria met"
            command="python -m synapse24.scripts.validate_tier_promotion session.xdf"
          />
        </div>
      </div>
    </div>
  );
}

function formatTime(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
}

function RecordingStatusIndicator({ recording, elapsed }: { recording: boolean; elapsed: number }) {
  if (!recording) return null;
  
  return (
    <div className="flex items-center gap-3 px-4 py-2 bg-red-900/30 border border-red-500/30 rounded-lg">
      <span className="w-2 h-2 rounded-full bg-red-400 animate-pulse" />
      <span className="text-red-400 font-mono text-sm">
        REC {formatTime(elapsed)}
      </span>
    </div>
  );
}

interface RecordingSession {
  id: string;
  name: string;
  startTime: number;
  endTime?: number;
  duration: number | null;
  pods: string[];
  status: 'recording' | 'completed' | 'failed';
  path: string | null;
}

function ValidationTool({ title, description, command }: { title: string; description: string; command: string }) {
  return (
    <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
      <h4 className="font-medium text-gray-200 mb-1">{title}</h4>
      <p className="text-gray-400 text-sm mb-3">{description}</p>
      <div className="bg-gray-950 border border-gray-700 rounded p-3">
        <code className="font-mono text-xs text-synapse-300">{command}</code>
      </div>
    </div>
  );
}