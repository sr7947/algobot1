import { Download, History } from 'lucide-react';
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts';

const dailyPnl = [
  { day: 'Mon', pnl: 0 },
  { day: 'Tue', pnl: 0 },
  { day: 'Wed', pnl: 0 },
  { day: 'Thu', pnl: 0 },
  { day: 'Fri', pnl: 0 },
];

const stats = [
  { label: 'Total P&L', value: '₹0.00', positive: true },
  { label: 'Win Rate', value: '0.0%', positive: true },
  { label: 'Profit Factor', value: '0.00', positive: true },
  { label: 'Total Trades', value: '0', positive: true },
  { label: 'Avg Win', value: '₹0.00', positive: true },
  { label: 'Avg Loss', value: '₹0.00', positive: false },
  { label: 'Max Drawdown', value: '₹0.00', positive: false },
  { label: 'Sharpe Ratio', value: '0.00', positive: true },
];

const strategyBreakdown = [
  { name: 'Trend Breakout', trades: 0, winRate: 0, pnl: 0 },
  { name: 'VWAP Reversal', trades: 0, winRate: 0, pnl: 0 },
  { name: 'Options Momentum', trades: 0, winRate: 0, pnl: 0 },
  { name: 'Short Premium', trades: 0, winRate: 0, pnl: 0 },
];

export default function Trades() {
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-bold">Trade History</h2>
        <button className="flex items-center gap-1.5 px-3 py-2 rounded-lg bg-navy-800 border border-gray-700 text-sm text-gray-400 hover:text-white">
          <Download className="w-4 h-4" /> Export CSV
        </button>
      </div>

      {/* Summary Stats */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {stats.map((s) => (
          <div key={s.label} className="stat-card">
            <p className="text-xs text-gray-400 mb-1">{s.label}</p>
            <p className={`text-lg font-bold ${s.positive ? 'text-emerald-400' : 'text-red-400'}`}>{s.value}</p>
          </div>
        ))}
      </div>

      {/* Daily P&L Chart */}
      <div className="glass-card p-5">
        <h3 className="text-sm font-semibold text-gray-300 mb-4">Daily P&L</h3>
        <ResponsiveContainer width="100%" height={200}>
          <BarChart data={dailyPnl}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
            <XAxis dataKey="day" stroke="#6b7280" fontSize={12} />
            <YAxis stroke="#6b7280" fontSize={12} tickFormatter={(v: number) => `₹${v}`} />
            <Tooltip
              contentStyle={{ background: '#111827', border: '1px solid #374151', borderRadius: 8, fontSize: 12 }}
              formatter={(value: number) => [`₹${value.toLocaleString('en-IN')}`, 'P&L']}
            />
            <Bar dataKey="pnl" radius={[4, 4, 0, 0]} fill="#10b981" />
          </BarChart>
        </ResponsiveContainer>
      </div>

      {/* Strategy Breakdown */}
      <div className="glass-card p-5">
        <h3 className="text-sm font-semibold text-gray-300 mb-4">Strategy Performance</h3>
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-800 text-gray-400 text-xs uppercase">
              <th className="text-left p-3">Strategy</th>
              <th className="text-center p-3">Trades</th>
              <th className="text-center p-3">Win Rate</th>
              <th className="text-right p-3">P&L</th>
            </tr>
          </thead>
          <tbody>
            {strategyBreakdown.map((s) => (
              <tr key={s.name} className="border-b border-gray-800/50 hover:bg-navy-700/30">
                <td className="p-3 font-medium">{s.name}</td>
                <td className="p-3 text-center text-gray-400">{s.trades}</td>
                <td className="p-3 text-center text-gray-400">{s.winRate}%</td>
                <td className="p-3 text-right font-bold text-gray-300">₹{s.pnl.toLocaleString('en-IN')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
