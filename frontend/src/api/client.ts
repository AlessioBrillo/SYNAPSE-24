/** API Client for SYNAPSE-24 Dashboard */

import axios, { AxiosInstance } from 'axios';
import type {
  SystemStatus,
  SyncStatus,
  PodSignalQuality,
  LiveDataPoint,
  HardwareConfig,
  TierChangeRequest,
  RecordingControlRequest,
  HealthCheckResponse,
} from '../types';

const API_BASE = '/api';

class ApiClient {
  private client: AxiosInstance;

  constructor() {
    this.client = axios.create({
      baseURL: API_BASE,
      timeout: 10000,
      headers: {
        'Content-Type': 'application/json',
      },
    });
  }

  // ==================== Health ====================
  async healthCheck(): Promise<HealthCheckResponse> {
    const response = await this.client.get('/health');
    return response.data;
  }

  // ==================== System Status ====================
  async getStatus(): Promise<SystemStatus> {
    const response = await this.client.get('/status');
    return response.data;
  }

  async getSyncStatus(): Promise<SyncStatus> {
    const response = await this.client.get('/sync');
    return response.data;
  }

  // ==================== Signal Quality ====================
  async getSignalQuality(podId: string): Promise<PodSignalQuality> {
    const response = await this.client.get(`/quality/${podId}`);
    return response.data;
  }

  // ==================== Live Data ====================
  async getLiveData(podId: string, durationS: number = 10): Promise<LiveDataPoint> {
    const response = await this.client.get(`/live/${podId}`, {
      params: { duration_s: durationS },
    });
    return response.data;
  }

  // ==================== Control ====================
  async changeTier(request: TierChangeRequest): Promise<{ success: boolean; target_tier: string }> {
    const response = await this.client.post('/control/tier', request);
    return response.data;
  }

  async controlRecording(request: RecordingControlRequest): Promise<{ success: boolean; session_name: string }> {
    const response = await this.client.post('/control/record', request);
    return response.data;
  }

  // ==================== Config ====================
  async getConfig(): Promise<HardwareConfig> {
    const response = await this.client.get('/config');
    return response.data;
  }
}

export const api = new ApiClient();