import { useState } from 'react';
import { Routes, Route, NavLink } from 'react-router-dom';
import {
  LayoutDashboard, Zap, Briefcase, History, TestTube, Settings,
  Activity, Shield, Menu, X, Moon, Sun, Radio,
} from 'lucide-react';
import Dashboard from './pages/Dashboard';
import Signals from './pages/Signals';
import Positions from './pages/Positions';
import Trades from './pages/Trades';
import Backtest from './pages/Backtest';
import SettingsPage from './pages/Settings';

const navItems = [
  { to: '/', icon: LayoutDashboard, label: 'Dashboard' },
  { to: '/signals', icon: Zap, label: 'Signals' },
  { to: '/positions', icon: Briefcase, label: 'Positions' },
  { to: '/trades', icon: History, label: 'Trades' },
  { to: '/backtest', icon: TestTube, label: 'Backtest' },
  { to: '/settings', icon: Settings, label: 'Settings' },
];

export default function App() {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [darkMode, setDarkMode] = useState(true);
  const isConnected = true; // Replace with real WS state

  return (
    <div className={`${darkMode ? 'dark' : ''} min-h-screen bg-navy-900 text-gray-100 flex`}>
      {/* Sidebar */}
      <aside className={`
        fixed lg:static inset-y-0 left-0 z-40 w-64 bg-navy-800 border-r border-gray-800
        transform transition-transform duration-200 ease-in-out
        ${sidebarOpen ? 'translate-x-0' : '-translate-x-full lg:translate-x-0'}
      `}>
        <div className="flex items-center gap-3 px-6 py-5 border-b border-gray-800">
          <div className="w-9 h-9 rounded-lg bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center">
            <Activity className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="text-sm font-bold tracking-wide">F&O Agent</h1>
            <span className="text-[10px] text-gray-500 uppercase tracking-widest">Trading System</span>
          </div>
        </div>

        <nav className="px-3 py-4 space-y-1">
          {navItems.map(({ to, icon: Icon, label }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              onClick={() => setSidebarOpen(false)}
              className={({ isActive }) =>
                `flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all
                ${isActive
                  ? 'bg-blue-500/15 text-blue-400 shadow-sm shadow-blue-500/10'
                  : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800/50'
                }`
              }
            >
              <Icon className="w-4 h-4" />
              {label}
            </NavLink>
          ))}
        </nav>

        <div className="absolute bottom-4 left-3 right-3 p-3 glass-card">
          <div className="flex items-center gap-2 text-xs">
            <Shield className="w-3.5 h-3.5 text-green-400" />
            <span className="text-gray-400">Mode:</span>
            <span className="font-semibold text-yellow-400">PAPER</span>
          </div>
        </div>
      </aside>

      {/* Overlay */}
      {sidebarOpen && (
        <div className="fixed inset-0 bg-black/50 z-30 lg:hidden" onClick={() => setSidebarOpen(false)} />
      )}

      {/* Main */}
      <main className="flex-1 min-h-screen">
        {/* Header */}
        <header className="sticky top-0 z-20 bg-navy-900/80 backdrop-blur-md border-b border-gray-800 px-4 lg:px-6 py-3">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <button className="lg:hidden p-1.5 rounded-lg hover:bg-gray-800" onClick={() => setSidebarOpen(!sidebarOpen)}>
                {sidebarOpen ? <X className="w-5 h-5" /> : <Menu className="w-5 h-5" />}
              </button>
              <div className="flex items-center gap-2 text-xs text-gray-400">
                <span className={`pulse-dot ${isConnected ? 'connected' : 'disconnected'}`} />
                <span>{isConnected ? 'Connected' : 'Disconnected'}</span>
              </div>
            </div>
            <div className="flex items-center gap-3">
              <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-gray-800/50 text-xs">
                <Radio className="w-3 h-3 text-green-400" />
                <span className="text-gray-400">NSE</span>
                <span className="text-green-400 font-semibold">Live</span>
              </div>
              <button
                onClick={() => setDarkMode(!darkMode)}
                className="p-2 rounded-lg hover:bg-gray-800 text-gray-400"
              >
                {darkMode ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
              </button>
            </div>
          </div>
        </header>

        {/* Content */}
        <div className="p-4 lg:p-6 animate-fade-in">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/signals" element={<Signals />} />
            <Route path="/positions" element={<Positions />} />
            <Route path="/trades" element={<Trades />} />
            <Route path="/backtest" element={<Backtest />} />
            <Route path="/settings" element={<SettingsPage />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}
