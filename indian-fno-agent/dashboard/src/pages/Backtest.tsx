import { useState } from 'react';
import { Play, Clock, RefreshCw } from 'lucide-react';
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts';
import axios from 'axios';
import toast from 'react-hot-toast';

interface TradeLog {
  id: string;
  date: string;
  symbol: string;
  direction: string;
  entry: number;
  exit: number;
  qty: number;
  pnl: number;
  return_pct: number;
  charges: number;
}

interface BacktestResults {
  strategy: string;
  from_date: string;
  to_date: string;
  initial_capital: number;
  final_capital: number;
  net_profit: number;
  return_pct: number;
  total_trades: number;
  win_rate: number;
  profit_factor: number;
  max_drawdown: number;
  sharpe_ratio: number;
  equity_curve: { date: string; equity: number; pnl: number }[];
  trades: TradeLog[];
}

export default function Backtest() {
  const [strategy, setStrategy] = useState('Trend Breakout');
  const [fromDate, setFromDate] = useState('2024-01-01');
  const [toDate, setToDate] = useState('2024-06-30');
  const [capital, setCapital] = useState(500000);

  const [loading, setLoading] = useState(false);
  const [results, setResults] = useState<BacktestResults | null>(null);

  const handleRunBacktest = async () => {
    try {
      setLoading(true);
      toast.loading('Running walk-forward backtest simulation...', { id: 'bt' });

      const res = await axios.post('/api/v1/backtest/run', {
        strategy,
        from_date: fromDate,
        to_date: toDate,
        capital: Number(capital),
      });

      if (res.data && res.data.status === 'success') {
        setResults(res.data);
        toast.success(`Backtest complete! Net Profit: ₹${res.data.net_profit.toLocaleString('en-IN')}`, { id: 'bt' });
      }
    } catch (err) {
      console.error('Backtest error:', err);
      toast.error('Failed to run backtest.', { id: 'bt' });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-6">
      <h2 className="text-xl font-bold">Backtesting Engine</h2>

      {/* Control Form */}
      <div className="glass-card p-6">
        <h3 className="text-sm font-semibold text-gray-300 mb-4">Run Walk-Forward Backtest</h3>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
          <div>
            <label className="block text-xs text-gray-400 mb-1">Strategy</label>
            <select
              value={strategy}
              onChange={(e) => setStrategy(e.target.value)}
              className="w-full px-3 py-2 rounded-lg bg-navy-900 border border-gray-700 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
            >
              <option>Trend Breakout</option>
              <option>VWAP Reversal</option>
              <option>Options Momentum</option>
              <option>Short Premium</option>
            </select>
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">From Date</label>
            <input
              type="date"
              value={fromDate}
              onChange={(e) => setFromDate(e.target.value)}
              className="w-full px-3 py-2 rounded-lg bg-navy-900 border border-gray-700 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
            />
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">To Date</label>
            <input
              type="date"
              value={toDate}
              onChange={(e) => setToDate(e.target.value)}
              className="w-full px-3 py-2 rounded-lg bg-navy-900 border border-gray-700 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
            />
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">Capital (₹)</label>
            <input
              type="number"
              value={capital}
              onChange={(e) => setCapital(Number(e.target.value))}
              className="w-full px-3 py-2 rounded-lg bg-navy-900 border border-gray-700 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
            />
          </div>
        </div>

        <button
          onClick={handleRunBacktest}
          disabled={loading}
          className="flex items-center gap-2 px-6 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-sm font-medium text-white transition-colors cursor-pointer"
        >
          {loading ? (
            <>
              <RefreshCw className="w-4 h-4 animate-spin" /> Simulating...
            </>
          ) : (
            <>
              <Play className="w-4 h-4" /> Run Backtest
            </>
          )}
        </button>
      </div>

      {/* Results View */}
      {results ? (
        <div className="space-y-6 animate-slide-in">
          {/* Summary Cards */}
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
            <div className="stat-card">
              <p className="text-xs text-gray-400 mb-1">Net Profit</p>
              <p className={`text-lg font-bold ${results.net_profit >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                +₹{results.net_profit.toLocaleString('en-IN')}
              </p>
              <p className="text-[10px] text-emerald-500">+{results.return_pct}% return</p>
            </div>

            <div className="stat-card">
              <p className="text-xs text-gray-400 mb-1">Win Rate</p>
              <p className="text-lg font-bold text-white">{results.win_rate}%</p>
              <p className="text-[10px] text-gray-500">{results.total_trades} total trades</p>
            </div>

            <div className="stat-card">
              <p className="text-xs text-gray-400 mb-1">Profit Factor</p>
              <p className="text-lg font-bold text-emerald-400">{results.profit_factor}</p>
              <p className="text-[10px] text-gray-500">Gross Win / Loss</p>
            </div>

            <div className="stat-card">
              <p className="text-xs text-gray-400 mb-1">Max Drawdown</p>
              <p className="text-lg font-bold text-red-400">₹{results.max_drawdown.toLocaleString('en-IN')}</p>
              <p className="text-[10px] text-red-400/70">Peak-to-trough drop</p>
            </div>

            <div className="stat-card">
              <p className="text-xs text-gray-400 mb-1">Sharpe Ratio</p>
              <p className="text-lg font-bold text-blue-400">{results.sharpe_ratio}</p>
              <p className="text-[10px] text-gray-500">Risk-adjusted return</p>
            </div>

            <div className="stat-card">
              <p className="text-xs text-gray-400 mb-1">Ending Capital</p>
              <p className="text-lg font-bold text-white">₹{results.final_capital.toLocaleString('en-IN')}</p>
              <p className="text-[10px] text-gray-500">Final Portfolio Value</p>
            </div>
          </div>

          {/* Equity Curve Chart */}
          <div className="glass-card p-5">
            <h3 className="text-sm font-semibold text-gray-300 mb-4">Equity Curve & Portfolio Growth</h3>
            <ResponsiveContainer width="100%" height={260}>
              <AreaChart data={results.equity_curve} margin={{ top: 10, right: 10, left: 10, bottom: 5 }}>
                <defs>
                  <linearGradient id="equityGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#10b981" stopOpacity={0.3} />
                    <stop offset="100%" stopColor="#10b981" stopOpacity={0.0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                <XAxis dataKey="date" stroke="#6b7280" fontSize={11} />
                <YAxis stroke="#6b7280" fontSize={11} tickFormatter={(v: number) => `₹${(v / 1000).toFixed(0)}k`} />
                <Tooltip
                  contentStyle={{ background: '#111827', border: '1px solid #374151', borderRadius: 8, fontSize: 12 }}
                  formatter={(val: number) => [`₹${val.toLocaleString('en-IN')}`, 'Portfolio Value']}
                />
                <Area type="monotone" dataKey="equity" stroke="#10b981" strokeWidth={2} fill="url(#equityGrad)" />
              </AreaChart>
            </ResponsiveContainer>
          </div>

          {/* Simulated Trade Logs */}
          <div className="glass-card p-5">
            <h3 className="text-sm font-semibold text-gray-300 mb-4">Backtested Trade Logs</h3>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-gray-800 text-gray-400 uppercase">
                    <th className="text-left p-3">Trade ID</th>
                    <th className="text-left p-3">Date</th>
                    <th className="text-left p-3">Symbol</th>
                    <th className="text-center p-3">Side</th>
                    <th className="text-right p-3">Entry</th>
                    <th className="text-right p-3">Exit</th>
                    <th className="text-right p-3">Return</th>
                    <th className="text-right p-3">Net P&L</th>
                  </tr>
                </thead>
                <tbody>
                  {results.trades.map((t) => (
                    <tr key={t.id} className="border-b border-gray-800/40 hover:bg-navy-700/30">
                      <td className="p-3 font-mono text-gray-400">{t.id}</td>
                      <td className="p-3 text-gray-300">{t.date}</td>
                      <td className="p-3 font-medium text-white">{t.symbol}</td>
                      <td className="p-3 text-center">
                        <span className="badge badge-buy">{t.direction}</span>
                      </td>
                      <td className="p-3 text-right text-gray-300">₹{t.entry}</td>
                      <td className="p-3 text-right text-gray-300">₹{t.exit}</td>
                      <td className={`p-3 text-right font-medium ${t.return_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                        {t.return_pct >= 0 ? '+' : ''}{t.return_pct}%
                      </td>
                      <td className={`p-3 text-right font-bold ${t.pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                        {t.pnl >= 0 ? '+' : ''}₹{t.pnl.toLocaleString('en-IN')}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      ) : (
        <div className="glass-card p-12 text-center text-gray-500">
          <Clock className="w-10 h-10 mx-auto mb-3 opacity-30 text-blue-400" />
          <p className="text-sm font-medium text-gray-300">No backtest results generated yet</p>
          <p className="text-xs text-gray-500 mt-1">Configure your strategy parameters above and click "Run Backtest".</p>
        </div>
      )}
    </div>
  );
}
