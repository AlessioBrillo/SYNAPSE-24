import { useState } from 'react'
import { generateMockSignals } from './mockData'
import type { Scenario, SignalState } from './types'
import Sidebar from './components/Sidebar'
import Overview from './components/Overview'
import LiveSignals from './components/LiveSignals'
import SignalQuality from './components/SignalQuality'
import ScenarioSelector from './components/ScenarioSelector'

type Page = 'overview' | 'live_signals' | 'signal_quality' | 'scenario'

export default function App() {
  const [currentPage, setCurrentPage] = useState<Page>('overview')
  const [scenario, setScenario] = useState<Scenario>('normal')
  const [signalState, setSignalState] = useState<SignalState>(() => generateMockSignals(scenario))

  const handleScenarioChange = (newScenario: Scenario) => {
    setScenario(newScenario)
    setSignalState(generateMockSignals(newScenario))
  }

  return (
    <div className="flex h-screen bg-gray-950">
      <Sidebar currentPage={currentPage} onPageChange={setCurrentPage} />

      <main className="flex-1 overflow-auto">
        <div className="p-8">
          {currentPage === 'overview' && <Overview signalState={signalState} />}
          {currentPage === 'live_signals' && (
            <LiveSignals setSignalState={setSignalState} />
          )}
          {currentPage === 'signal_quality' && <SignalQuality signalState={signalState} />}
          {currentPage === 'scenario' && (
            <ScenarioSelector scenario={scenario} onScenarioChange={handleScenarioChange} />
          )}
        </div>
      </main>
    </div>
  )
}