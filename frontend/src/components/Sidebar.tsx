type Page = 'overview' | 'live_signals' | 'signal_quality' | 'scenario'

interface SidebarProps {
  currentPage: Page
  onPageChange: (page: Page) => void
}

export default function Sidebar({ currentPage, onPageChange }: SidebarProps) {
  const menuItems = [
    { id: 'overview' as const, label: 'Overview' },
    { id: 'live_signals' as const, label: 'Live Signals' },
    { id: 'signal_quality' as const, label: 'Signal Quality' },
    { id: 'scenario' as const, label: 'Mock Scenario' },
  ]

  return (
    <aside className="w-64 bg-gray-900 border-r border-gray-800 flex flex-col">
      <div className="p-6 border-b border-gray-800">
        <h1 className="text-xl font-bold text-blue-400">SYNAPSE-24</h1>
        <p className="text-xs text-gray-500 mt-1">Dashboard</p>
      </div>

      <nav className="flex-1 p-4 space-y-2">
        {menuItems.map((item) => (
          <button
            key={item.id}
            onClick={() => onPageChange(item.id)}
            className={`w-full text-left px-4 py-2 rounded transition-colors ${
              currentPage === item.id
                ? 'bg-blue-600 text-white'
                : 'text-gray-300 hover:bg-gray-800'
            }`}
          >
            {item.label}
          </button>
        ))}
      </nav>

      <div className="p-4 border-t border-gray-800 text-xs text-gray-500">
        <p>v0.0.1</p>
      </div>
    </aside>
  )
}