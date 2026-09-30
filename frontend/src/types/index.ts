/** SYNAPSE-24 Dashboard TypeScript Types
 * 
 * These types mirror the Python dataclasses from:
 * - synapse24.acquisition.tier1_coordinator.Tier1Coordinator.get_status()
 * - synapse24.acquisition.lsl_gateway.LSLGateway.get_sync_status()
 * - synapse24.signal_quality base types
 */

// ==================== Core Types ====================

export type Tier = 'T0' | 'T1' | 'T2';
export type SensorStatus = 'good' | 'warning' | 'poor' | 'disconnected';
export type TransitionReason = 
  | 'immobility_detected' 
  | 'night_window_start' 
  | 'night_window_end'
  | 'movement_detected'
  | 'power_budget_exceeded'
  | 'user_requested';

// ==================== Pod Status Types ====================

export interface BasePodStatus {
  name: string;
  connected: boolean;
  streaming: boolean;
  clock_offset_ms: number;
  error_count: number;
  last_data_age_s: number | null;
}

export interface Tier1PodStatus extends BasePodStatus {
  impedance_checked: boolean;
  quality_passed: boolean;
}

export interface Tier0PodStatus extends BasePodStatus {}

export interface InEarPodStatus extends BasePodStatus {
  battery_remaining_pct: number;
}

export interface AllPodsStatus {
  tier1_pods: Record<string, Tier1PodStatus>;
  tier0_pods: Record<string, Tier0PodStatus>;
  inear_pods: Record<string, InEarPodStatus>;
}

// ==================== Sync Types ====================

export interface PodSyncInfo {
  offset_ms: number;
  drift_ppm: number;
  within_tolerance: boolean;
  tier_evaluated: Tier;
}

export interface SyncStatus {
  pods: Record<string, PodSyncInfo>;
}

// ==================== Controller Types ====================

export interface MotionGateStatus {
  sqi_min: number;
  map_max: number;
  required_consecutive_clean: number;
  sqi_staleness_max_s: number;
  latest_sqi: number | null;
  latest_map: number | null;
  latest_sqi_age_s: number | null;
  consecutive_clean: number;
  armed: boolean;
}

export interface TransitionEvent {
  from: Tier;
  to: Tier;
  reason: string;
  timestamp: number;
}

export interface ControllerStatus {
  current_tier: Tier;
  previous_tier: Tier | null;
  tier1_duration_s: number | null;
  tier2_duration_s: number | null;
  transition_count: number;
  recent_transitions: TransitionEvent[];
  motion_gate: MotionGateStatus;
}

// ==================== Main Status Type ====================

export interface SystemStatus {
  current_tier: Tier;
  tier1_pods: Record<string, Tier1PodStatus>;
  tier0_pods: Record<string, Tier0PodStatus>;
  inear_pods: Record<string, InEarPodStatus>;
  sync: SyncStatus;
  controller: ControllerStatus;
}

// ==================== Signal Quality Types ====================

export interface SignalQualityMetrics {
  // ECG
  hr_bpm?: number;
  rmssd_ms?: number;
  sdnn_ms?: number;
  snr_db?: number;
  r_peak_sensitivity?: number;
  r_peak_ppv?: number;
  
  // PPG
  sqi?: number;
  map?: number;
  
  // EEG
  impedance_kohm?: number[];
  
  // ACC
  motion_intensity?: number;
  posture?: string;
  
  // Generic
  quality: SensorStatus;
}

export interface PodSignalQuality {
  [modality: string]: SignalQualityMetrics;
}

// ==================== Live Data Types ====================

export interface LiveDataPoint {
  timestamps: number[];
  data: number[][] | Record<string, number[]>;
  channels: string[];
  sampling_rate: number;
}

// ==================== Config Types ====================

export interface PodConfig {
  pod_id: string;
  name: string;
  modalities: string[];
  tier: number;
  sampling_rate: Record<string, number>;
  channels?: Record<string, number>;
  placement?: string;
  electrode_type?: string;
}

export interface SyncConfig {
  tier_budgets: {
    T0: { max_residual_drift_ms: number; sync_interval_s: number };
    T1: { max_residual_drift_ms: number; sync_interval_s: number };
  };
}

export interface PowerBudgetConfig {
  hub_battery_mah: number;
  target_lifetime_h: number;
  tier_profiles: Record<Tier, { avg_mw: number; peak_mw: number; max_duration_h?: number }>;
}

export interface HardwareConfig {
  pods: PodConfig[];
  sync: SyncConfig;
  power_budget: PowerBudgetConfig;
}

// ==================== API Request/Response Types ====================

export interface TierChangeRequest {
  target_tier: Tier;
  reason?: string;
}

export interface RecordingControlRequest {
  action: 'start' | 'stop';
  session_name?: string;
  duration_s?: number;
}

export interface HealthCheckResponse {
  status: 'ok';
  service: string;
}