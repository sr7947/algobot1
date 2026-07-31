import { useEffect, useState } from 'react';
import { ArrowUpRight, ArrowDownRight, RefreshCw } from 'lucide-react';
import axios from 'axios';

interface QuoteItem {
  symbol: string;
  ltp: number;
  change: number;
  oi: string;
  pcr: number;
  regime: string;
}

const defaultWatchlist: QuoteItem[] = [
  { symbol: 'NIFTY', ltp: 24395.50, change: 0.32, oi: '12.5M', pcr: 1.12, regime: 'Trending Bull' },
  { symbol: 'BANKNIFTY', ltp: 51480.20, change: -0.34, oi: '8.2M', pcr: 0.89, regime: 'Range Bound' },
  { symbol: 'FINNIFTY', ltp: 22340.55, change: 0.52, oi: '3.1M', pcr: 1.05, regime: 'Trending Bull' },
  { symbol: 'RELIANCE', ltp: 1301.90, change: -0.92, oi: '4.8M', pcr: 0.78, regime: 'Reversal' },
  { symbol: 'HDFCBANK', ltp: 749.90, change: 1.23, oi: '6.3M', pcr: 1.35, regime: 'Trending Bull' },
  { symbol: 'TCS', ltp: 2362.40, change: 0.15, oi: '2.1M', pcr: 0.95, regime: 'Range Bound' },
];

const regimeColors: Record<string, string> = {
  'Trending Bull': 'text-emerald-400 bg-emerald-500/10',
  'Trending Bear': 'text-red-400 bg-red-500/10',
  'Range Bound': 'text-yellow-400 bg-yellow-500/10',
  'Volatile Breakout': 'text-purple-400 bg-purple-500/10',
  'Reversal': 'text-orange-400 bg-orange-500/10',
};

export default function Watchlist() {
  const [watchlist, setWatchlist] = useState<QuoteItem[]>(defaultWatchlist);
  const [loading, setLoading] = useState(false);

  const fetchQuotes = async () => {
    try {
      setLoading(true);
      const res = await axios.get('/api/v1/market/quotes');
      if (res.data && res.data.quotes && res.data.quotes.length > 0) {
        setWatchlist(res.data.quotes);
      }
    } catch (err) {
      console.warn('Using default quotes:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchQuotes();
    const interval = setInterval(fetchQuotes, 15000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="overflow-x-auto">
      <div className="flex justify-between items-center mb-2">
        <span className="text-[10px] text-gray-500 flex items-center gap-1">
          <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin text-blue-400' : ''}`} />
          Auto-refreshing live ticks
        </span>
      </div>
      <table className="w-full text-xs">
        <thead>
          <tr className="text-gray-500 uppercase border-b border-gray-800">
            <th className="text-left py-2 px-1">Symbol</th>
            <th className="text-right py-2 px-1">LTP</th>
            <th className="text-right py-2 px-1">Chg%</th>
            <th className="text-right py-2 px-1">OI</th>
            <th className="text-right py-2 px-1">PCR</th>
            <th className="text-left py-2 px-1">Regime</th>
          </tr>
        </thead>
        <tbody>
          {watchlist.map((item) => {
            const isUp = item.change >= 0;
            return (
              <tr
                key={item.symbol}
                className="border-b border-gray-800/30 hover:bg-navy-700/30 transition-colors"
              >
                <td className="py-2.5 px-1 font-semibold text-sm">{item.symbol}</td>
                <td className="py-2.5 px-1 text-right text-sm font-medium text-white">
                  ₹{item.ltp.toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                </td>
                <td className={`py-2.5 px-1 text-right font-medium ${isUp ? 'text-emerald-400' : 'text-red-400'}`}>
                  <span className="inline-flex items-center gap-0.5">
                    {isUp ? <ArrowUpRight className="w-3 h-3" /> : <ArrowDownRight className="w-3 h-3" />}
                    {Math.abs(item.change).toFixed(2)}%
                  </span>
                </td>
                <td className="py-2.5 px-1 text-right text-gray-400">{item.oi}</td>
                <td className={`py-2.5 px-1 text-right font-medium ${item.pcr > 1 ? 'text-emerald-400' : item.pcr < 0.85 ? 'text-red-400' : 'text-gray-300'}`}>
                  {item.pcr.toFixed(2)}
                </td>
                <td className="py-2.5 px-1">
                  <span className={`text-[10px] px-1.5 py-0.5 rounded ${regimeColors[item.regime] || 'text-gray-400 bg-gray-800'}`}>
                    {item.regime}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
