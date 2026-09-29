import { useState, useEffect, useCallback } from 'react'
import Plot from 'react-plotly.js'
import type { SignalState } from '../types'
import { updateLiveSignal, resetSignalState } from '../mockData'

interface LiveSignalsProps {
  setSignalState: React.Dispatch<React.SetStateAction<SignalState>>
}

const WINDOW_SIZE = 15

export default function LiveSignals({ setSignalState }: LiveSignalsProps) {
  const [isPlaying, setIsPlaying] = useState(false)
  const [localState, setLocalState] = useState<SignalState | null>(null)

  useEffect(() => {
    setSignalState((prev) => {
      setLocalState(prev)
      return prev
    })
  }, [setSignalState])

  useEffect(() => {
    if (!isPlaying) return

    const interval = setInterval(() => {
      setSignalState((prevState) => {
        const newState = updateLiveSignal(prevState, Date.now())
        setLocalState(newState)
        return newState
      })
    }, 100)

    return () => clearInterval(interval)
  }, [isPlaying, setSignalState])

  const handlePause = useCallback(() => {
    setIsPlaying(false)
  }, [])

  const handleResume = useCallback(() => {
    setIsPlaying(true)
  }, [])

  const handleReset = useCallback(() => {
    setIsPlaying(false)
    setSignalState((prevState) => {
      const newState = resetSignalState(prevState)
      setLocalState(newState)
      return newState
    })
  }, [setSignalState])

  if (!localState) return <div>Loading...</div>

  const getWindowedData = (data: { timestamps: number[]; values: number[] }) => {
    const simTime = localState.simulationTime
    const minTime = Math.max(0, simTime - WINDOW_SIZE)
    
    const indices = data.timestamps.reduce<number[]>((acc, t, i) => {
      if (t >= minTime && t <= simTime) acc.push(i)
      return acc
    }, [])

    return {
      x: indices.map((i) => data.timestamps[i]),
      y: indices.map((i) => data.values[i]),
    }
  }

  const currentECG = localState.sensors.ecg.connected
    ? getWindowedData(localState.ecg)
    : null
  const currentPPG = localState.sensors.ppg.connected
    ? getWindowedData(localState.ppg)
    : null
  const currentEDA = localState.sensors.eda.connected
    ? getWindowedData(localState.eda)
    : null
  const currentACCX = localState.sensors.acc.connected
    ? getWindowedData(localState.accX)
    : null
  const currentACCY = localState.sensors.acc.connected
    ? getWindowedData(localState.accY)
    : null
  const currentACCZ = localState.sensors.acc.connected
    ? getWindowedData(localState.accZ)
    : null

  const simTime = localState.simulationTime
  const xRange: [number, number] = simTime > WINDOW_SIZE 
    ? [simTime - WINDOW_SIZE, simTime] 
    : [0, WINDOW_SIZE]

  const renderSignalCard = (
    title: string,
    data: { x: number[]; y: number[] } | null,
    isConnected: boolean
  ) => {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded overflow-hidden">
        <div className="p-3 border-b border-gray-800 flex items-center justify-between">
          <h3 className="font-semibold text-sm">{title}</h3>
          {!isConnected && (
            <span className="text-xs text-red-400 font-medium">Disconnected</span>
          )}
        </div>
        {isConnected && data && data.x.length > 0 ? (
          <Plot
            data={[
              {
                x: data.x,
                y: data.y,
                type: 'scatter',
                mode: 'lines',
                line: {
                  color:
                    title.includes('ECG')
                      ? 'rgb(239, 68, 68)'
                      : title.includes('PPG')
                        ? 'rgb(34, 197, 94)'
                        : title.includes('EDA')
                          ? 'rgb(59, 130, 246)'
                          : 'rgb(168, 85, 247)',
                  width: 1,
                },
                name: title,
              },
            ]}
            layout={{
              plot_bgcolor: 'rgb(17, 24, 39)',
              paper_bgcolor: 'rgb(5, 5, 5)',
              font: { color: 'rgb(209, 213, 219)' },
              margin: { l: 50, r: 30, t: 40, b: 40 },
              hovermode: 'x unified' as const,
              xaxis: {
                showgrid: true,
                gridcolor: 'rgb(55, 65, 81)',
                title: { text: 'Time (s)', font: { size: 10 } },
                range: xRange,
              },
              yaxis: {
                showgrid: true,
                gridcolor: 'rgb(55, 65, 81)',
              },
              height: 280,
            }}
            config={{
              responsive: true,
              displayModeBar: true,
              displaylogo: false,
            }}
          />
        ) : (
          <div className="flex items-center justify-center h-[280px] bg-gray-950">
            <div className="text-center">
              <p className="text-gray-500 text-sm">No signal</p>
              <p className="text-gray-600 text-xs mt-1">Sensor disconnected</p>
            </div>
          </div>
        )}
      </div>
    )
  }

  const renderACCCard = () => {
    const isConnected = localState.sensors.acc.connected

    return (
      <div className="bg-gray-900 border border-gray-800 rounded overflow-hidden">
        <div className="p-3 border-b border-gray-800 flex items-center justify-between">
          <h3 className="font-semibold text-sm">ACC (32 Hz)</h3>
          {!isConnected && (
            <span className="text-xs text-red-400 font-medium">Disconnected</span>
          )}
        </div>
        {isConnected && currentACCX && currentACCY && currentACCZ ? (
          <Plot
            data={[
              {
                x: currentACCX.x,
                y: currentACCX.y,
                type: 'scatter',
                mode: 'lines',
                line: { color: 'rgb(168, 85, 247)', width: 1 },
                name: 'ACC X',
              },
              {
                x: currentACCY.x,
                y: currentACCY.y,
                type: 'scatter',
                mode: 'lines',
                line: { color: 'rgb(249, 115, 22)', width: 1 },
                name: 'ACC Y',
              },
              {
                x: currentACCZ.x,
                y: currentACCZ.y,
                type: 'scatter',
                mode: 'lines',
                line: { color: 'rgb(236, 72, 153)', width: 1 },
                name: 'ACC Z',
              },
            ]}
            layout={{
              plot_bgcolor: 'rgb(17, 24, 39)',
              paper_bgcolor: 'rgb(5, 5, 5)',
              font: { color: 'rgb(209, 213, 219)' },
              margin: { l: 50, r: 30, t: 40, b: 40 },
              hovermode: 'x unified' as const,
              xaxis: {
                showgrid: true,
                gridcolor: 'rgb(55, 65, 81)',
                title: { text: 'Time (s)', font: { size: 10 } },
                range: xRange,
              },
              yaxis: {
                showgrid: true,
                gridcolor: 'rgb(55, 65, 81)',
              },
              height: 280,
            }}
            config={{
              responsive: true,
              displayModeBar: true,
              displaylogo: false,
            }}
          />
        ) : (
          <div className="flex items-center justify-center h-[280px] bg-gray-950">
            <div className="text-center">
              <p className="text-gray-500 text-sm">No signal</p>
              <p className="text-gray-600 text-xs mt-1">Sensor disconnected</p>
            </div>
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold">Live Signals</h2>
        <div className="text-sm text-gray-400">
          Time: {localState.simulationTime.toFixed(1)}s
        </div>
      </div>

      <div className="flex gap-3">
        <button
          onClick={isPlaying ? handlePause : handleResume}
          className={`px-4 py-2 rounded text-sm font-medium ${
            isPlaying
              ? 'bg-yellow-700 hover:bg-yellow-600'
              : 'bg-green-700 hover:bg-green-600'
          }`}
        >
          {isPlaying ? 'Pause' : 'Resume'}
        </button>
        <button
          onClick={handleReset}
          className="px-4 py-2 bg-blue-700 hover:bg-blue-600 rounded text-sm font-medium"
        >
          Reset
        </button>
      </div>

      <div className="grid grid-cols-2 gap-6">
        {renderSignalCard('ECG (700 Hz)', currentECG, localState.sensors.ecg.connected)}
        {renderSignalCard('PPG (64 Hz)', currentPPG, localState.sensors.ppg.connected)}
        {renderSignalCard('EDA (4 Hz)', currentEDA, localState.sensors.eda.connected)}
        {renderACCCard()}
      </div>
    </div>
  )
}