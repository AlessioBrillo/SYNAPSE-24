import type { SignalState, SignalData, Scenario } from './types'

const WINDOW_SIZE = 15

function generateTimestamps(duration: number, fs: number): number[] {
  const samples = duration * fs
  return Array.from({ length: samples }, (_, i) => i / fs)
}

function pruneOldData(data: SignalData, minTime: number): SignalData {
  const minIndex = data.timestamps.findIndex((t) => t >= minTime)
  if (minIndex === -1) return { timestamps: [], values: [] }
  return {
    timestamps: data.timestamps.slice(minIndex),
    values: data.values.slice(minIndex),
  }
}

function generateECG(duration: number, scenario: Scenario, heartRate: number): SignalData {
  const fs = 700
  const timestamps = generateTimestamps(duration, fs)
  const t = timestamps

  let values = t.map((ti) => {
    const beatPhase = ((ti * heartRate) / 60) % 1
    let ecg = 0

    ecg += 0.15 * Math.exp(-Math.pow((beatPhase - 0.1) * 20, 2))
    ecg += -0.2 * Math.exp(-Math.pow((beatPhase - 0.2) * 30, 2))
    ecg += 1.2 * Math.exp(-Math.pow((beatPhase - 0.22) * 40, 2))
    ecg += -0.25 * Math.exp(-Math.pow((beatPhase - 0.25) * 30, 2))
    ecg += 0.3 * Math.exp(-Math.pow((beatPhase - 0.4) * 15, 2))

    let noise = (Math.random() - 0.5) * 0.05
    if (scenario === 'noisy_ecg') noise *= 12
    if (scenario === 'motion_artifact') {
      noise += 0.3 * Math.sin(ti * 8) * Math.random()
    }

    return ecg + noise
  })

  return { timestamps, values }
}

function generateNextECGValue(
  time: number,
  scenario: Scenario,
  heartRate: number
): number {
  const beatPhase = ((time * heartRate) / 60) % 1
  let ecg = 0

  ecg += 0.15 * Math.exp(-Math.pow((beatPhase - 0.1) * 20, 2))
  ecg += -0.2 * Math.exp(-Math.pow((beatPhase - 0.2) * 30, 2))
  ecg += 1.2 * Math.exp(-Math.pow((beatPhase - 0.22) * 40, 2))
  ecg += -0.25 * Math.exp(-Math.pow((beatPhase - 0.25) * 30, 2))
  ecg += 0.3 * Math.exp(-Math.pow((beatPhase - 0.4) * 15, 2))

  let noise = (Math.random() - 0.5) * 0.05
  if (scenario === 'noisy_ecg') noise *= 12
  if (scenario === 'motion_artifact') {
    noise += 0.3 * Math.sin(time * 8) * Math.random()
  }

  return ecg + noise
}

function generatePPG(duration: number, scenario: Scenario, heartRate: number): SignalData {
  const fs = 64
  const timestamps = generateTimestamps(duration, fs)
  const t = timestamps

  if (scenario === 'sensor_disconnect') {
    return { timestamps: [], values: [] }
  }

  let values = t.map((ti) => {
    const beatPhase = ((ti * heartRate) / 60) % 1
    let ppg = 100 + 20 * Math.sin(beatPhase * Math.PI * 2)

    let noise = (Math.random() - 0.5) * 2
    if (scenario === 'motion_artifact') {
      noise += 15 * Math.sin(ti * 5) * (0.5 + Math.random())
    }

    return ppg + noise
  })

  return { timestamps, values }
}

function generateNextPPGValue(
  time: number,
  scenario: Scenario,
  heartRate: number
): number {
  if (scenario === 'sensor_disconnect') {
    return 0
  }

  const beatPhase = ((time * heartRate) / 60) % 1
  let ppg = 100 + 20 * Math.sin(beatPhase * Math.PI * 2)

  let noise = (Math.random() - 0.5) * 2
  if (scenario === 'motion_artifact') {
    noise += 15 * Math.sin(time * 5) * (0.5 + Math.random())
  }

  return ppg + noise
}

function generateEDA(duration: number, _scenario: Scenario): SignalData {
  const fs = 4
  const timestamps = generateTimestamps(duration, fs)
  const t = timestamps

  let values = t.map((ti) => {
    let eda = 2 + 0.5 * Math.sin(ti * 0.1)
    let noise = (Math.random() - 0.5) * 0.15

    return Math.max(0, eda + noise)
  })

  return { timestamps, values }
}

function generateNextEDAValue(time: number, _scenario: Scenario): number {
  let eda = 2 + 0.5 * Math.sin(time * 0.1)
  let noise = (Math.random() - 0.5) * 0.15

  return Math.max(0, eda + noise)
}

function generateACC(duration: number, scenario: Scenario, axis: 'x' | 'y' | 'z'): SignalData {
  const fs = 32
  const timestamps = generateTimestamps(duration, fs)
  const t = timestamps

  const offset = axis === 'x' ? 0 : axis === 'y' ? 0.3 : 0.6

  let values = t.map((ti) => {
    let acc = 0.05 * Math.sin(ti * 2 + offset)

    if (scenario === 'motion_artifact') {
      acc = 1.8 * Math.sin(ti * 5 + offset) + 1.2 * Math.sin(ti * 0.4 + offset)
    }

    let noise = (Math.random() - 0.5) * 0.08

    return acc + noise
  })

  return { timestamps, values }
}

function generateNextACCValue(time: number, scenario: Scenario, axis: 'x' | 'y' | 'z'): number {
  const offset = axis === 'x' ? 0 : axis === 'y' ? 0.3 : 0.6
  let acc = 0.05 * Math.sin(time * 2 + offset)

  if (scenario === 'motion_artifact') {
    acc = 1.8 * Math.sin(time * 5 + offset) + 1.2 * Math.sin(time * 0.4 + offset)
  }

  let noise = (Math.random() - 0.5) * 0.08

  return acc + noise
}

function updateBuffer(
  data: SignalData,
  newTimestamps: number[],
  newValues: number[],
  minTime: number
): SignalData {
  const combinedTimestamps = [...data.timestamps, ...newTimestamps]
  const combinedValues = [...data.values, ...newValues]
  return pruneOldData(
    { timestamps: combinedTimestamps, values: combinedValues },
    minTime
  )
}

function getHeartRate(scenario: Scenario): number {
  switch (scenario) {
    case 'motion_artifact':
      return 95
    case 'noisy_ecg':
      return 75
    case 'sensor_disconnect':
      return 72
    default:
      return 72
  }
}

function getQualityScore(scenario: Scenario, sensor: string): number {
  const baseScores: Record<string, Record<Scenario, number>> = {
    ecg: {
      normal: 0.95,
      noisy_ecg: 0.35,
      motion_artifact: 0.55,
      sensor_disconnect: 0.93,
    },
    ppg: {
      normal: 0.92,
      noisy_ecg: 0.88,
      motion_artifact: 0.4,
      sensor_disconnect: 0,
    },
    eda: {
      normal: 0.88,
      noisy_ecg: 0.86,
      motion_artifact: 0.82,
      sensor_disconnect: 0.87,
    },
    acc: {
      normal: 0.9,
      noisy_ecg: 0.88,
      motion_artifact: 0.25,
      sensor_disconnect: 0.89,
    },
  }

  return baseScores[sensor]?.[scenario] ?? 0.5
}

function getStatus(
  quality: number,
  connected: boolean
): 'good' | 'warning' | 'poor' | 'disconnected' {
  if (!connected) return 'disconnected'
  if (quality >= 0.8) return 'good'
  if (quality >= 0.5) return 'warning'
  return 'poor'
}

function isConnected(scenario: Scenario, sensor: string): boolean {
  if (scenario === 'sensor_disconnect' && sensor === 'ppg') return false
  return true
}

export function generateMockSignals(scenario: Scenario): SignalState {
  const duration = 15
  const heartRate = getHeartRate(scenario)

  const ecgConnected = isConnected(scenario, 'ecg')
  const ppgConnected = isConnected(scenario, 'ppg')
  const edaConnected = isConnected(scenario, 'eda')
  const accConnected = isConnected(scenario, 'acc')

  const ecgQuality = getQualityScore(scenario, 'ecg')
  const ppgQuality = getQualityScore(scenario, 'ppg')
  const edaQuality = getQualityScore(scenario, 'eda')
  const accQuality = getQualityScore(scenario, 'acc')

  return {
    active: false,
    scenario,
    lastUpdate: Date.now(),
    simulationTime: duration,
    ecg: generateECG(duration, scenario, heartRate),
    ppg: generatePPG(duration, scenario, heartRate),
    eda: generateEDA(duration, scenario),
    accX: generateACC(duration, scenario, 'x'),
    accY: generateACC(duration, scenario, 'y'),
    accZ: generateACC(duration, scenario, 'z'),
    heartRate,
    sensors: {
      ecg: {
        name: 'ECG',
        quality: ecgQuality,
        status: getStatus(ecgQuality, ecgConnected),
        connected: ecgConnected,
      },
      ppg: {
        name: 'PPG',
        quality: ppgQuality,
        status: getStatus(ppgQuality, ppgConnected),
        connected: ppgConnected,
      },
      eda: {
        name: 'EDA',
        quality: edaQuality,
        status: getStatus(edaQuality, edaConnected),
        connected: edaConnected,
      },
      acc: {
        name: 'ACC',
        quality: accQuality,
        status: getStatus(accQuality, accConnected),
        connected: accConnected,
      },
    },
  }
}

export function updateLiveSignal(
  state: SignalState,
  currentTimeMs: number
): SignalState {
  const deltaMs = currentTimeMs - state.lastUpdate
  const deltaSec = deltaMs / 1000
  const newSimTime = state.simulationTime + deltaSec
  const minTime = newSimTime - WINDOW_SIZE

  let newECG = state.ecg
  if (state.sensors.ecg.connected) {
    const fs = 700
    const numSamples = Math.floor(deltaSec * fs)
    const newTimestamps: number[] = []
    const newValues: number[] = []
    for (let i = 0; i < numSamples; i++) {
      const t = newSimTime - deltaSec + (i / fs)
      newTimestamps.push(t)
      newValues.push(generateNextECGValue(t, state.scenario, state.heartRate))
    }
    newECG = updateBuffer(newECG, newTimestamps, newValues, minTime)
  }

  let newPPG = state.ppg
  if (state.sensors.ppg.connected) {
    const fs = 64
    const numSamples = Math.floor(deltaSec * fs)
    const newTimestamps: number[] = []
    const newValues: number[] = []
    for (let i = 0; i < numSamples; i++) {
      const t = newSimTime - deltaSec + (i / fs)
      newTimestamps.push(t)
      newValues.push(generateNextPPGValue(t, state.scenario, state.heartRate))
    }
    newPPG = updateBuffer(newPPG, newTimestamps, newValues, minTime)
  }

  let newEDA = state.eda
  if (state.sensors.eda.connected) {
    const fs = 4
    const numSamples = Math.floor(deltaSec * fs)
    const newTimestamps: number[] = []
    const newValues: number[] = []
    for (let i = 0; i < numSamples; i++) {
      const t = newSimTime - deltaSec + (i / fs)
      newTimestamps.push(t)
      newValues.push(generateNextEDAValue(t, state.scenario))
    }
    newEDA = updateBuffer(newEDA, newTimestamps, newValues, minTime)
  }

  let newACCX = state.accX
  let newACCY = state.accY
  let newACCZ = state.accZ
  if (state.sensors.acc.connected) {
    const fs = 32
    const numSamples = Math.floor(deltaSec * fs)
    const newTimestamps: number[] = []
    const newValuesX: number[] = []
    const newValuesY: number[] = []
    const newValuesZ: number[] = []
    for (let i = 0; i < numSamples; i++) {
      const t = newSimTime - deltaSec + (i / fs)
      newTimestamps.push(t)
      newValuesX.push(generateNextACCValue(t, state.scenario, 'x'))
      newValuesY.push(generateNextACCValue(t, state.scenario, 'y'))
      newValuesZ.push(generateNextACCValue(t, state.scenario, 'z'))
    }
    newACCX = updateBuffer(newACCX, newTimestamps, newValuesX, minTime)
    newACCY = updateBuffer(newACCY, newTimestamps, newValuesY, minTime)
    newACCZ = updateBuffer(newACCZ, newTimestamps, newValuesZ, minTime)
  }

  return {
    ...state,
    lastUpdate: currentTimeMs,
    simulationTime: newSimTime,
    ecg: newECG,
    ppg: newPPG,
    eda: newEDA,
    accX: newACCX,
    accY: newACCY,
    accZ: newACCZ,
  }
}

export function resetSignalState(state: SignalState): SignalState {
  return generateMockSignals(state.scenario)
}

export function startLiveSimulation(state: SignalState): SignalState {
  return { ...state, active: true }
}

export function stopLiveSimulation(state: SignalState): SignalState {
  return { ...state, active: false }
}