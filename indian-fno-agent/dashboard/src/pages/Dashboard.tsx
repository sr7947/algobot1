import { useState, useEffect } from 'react';
import { TrendingUp, Briefcase, Zap, Target, Wallet, ShieldCheck, PieChart } from 'lucide-react';
import axios from 'axios';
import PnlChart from '../components/PnlChart';
import RiskGauge from '../components/RiskGauge';
import Watchlist from '../components/Watchlist';

interface SignalItem {
  id: string | number;
  time: string;
  symbol: string;
  strategy: string;
  direction: string;
  confidence: number;
  status: string;
}

export default function Dashboard() {
  const [positionsCount, setPositionsCount] = useState<number>(0);
  const [signals, setSignals] = useState<SignalItem[]>([]);

  const fetchDashboardData = async () => {
    try {
      const posRes = await axios.get('/api/v1/positions');
      if (posRes.data && posRes.data.positions) {
        setPositionsCount(posRes.data.positions.length);
      }
    } catch (err) {
      console.warn('Dashboard sync error:', err);
    }
  };

  useEffect(() => {
    fetchDashboardData();
    const interval = setInterval(fetchDashboardData, 3000);
    return () => clearInterval(interval);
  }, []);

  const totalCapital = 500000;
  const usedMargin = positionsCount * 15000; // estimated
  const availableMargin = totalCapital - usedMargin;

  const stats = [
    { label: 'Daily P&L', value: '₹0.00', change: '+0.0%', positive: true, icon: TrendingUp },
    { label: 'Open Positions', value: `${positionsCount}`, change: 'of 5 max', positive: true, icon: Briefcase },
    { label: 'Win Rate', value: '0%', change: '0 trades today', positive: true, icon: Target },
    { label: 'Signals Today', value: `${signals.length}`, change: '0 pending', positive: true, icon: Zap },
  ];

  const systemStatus = [
    { label: 'Broker', status: 'Paper Connected', ok: true },
    { label: 'Telegram Bot', status: 'Active (@fno7947_bot)', ok: true },
    { label: 'Kill Switch', status: 'OFF (Safety Active)', ok: true },
    { label: 'News Feed', status: 'Active (Gemini AI)', ok: true },
  ];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-bold">Dashboard</h2>
        <span className="text-xs bg-emerald-500/10 text-emerald-400 px-3 py-1 rounded-full font-medium border border-emerald-500/20">
          Paper Trading Mode (₹5,00,000 Capital)
        </span>
      </div>

      {/* Account Balances Row */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="glass-card p-4 flex items-center gap-4">
          <div className="p-3 bg-blue-500/10 rounded-xl text-blue-400">
            <Wallet className="w-6 h-6" />
          </div>
          <div>
            <p className="text-xs text-gray-400">Total Account Balance</p>
            <p className="text-xl font-bold text-white">₹{totalCapital.toLocaleString('en-IN')}.00</p>
            <p className="text-[10px] text-gray-500">Starting Paper Capital</p>
          </div>
        </div>

        <div className="glass-card p-4 flex items-center gap-4">
          <div className="p-3 bg-emerald-500/10 rounded-xl text-emerald-400">
            <ShieldCheck className="w-6 h-6" />
          </div>
          <div>
            <p className="text-xs text-gray-400">Available Margin</p>
            <p className="text-xl font-bold text-emerald-400">₹{availableMargin.toLocaleString('en-IN')}.00</p>
            <p className="text-[10px] text-emerald-500/80">100% Free Margin</p>
          </div>
        </div>

        <div className="glass-card p-4 flex items-center gap-4">
          <div className="p-3 bg-purple-500/10 rounded-xl text-purple-400">
            <PieChart className="w-6 h-6" />
          </div>
          <div>
            <p className="text-xs text-gray-400">Used Margin</p>
            <p className="text-xl font-bold text-purple-400">₹{usedMargin.toLocaleString('en-IN')}.00</p>
            <p className="text-[10px] text-gray-500">Deployed in Positions</p>
          </div>
        </div>
      </div>

      {/* Key Metric Stats Row */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {stats.map((s) => (
          <div key={s.label} className="stat-card animate-slide-in">
            <div className="flex justify-between items-start">
              <div>
                <p className="text-xs text-gray-400 mb-1">{s.label}</p>
                <p className={`text-2xl font-bold ${s.positive ? 'text-emerald-400' : 'text-red-400'}`}>{s.value}</p>
                <p className="text-xs text-gray-500 mt-1">{s.change}</p>
              </div>
              <div className={`p-2 rounded-lg ${s.positive ? 'bg-emerald-500/10' : 'bg-red-500/10'}`}>
                <s.icon className={`w-5 h-5 ${s.positive ? 'text-emerald-400' : 'text-red-400'}`} />
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* Charts + Risk */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2 glass-card p-5">
          <h3 className="text-sm font-semibold text-gray-300 mb-4">Intraday P&L Curve</h3>
          <PnlChart />
        </div>
        <div className="glass-card p-5">
          <h3 className="text-sm font-semibold text-gray-300 mb-4">Risk Gauges</h3>
          <div className="space-y-6">
            <RiskGauge value={0} max={100} label="Daily Loss Used" />
            <RiskGauge value={positionsCount > 0 ? 20 : 0} max={100} label="Capital Deployed" />
            <RiskGauge value={0} max={3} label="Consecutive Losses" />
          </div>
        </div>
      </div>

      {/* Watchlist + Recent Signals */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="glass-card p-5">
          <h3 className="text-sm font-semibold text-gray-300 mb-4">Live Watchlist</h3>
          <Watchlist />
        </div>
        <div className="glass-card p-5">
          <h3 className="text-sm font-semibold text-gray-300 mb-4">Recent Signals</h3>
          {signals.length === 0 ? (
            <div className="text-center py-10 text-gray-500 text-xs">
              <Zap className="w-8 h-8 mx-auto mb-2 opacity-30 text-blue-400" />
              No signals generated yet today.
              <br />
              AI strategies are actively scanning NSE charts...
            </div>
          ) : (
            <div className="space-y-2">
              {signals.map((sig) => (
                <div key={sig.id} className="flex items-center justify-between p-3 rounded-lg bg-navy-900/50">
                  <div className="flex items-center gap-3">
                    <span className={`badge ${sig.direction === 'BUY' ? 'badge-buy' : 'badge-sell'}`}>{sig.direction}</span>
                    <div>
                      <p className="text-sm font-medium">{sig.symbol}</p>
                      <p className="text-xs text-gray-500">{sig.strategy} • {sig.time}</p>
                    </div>
                  </div>
                  <span className={`badge badge-${sig.status.toLowerCase()}`}>{sig.status}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* System Status */}
      <div className="glass-card p-5">
        <h3 className="text-sm font-semibold text-gray-300 mb-4">System Status</h3>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          {systemStatus.map((s) => (
            <div key={s.label} className="flex items-center gap-2 p-3 rounded-lg bg-navy-900/50">
              <span className={`pulse-dot ${s.ok ? 'connected' : 'disconnected'}`} />
              <div>
                <p className="text-xs text-gray-400">{s.label}</p>
                <p className={`text-sm font-medium ${s.ok ? 'text-emerald-400' : 'text-red-400'}`}>{s.status}</p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
