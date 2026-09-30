/** Main App Component */

import { useState } from 'react';
import { Sidebar } from './components/Sidebar';
import { Overview } from './components/Overview';
import { LiveSignals } from './components/LiveSignals';
import { SignalQuality } from './components/SignalQuality';
import { SyncMonitor } from './components/SyncMonitor';
import { TierControl } from './components/TierControl';
import { PodManager } from './components/PodManager';
import { RecordingPanel } from './components/RecordingPanel';
import { useSystemStatus } from './hooks/useData';
import type { SystemStatus, Tier } from './types';

const PAGES = ['overview', 'live', 'quality', 'sync', 'tiers', 'pods', 'recording'] as const;
type Page = typeof PAGES[number];

function PageContent({ status, currentPage }: { status: SystemStatus | null; currentPage: Page }) {
  switch (currentPage) {
    case 'overview':
      return <Overview status={status} />;
    case 'live':
      return <LiveSignals status={status} />;
    case 'quality':
      return <SignalQuality status={status} />;
    case 'sync':
      return <SyncMonitor status={status} />;
    case 'tiers':
      return <TierControl status={status} />;
    case 'pods':
      return <PodManager status={status} />;
    case 'recording':
      return <RecordingPanel status={status} />;
    default:
      return <Overview status={status} />;
  }
}

export default function App() {
  const [currentPage, setCurrentPage] = useState<Page>('overview');
  const { data: status, loading, error } = useSystemStatus(2000);

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-center">
          <div className="animate-spin rounded-full h-12 w-12 border-4 border-synapse-500 border-t-transparent mx-auto mb-4" />
          <p className="text-gray-400">Loading SYNAPSE-24 Dashboard...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-center p-8">
          <p className="text-red-400 mb-4">Failed to load dashboard</p>
          <p className="text-gray-500">{error.message}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-950 flex">
      <Sidebar currentPage={currentPage} onPageChange={setCurrentPage} />
      <div className="flex-1 flex flex-col overflow-hidden">
        <header className="bg-gray-900 border-b border-gray-800 px-6 py-4">
          <div className="flex items-center justify-between">
            <h1 className="text-2xl font-bold bg-gradient-to-r from-synapse-400 to-synapse-600 bg-clip-text text-transparent">
              SYNAPSE-24 Dashboard
            </h1>
            <div className="flex items-center gap-4">
              <TierBadge tier={status?.current_tier ?? 'T0'} />
              <ConnectionStatus status={status} />
            </div>
          </div>
        </header>
        <main className="flex-1 overflow-auto p-6">
          <PageContent status={status} currentPage={currentPage} />
        </main>
      </div>
    </div>
  );
}

function TierBadge({ tier }: { tier: Tier }) {
  const colors = {
    T0: 'bg-green-900 text-green-200',
    T1: 'bg-blue-900 text-blue-200',
    T2: 'bg-purple-900 text-purple-200',
  };
  return (
    <span className={`px-3 py-1 rounded-full text-xs font-semibold ${colors[tier]}`}>
      Tier {tier.slice(1)}
    </span>
  );
}

function ConnectionStatus({ status }: { status: SystemStatus | null }) {
  if (!status) return <span className="text-xs text-gray-500">Disconnected</span>;

  const allPods = [
    ...Object.values(status.tier1_pods),
    ...Object.values(status.tier0_pods),
    ...Object.values(status.inear_pods),
  ];
  const connected = allPods.filter(p => p.connected).length;
  const total = allPods.length;

  return (
    <span className="text-xs text-gray-400 flex items-center gap-2">
      <span className={`w-2 h-2 rounded-full ${connected === total ? 'bg-green-400' : 'bg-yellow-400'}`} />
      {connected}/{total} pods
    </span>
  );
}