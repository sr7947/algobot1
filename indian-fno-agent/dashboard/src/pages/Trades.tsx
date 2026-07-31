import { useEffect, useState } from 'react';
import { Download, RefreshCw, Calendar, ArrowUpRight, ArrowDownRight, CheckCircle2, Coins, Zap } from 'lucide-react';
import axios from 'axios';
import { useAppStore } from '../store/useAppStore';

interface ClosedTradeItem {
  id: string | number;
  symbol: string;
  exchange: string;
  asset_class?: string;
  leverage?: string;
  direction: string;
  qty: number;
  entry: number;
  exit: number;
  pnl: number;
  charges: number;
  net_pnl: number;
  strategy: string;
  exit_reason: string;
  entry_time: string;
  exit_time: string;
}

interface AnalyticsData {
  total_trades: number;
  win_rate: number;
  total_pnl: number;
  profit_factor: number;
  avg_win: number;
  avg_loss: number;
  max_drawdown: number;
  sharpe_ratio: number;
}

export default function Trades() {
  const { assetClass } = useAppStore();
  const [trades, setTrades] = useState<ClosedTradeItem[]>([]);
  const [analytics, setAnalytics] = useState<AnalyticsData>({
    total_trades: 0,
    win_rate: 0.0,
    total_pnl: 0.0,
    profit_factor: 0.0,
    avg_win: 0.0,
    avg_loss: 0.0,
    max_drawdown: 0.0,
    sharpe_ratio: 0.0,
  });
  const [loading, setLoading] = useState(false);

  const isCrypto = assetClass === 'CRYPTO';
  const currencySymbol = isCrypto ? '$' : '₹';

  const fetchTradeData = async () => {
    try {
      setLoading(true);
      const [listRes, analyticsRes] = await Promise.all([
        axios.get(`/api/v1/trades/list-raw?asset_class=${assetClass}`),
        axios.get(`/api/v1/trades/analytics-raw?asset_class=${assetClass}`),
      ]);
      if (listRes.data && listRes.data.trades) {
        setTrades(listRes.data.trades);
      }
      if (analyticsRes.data && analyticsRes.data.status === 'success') {
        setAnalytics(analyticsRes.data);
      }
    } catch (err) {
      console.warn('Trade history fetch error:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchTradeData();
    const interval = setInterval(fetchTradeData, 3000);
    return () => clearInterval(interval);
  }, [assetClass]);

  const stats = [
    { label: 'Total Realized P&L', value: `${currencySymbol}${analytics.total_pnl.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`, positive: analytics.total_pnl >= 0 },
    { label: 'Win Rate', value: `${analytics.win_rate.toFixed(1)}%`, positive: analytics.win_rate >= 50 },
    { label: 'Profit Factor', value: `${analytics.profit_factor.toFixed(2)}`, positive: analytics.profit_factor >= 1.0 },
    { label: 'Total Closed Trades', value: `${analytics.total_trades}`, positive: true },
    { label: 'Avg Win', value: `${currencySymbol}${analytics.avg_win.toFixed(2)}`, positive: true },
    { label: 'Avg Loss', value: `${currencySymbol}${analytics.avg_loss.toFixed(2)}`, positive: false },
    { label: 'Max Drawdown', value: `${currencySymbol}${analytics.max_drawdown.toFixed(2)}`, positive: false },
    { label: 'Sharpe Ratio', value: `${analytics.sharpe_ratio.toFixed(2)}`, positive: true },
  ];

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <h2 className="text-xl font-bold flex items-center gap-2">
            {isCrypto ? (
              <span className="flex items-center gap-2 text-purple-400">
                <Coins className="w-6 h-6 text-yellow-400" /> Crypto Closed Trade History
              </span>
            ) : (
              <span>🇮🇳 Indian F&O Closed Trade History</span>
            )}
          </h2>
          <span className="text-xs text-gray-500 flex items-center gap-1">
            <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin text-blue-400' : ''}`} />
            Live sync
          </span>
        </div>

        <div className="flex items-center gap-2">
          {isCrypto && (
            <span className="text-xs px-3 py-1 rounded-full bg-purple-500/10 text-purple-400 border border-purple-500/20 font-medium flex items-center gap-1">
              <Zap className="w-3.5 h-3.5 text-yellow-400" /> Delta Default Leverage: 25x (25% Margin)
            </span>
          )}
          <a
            href={`/api/v1/trades/export?asset_class=${assetClass}`}
            download
            className="flex items-center gap-1.5 px-3 py-2 rounded-lg bg-navy-800 border border-gray-700 text-sm text-gray-300 hover:text-white transition-colors cursor-pointer"
          >
            <Download className="w-4 h-4" /> Export CSV
          </a>
        </div>
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

      {/* Closed Trades History Table */}
      <div className="glass-card overflow-hidden">
        <div className="p-4 border-b border-gray-800 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2">
            <CheckCircle2 className="w-4 h-4 text-emerald-400" />
            Closed Positions Log ({isCrypto ? 'Crypto Mode — 0.001 BTC Contract Multiplier' : 'Indian F&O Mode'})
          </h3>
          <span className="text-xs text-gray-500">{trades.length} trades recorded</span>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-800 text-gray-400 text-xs uppercase">
                <th className="text-left p-4">Symbol / Strategy</th>
                <th className="text-center p-4">Direction</th>
                <th className="text-center p-4">Leverage</th>
                <th className="text-right p-4">Qty</th>
                <th className="text-right p-4">Entry Price</th>
                <th className="text-right p-4">Exit Price</th>
                <th className="text-right p-4">Realized P&L ({isCrypto ? 'USD' : 'INR'})</th>
                <th className="text-right p-4">Charges</th>
                <th className="text-center p-4">Exit Reason</th>
                <th className="text-center p-4">Closed Time</th>
              </tr>
            </thead>
            <tbody>
              {trades.length === 0 ? (
                <tr>
                  <td colSpan={10} className="p-12 text-center text-gray-500 text-xs">
                    No closed trades recorded yet in {isCrypto ? 'Crypto' : 'Indian F&O'} mode.
                    <br />
                    When you manually close an open position or take profit, it will instantly appear here!
                  </td>
                </tr>
              ) : (
                trades.map((t) => (
                  <tr key={t.id} className={`border-b border-gray-800/50 hover:bg-navy-700/30 transition-colors ${t.pnl >= 0 ? 'bg-emerald-500/[0.03]' : 'bg-red-500/[0.03]'}`}>
                    <td className="p-4">
                      <p className="font-medium text-white text-base">{t.symbol}</p>
                      <p className="text-[11px] text-gray-400 mt-0.5">{t.strategy} • {t.exchange}</p>
                    </td>
                    <td className="p-4 text-center">
                      <span className={`badge ${t.direction === 'BUY' ? 'badge-buy' : 'badge-sell'}`}>
                        {t.direction === 'BUY' ? <ArrowUpRight className="w-3 h-3 mr-1" /> : <ArrowDownRight className="w-3 h-3 mr-1" />}
                        {t.direction}
                      </span>
                    </td>
                    <td className="p-4 text-center">
                      <span className="text-xs px-2 py-0.5 rounded bg-purple-500/10 text-purple-400 border border-purple-500/20 font-medium">
                        {t.leverage || (isCrypto ? '25x (25% Margin)' : '1x')}
                      </span>
                    </td>
                    <td className="p-4 text-right font-semibold">{t.qty}</td>
                    <td className="p-4 text-right text-gray-300">{currencySymbol}{t.entry.toFixed(2)}</td>
                    <td className="p-4 text-right font-bold text-white">{currencySymbol}{t.exit.toFixed(2)}</td>
                    <td className={`p-4 text-right font-bold text-base ${t.pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      {t.pnl >= 0 ? '+' : ''}{currencySymbol}{t.pnl.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                    </td>
                    <td className="p-4 text-right text-gray-400">{currencySymbol}{t.charges.toFixed(2)}</td>
                    <td className="p-4 text-center">
                      <span className="text-xs px-2.5 py-1 rounded-full bg-blue-500/10 text-blue-400 border border-blue-500/20 font-medium">
                        {t.exit_reason || 'MANUAL_CLOSE'}
                      </span>
                    </td>
                    <td className="p-4 text-center text-gray-400 text-xs flex items-center justify-center gap-1">
                      <Calendar className="w-3 h-3" /> {t.exit_time || 'Just now'}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
