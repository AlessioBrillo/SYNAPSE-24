import { SignalState } from '../types'

interface OverviewProps {
  signalState: SignalState
}

export default function Overview({ signalState }: OverviewProps) {
  const { heartRate, sensors, scenario } = signalState

  const activeSensors = Object.values(sensors).filter((s) => s.connected).length
  const connectedSensors = Object.values(sensors).filter((s) => s.connected)
  const avgQuality =
    connectedSensors.length > 0
      ? connectedSensors.reduce((sum, s) => sum + s.quality, 0) / connectedSensors.length
      : 0

  const scenarioLabels: Record<string, string> = {
    normal: 'Normal',
    noisy_ecg: 'Noisy ECG',
    motion_artifact: 'Motion Artifact',
    sensor_disconnect: 'Sensor Disconnect',
  }

  const overallStatus =
    avgQuality >= 0.8 ? 'good' : avgQuality >= 0.5 ? 'warning' : 'poor'
  const statusColor =
    overallStatus === 'good'
      ? 'text-green-400'
      : overallStatus === 'warning'
        ? 'text-yellow-400'
        : 'text-red-400'

  return (
    <div className="space-y-6">
      <h2 className="text-2xl font-bold">Overview</h2>

      <div className="grid grid-cols-4 gap-4">
        <div className="bg-gray-900 border border-gray-800 rounded p-6">
          <p className="text-gray-400 text-sm mb-2">Session Status</p>
          <p className="text-2xl font-bold text-green-400">Running</p>
        </div>

        <div className="bg-gray-900 border border-gray-800 rounded p-6">
          <p className="text-gray-400 text-sm mb-2">Heart Rate</p>
          <p className="text-2xl font-bold">{heartRate}</p>
          <p className="text-xs text-gray-500">BPM</p>
        </div>

        <div className="bg-gray-900 border border-gray-800 rounded p-6">
          <p className="text-gray-400 text-sm mb-2">Active Sensors</p>
          <p className="text-2xl font-bold">{activeSensors}/4</p>
        </div>

        <div className="bg-gray-900 border border-gray-800 rounded p-6">
          <p className="text-gray-400 text-sm mb-2">Overall Quality</p>
          <p className={`text-2xl font-bold ${statusColor}`}>
            {Math.round(avgQuality * 100)}%
          </p>
          <p className="text-xs text-gray-500 capitalize mt-1">{overallStatus}</p>
        </div>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded p-6">
        <h3 className="text-lg font-semibold mb-4">Sensor Status</h3>
        <div className="space-y-3">
          {Object.entries(sensors).map(([key, sensor]) => (
            <div key={key} className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <span className="text-gray-300 w-12">{sensor.name}</span>
                <span
                  className={`text-xs font-medium ${
                    sensor.connected ? 'text-green-400' : 'text-red-400'
                  }`}
                >
                  {sensor.connected ? 'Connected' : 'Disconnected'}
                </span>
              </div>
              <div className="flex items-center gap-3">
                <div className="w-24 bg-gray-800 rounded h-2 overflow-hidden">
                  <div
                    className={`h-full ${
                      sensor.status === 'good'
                        ? 'bg-green-500'
                        : sensor.status === 'warning'
                          ? 'bg-yellow-500'
                          : sensor.status === 'poor'
                            ? 'bg-red-500'
                            : 'bg-gray-600'
                    }`}
                    style={{ width: `${sensor.quality * 100}%` }}
                  />
                </div>
                <span className="text-xs text-gray-400 w-10 text-right">
                  {Math.round(sensor.quality * 100)}%
                </span>
              </div>
            </div>
          ))}
        </div>

        <div className="mt-6 pt-4 border-t border-gray-800">
          <p className="text-sm text-gray-400">
            Scenario: <span className="text-gray-200 font-medium">{scenarioLabels[scenario]}</span>
          </p>
        </div>
      </div>
    </div>
  )
}