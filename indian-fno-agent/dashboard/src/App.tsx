import { useState } from 'react';
import { Routes, Route, NavLink } from 'react-router-dom';
import {
  LayoutDashboard, Zap, Briefcase, History, TestTube, Settings,
  Activity, Shield, Menu, X, Moon, Sun, Radio, Coins, TrendingUp,
} from 'lucide-react';
import Dashboard from './pages/Dashboard';
import Signals from './pages/Signals';
import Positions from './pages/Positions';
import Trades from './pages/Trades';
import Backtest from './pages/Backtest';
import SettingsPage from './pages/Settings';
import { useAppStore } from './store/useAppStore';

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
  const isConnected = true;

  const { assetClass, setAssetClass } = useAppStore();

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
            <span className="text-[10px] text-gray-500 uppercase tracking-widest">
              {assetClass === 'CRYPTO' ? 'Crypto Algo System' : 'Indian F&O System'}
            </span>
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

        <div className="absolute bottom-4 left-3 right-3 p-3 glass-card space-y-1.5">
          <div className="flex items-center justify-between text-xs">
            <span className="flex items-center gap-1.5 text-gray-400">
              <Shield className="w-3.5 h-3.5 text-emerald-400" />
              Broker:
            </span>
            <span className="font-semibold text-yellow-400">
              {assetClass === 'CRYPTO' ? 'Delta Testnet' : 'Paper Broker'}
            </span>
          </div>
          <div className="text-[10px] text-gray-500">
            {assetClass === 'CRYPTO' ? 'Crypto Futures & Options 24/7' : 'NSE / BSE Derivatives'}
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

            {/* Asset Class Toggle Switch */}
            <div className="flex items-center gap-3">
              <div className="flex items-center bg-navy-800 p-1 rounded-xl border border-gray-700/60 shadow-inner">
                <button
                  onClick={() => setAssetClass('FNO')}
                  className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
                    assetClass === 'FNO'
                      ? 'bg-blue-600 text-white shadow-md shadow-blue-600/30'
                      : 'text-gray-400 hover:text-gray-200'
                  }`}
                >
                  <TrendingUp className="w-3.5 h-3.5" /> 🇮🇳 Indian F&O
                </button>
                <button
                  onClick={() => setAssetClass('CRYPTO')}
                  className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
                    assetClass === 'CRYPTO'
                      ? 'bg-purple-600 text-white shadow-md shadow-purple-600/30'
                      : 'text-gray-400 hover:text-gray-200'
                  }`}
                >
                  <Coins className="w-3.5 h-3.5 text-yellow-400" /> 🪙 Crypto Mode
                </button>
              </div>

              {/* Status Badge */}
              <div className="hidden sm:flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-gray-800/60 border border-gray-700/50 text-xs">
                <Radio className="w-3 h-3 text-emerald-400 animate-pulse" />
                <span className="text-gray-400">
                  {assetClass === 'CRYPTO' ? 'Delta Exch' : 'NSE'}
                </span>
                <span className="text-emerald-400 font-semibold">
                  {assetClass === 'CRYPTO' ? 'Live 24/7' : 'Live'}
                </span>
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
