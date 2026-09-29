import { Scenario } from '../types'

interface ScenarioSelectorProps {
  scenario: Scenario
  onScenarioChange: (scenario: Scenario) => void
}

const scenarios: { id: Scenario; name: string; description: string }[] = [
  {
    id: 'normal',
    name: 'Normal',
    description: 'Baseline scenario with optimal signal quality',
  },
  {
    id: 'noisy_ecg',
    name: 'Noisy ECG',
    description: 'ECG with high noise, reduced quality',
  },
  {
    id: 'motion_artifact',
    name: 'Motion Artifact',
    description: 'Increased motion artifacts, heart rate elevation',
  },
  {
    id: 'sensor_disconnect',
    name: 'Sensor Disconnect',
    description: 'Simulated sensor disconnection',
  },
]

export default function ScenarioSelector({
  scenario,
  onScenarioChange,
}: ScenarioSelectorProps) {
  return (
    <div className="space-y-6">
      <h2 className="text-2xl font-bold">Mock Scenario</h2>

      <p className="text-gray-400">
        Select a scenario to modify signal characteristics and quality metrics. Changes apply
        immediately.
      </p>

      <div className="grid grid-cols-2 gap-4">
        {scenarios.map((item) => (
          <button
            key={item.id}
            onClick={() => onScenarioChange(item.id)}
            className={`text-left p-6 rounded border-2 transition-all ${
              scenario === item.id
                ? 'border-blue-500 bg-blue-950'
                : 'border-gray-700 bg-gray-900 hover:border-gray-600'
            }`}
          >
            <div className="flex items-start justify-between">
              <div>
                <h3 className="text-lg font-semibold">{item.name}</h3>
                <p className="text-sm text-gray-400 mt-2">{item.description}</p>
              </div>
              {scenario === item.id && (
                <div className="w-4 h-4 rounded-full bg-blue-500 mt-1" />
              )}
            </div>
          </button>
        ))}
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded p-6">
        <h3 className="text-lg font-semibold mb-4">Scenario Details</h3>
        <div className="space-y-2 text-sm text-gray-300">
          {scenario === 'normal' && (
            <>
              <p>✓ Heart Rate: 72 BPM</p>
              <p>✓ All sensors at optimal quality</p>
              <p>✓ Minimal noise and artifacts</p>
            </>
          )}
          {scenario === 'noisy_ecg' && (
            <>
              <p>✓ Heart Rate: 75 BPM</p>
              <p>✓ ECG quality reduced to 35%</p>
              <p>✓ High frequency noise in ECG signal</p>
            </>
          )}
          {scenario === 'motion_artifact' && (
            <>
              <p>✓ Heart Rate: 95 BPM (elevated)</p>
              <p>✓ ACC signal shows motion (X/Y/Z spikes)</p>
              <p>✓ ECG and PPG quality degraded</p>
            </>
          )}
          {scenario === 'sensor_disconnect' && (
            <>
              <p>✓ Heart Rate: 0 BPM</p>
              <p>✓ All sensors show zero signal</p>
              <p>✓ Overall quality: 0%</p>
            </>
          )}
        </div>
      </div>
    </div>
  )
}