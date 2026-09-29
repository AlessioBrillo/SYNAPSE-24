export type Scenario = 'normal' | 'noisy_ecg' | 'motion_artifact' | 'sensor_disconnect'

export interface SignalData {
  timestamps: number[]
  values: number[]
}

export interface LiveSignalState {
  active: boolean
  scenario: Scenario
  lastUpdate: number
  ecg: SignalData
  ppg: SignalData
  eda: SignalData
  accX: SignalData
  accY: SignalData
  accZ: SignalData
  heartRate: number
  sensors: {
    ecg: SensorStatus
    ppg: SensorStatus
    eda: SensorStatus
    acc: SensorStatus
  }
}

export interface SignalState {
  active: boolean
  scenario: Scenario
  lastUpdate: number
  simulationTime: number
  ecg: SignalData
  ppg: SignalData
  eda: SignalData
  accX: SignalData
  accY: SignalData
  accZ: SignalData
  heartRate: number
  sensors: {
    ecg: SensorStatus
    ppg: SensorStatus
    eda: SensorStatus
    acc: SensorStatus
  }
}

export interface SensorStatus {
  name: string
  quality: number
  status: 'good' | 'warning' | 'poor' | 'disconnected'
  connected: boolean
}