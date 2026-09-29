import { SignalState } from '../types'

interface SignalQualityProps {
  signalState: SignalState
}

export default function SignalQuality({ signalState }: SignalQualityProps) {
  const { sensors } = signalState

  const getStatusBadge = (status: 'good' | 'warning' | 'poor' | 'disconnected') => {
    switch (status) {
      case 'good':
        return (
          <span className="px-3 py-1 bg-green-900 text-green-200 text-xs font-semibold rounded">
            Good
          </span>
        )
      case 'warning':
        return (
          <span className="px-3 py-1 bg-yellow-900 text-yellow-200 text-xs font-semibold rounded">
            Warning
          </span>
        )
      case 'poor':
        return (
          <span className="px-3 py-1 bg-red-900 text-red-200 text-xs font-semibold rounded">
            Poor
          </span>
        )
      case 'disconnected':
        return (
          <span className="px-3 py-1 bg-gray-700 text-gray-300 text-xs font-semibold rounded">
            Disconnected
          </span>
        )
    }
  }

  return (
    <div className="space-y-6">
      <h2 className="text-2xl font-bold">Signal Quality</h2>

      <div className="grid grid-cols-2 gap-4">
        {Object.entries(sensors).map(([key, sensor]) => (
          <div key={key} className="bg-gray-900 border border-gray-800 rounded p-6">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold">{sensor.name}</h3>
              {getStatusBadge(sensor.status)}
            </div>

            <div className="space-y-3">
              <div>
                <div className="flex justify-between mb-2">
                  <span className="text-sm text-gray-400">Quality Score</span>
                  <span className="text-sm font-semibold">
                    {Math.round(sensor.quality * 100)}%
                  </span>
                </div>
                <div className="w-full bg-gray-800 rounded h-3 overflow-hidden">
                  <div
                    className={`h-full transition-all ${
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
              </div>

              <div className="text-xs text-gray-400 pt-2">
                Status: <span className="text-gray-300 capitalize">{sensor.status}</span>
              </div>
            </div>
          </div>
        ))}
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded p-6">
        <h3 className="text-lg font-semibold mb-4">Quality Matrix</h3>
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-700">
              <th className="text-left py-2 text-gray-400">Sensor</th>
              <th className="text-center py-2 text-gray-400">Score</th>
              <th className="text-center py-2 text-gray-400">Status</th>
            </tr>
          </thead>
          <tbody className="space-y-1">
            {Object.entries(sensors).map(([key, sensor]) => (
              <tr key={key} className="border-b border-gray-800">
                <td className="py-3">{sensor.name}</td>
                <td className="text-center">{Math.round(sensor.quality * 100)}%</td>
                <td className="text-center">
                  <span
                    className={`text-xs font-semibold ${
                      sensor.status === 'good'
                        ? 'text-green-400'
                        : sensor.status === 'warning'
                          ? 'text-yellow-400'
                          : sensor.status === 'poor'
                            ? 'text-red-400'
                            : 'text-gray-400'
                    }`}
                  >
                    {sensor.status.toUpperCase()}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}