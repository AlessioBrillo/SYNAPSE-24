/** React Hooks for SYNAPSE-24 Dashboard Data */

import { useState, useEffect, useCallback, useRef } from 'react';
import { api } from '../api/client';
import type {
  SystemStatus,
  SyncStatus,
  PodSignalQuality,
  LiveDataPoint,
  HardwareConfig,
  Tier,
  TierChangeRequest,
  RecordingControlRequest,
} from '../types';

// ==================== Generic Hooks ====================

function useAsync<T>(
  asyncFn: () => Promise<T>,
  deps: React.DependencyList = [],
  intervalMs?: number
): { data: T | null; error: Error | null; loading: boolean; refresh: () => Promise<void> } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const mountedRef = useRef(true);

  const execute = useCallback(async () => {
    if (!mountedRef.current) return;
    try {
      setLoading(true);
      setError(null);
      const result = await asyncFn();
      if (mountedRef.current) {
        setData(result);
      }
    } catch (err) {
      if (mountedRef.current) {
        setError(err instanceof Error ? err : new Error('Unknown error'));
      }
    } finally {
      if (mountedRef.current) {
        setLoading(false);
      }
    }
  }, [asyncFn]);

  useEffect(() => {
    mountedRef.current = true;
    execute();

    let intervalId: ReturnType<typeof setInterval> | null = null;
    if (intervalMs && intervalMs > 0) {
      intervalId = setInterval(execute, intervalMs);
    }

    return () => {
      mountedRef.current = false;
      if (intervalId) clearInterval(intervalId);
    };
  }, [execute, intervalMs, ...deps]);

  return { data, error, loading, refresh: execute };
}

// ==================== Specific Data Hooks ====================

export function useSystemStatus(intervalMs: number = 2000) {
  return useAsync(() => api.getStatus(), [], intervalMs);
}

export function useSyncStatus(intervalMs: number = 5000) {
  return useAsync(() => api.getSyncStatus(), [], intervalMs);
}

export function useSignalQuality(podId: string, intervalMs: number = 3000) {
  return useAsync(() => api.getSignalQuality(podId), [podId], intervalMs);
}

export function useLiveData(podId: string, durationS: number = 10, intervalMs: number = 1000) {
  return useAsync(() => api.getLiveData(podId, durationS), [podId, durationS], intervalMs);
}

export function useHardwareConfig() {
  return useAsync(() => api.getConfig(), []);
}

// ==================== Control Hooks ====================

export function useTierControl() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const changeTier = async (targetTier: Tier, reason: string = 'user_requested') => {
    setLoading(true);
    setError(null);
    try {
      const request: TierChangeRequest = { target_tier: targetTier, reason };
      await api.changeTier(request);
    } catch (err) {
      setError(err instanceof Error ? err : new Error('Failed to change tier'));
      throw err;
    } finally {
      setLoading(false);
    }
  };

  return { changeTier, loading, error };
}

export function useRecordingControl() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const startRecording = async (sessionName?: string, durationS?: number) => {
    setLoading(true);
    setError(null);
    try {
      const request: RecordingControlRequest = { action: 'start', session_name: sessionName, duration_s: durationS };
      return await api.controlRecording(request);
    } catch (err) {
      setError(err instanceof Error ? err : new Error('Failed to start recording'));
      throw err;
    } finally {
      setLoading(false);
    }
  };

  const stopRecording = async (sessionName?: string) => {
    setLoading(true);
    setError(null);
    try {
      const request: RecordingControlRequest = { action: 'stop', session_name: sessionName };
      return await api.controlRecording(request);
    } catch (err) {
      setError(err instanceof Error ? err : new Error('Failed to stop recording'));
      throw err;
    } finally {
      setLoading(false);
    }
  };

  return { startRecording, stopRecording, loading, error };
}

// ==================== WebSocket Hook ====================

export function useWebSocket(
  onMessage: (data: any) => void,
  url: string = '/ws/live'
) {
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    const wsUrl = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}${url}`;
    wsRef.current = new WebSocket(wsUrl);

    wsRef.current.onopen = () => {
      setConnected(true);
    };

    wsRef.current.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        onMessage(data);
      } catch (err) {
        console.error('WebSocket message parse error:', err);
      }
    };

    wsRef.current.onclose = () => {
      setConnected(false);
    };

    wsRef.current.onerror = () => {
      setConnected(false);
    };

    return () => {
      wsRef.current?.close();
    };
  }, [url, onMessage]);

  const send = useCallback((data: any) => {
    wsRef.current?.send(JSON.stringify(data));
  }, []);

  return { connected, send };
}