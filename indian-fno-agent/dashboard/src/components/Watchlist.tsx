import { useEffect, useState } from 'react';
import { ArrowUpRight, ArrowDownRight, RefreshCw, Layers, Building2, Coins } from 'lucide-react';
import axios from 'axios';

interface QuoteItem {
  symbol: string;
  category: 'indexes' | 'stocks' | 'crypto';
  ltp: number;
  change: number;
  oi: string;
  pcr: number;
  regime: string;
  currency?: string;
}

const regimeColors: Record<string, string> = {
  'Trending Bull': 'text-emerald-400 bg-emerald-500/10',
  'Trending Bear': 'text-red-400 bg-red-500/10',
  'Range Bound': 'text-yellow-400 bg-yellow-500/10',
  'Volatile Breakout': 'text-purple-400 bg-purple-500/10',
  'Reversal': 'text-orange-400 bg-orange-500/10',
};

export default function Watchlist() {
  const [quotes, setQuotes] = useState<QuoteItem[]>([]);
  const [activeTab, setActiveTab] = useState<'all' | 'indexes' | 'stocks' | 'crypto'>('indexes');
  const [loading, setLoading] = useState(false);

  const fetchQuotes = async () => {
    try {
      setLoading(true);
      const res = await axios.get('/api/v1/market/quotes');
      if (res.data && res.data.quotes) {
        setQuotes(res.data.quotes);
      }
    } catch (err) {
      console.warn('Watchlist fetch error:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchQuotes();
    const interval = setInterval(fetchQuotes, 10000);
    return () => clearInterval(interval);
  }, []);

  const filteredQuotes = quotes.filter((q) => {
    if (activeTab === 'all') return true;
    return q.category === activeTab;
  });

  return (
    <div className="space-y-3">
      {/* Category Tabs */}
      <div className="flex items-center justify-between border-b border-gray-800 pb-2">
        <div className="flex gap-1.5 overflow-x-auto text-xs">
          <button
            onClick={() => setActiveTab('indexes')}
            className={`flex items-center gap-1 px-2.5 py-1 rounded-md transition-colors cursor-pointer ${
              activeTab === 'indexes'
                ? 'bg-blue-600 text-white font-medium'
                : 'bg-navy-800 text-gray-400 hover:text-white'
            }`}
          >
            <Layers className="w-3.5 h-3.5" /> Indexes (6)
          </button>
          <button
            onClick={() => setActiveTab('stocks')}
            className={`flex items-center gap-1 px-2.5 py-1 rounded-md transition-colors cursor-pointer ${
              activeTab === 'stocks'
                ? 'bg-blue-600 text-white font-medium'
                : 'bg-navy-800 text-gray-400 hover:text-white'
            }`}
          >
            <Building2 className="w-3.5 h-3.5" /> Top 10 Nifty
          </button>
          <button
            onClick={() => setActiveTab('crypto')}
            className={`flex items-center gap-1 px-2.5 py-1 rounded-md transition-colors cursor-pointer ${
              activeTab === 'crypto'
                ? 'bg-blue-600 text-white font-medium'
                : 'bg-navy-800 text-gray-400 hover:text-white'
            }`}
          >
            <Coins className="w-3.5 h-3.5" /> Crypto (5)
          </button>
          <button
            onClick={() => setActiveTab('all')}
            className={`px-2.5 py-1 rounded-md transition-colors cursor-pointer ${
              activeTab === 'all'
                ? 'bg-blue-600 text-white font-medium'
                : 'bg-navy-800 text-gray-400 hover:text-white'
            }`}
          >
            All (21)
          </button>
        </div>

        <span className="text-[10px] text-gray-500 flex items-center gap-1">
          <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin text-blue-400' : ''}`} />
          Live ticks
        </span>
      </div>

      {/* Table */}
      <div className="overflow-x-auto max-h-[300px] overflow-y-auto">
        <table className="w-full text-xs">
          <thead className="sticky top-0 bg-navy-900 border-b border-gray-800">
            <tr className="text-gray-500 uppercase">
              <th className="text-left py-2 px-1">Symbol</th>
              <th className="text-right py-2 px-1">LTP</th>
              <th className="text-right py-2 px-1">Chg%</th>
              <th className="text-right py-2 px-1">OI</th>
              <th className="text-right py-2 px-1">PCR</th>
              <th className="text-left py-2 px-1">Regime</th>
            </tr>
          </thead>
          <tbody>
            {filteredQuotes.map((item) => {
              const isUp = item.change >= 0;
              const curr = item.currency || '₹';
              return (
                <tr
                  key={item.symbol}
                  className="border-b border-gray-800/30 hover:bg-navy-700/30 transition-colors"
                >
                  <td className="py-2.5 px-1 font-semibold text-xs text-white">{item.symbol}</td>
                  <td className="py-2.5 px-1 text-right text-xs font-mono font-medium text-white">
                    {curr}{item.ltp.toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                  </td>
                  <td className={`py-2.5 px-1 text-right font-medium ${isUp ? 'text-emerald-400' : 'text-red-400'}`}>
                    <span className="inline-flex items-center gap-0.5 font-mono">
                      {isUp ? <ArrowUpRight className="w-3 h-3" /> : <ArrowDownRight className="w-3 h-3" />}
                      {Math.abs(item.change).toFixed(2)}%
                    </span>
                  </td>
                  <td className="py-2.5 px-1 text-right text-gray-400 font-mono">{item.oi}</td>
                  <td className={`py-2.5 px-1 text-right font-mono font-medium ${item.pcr > 1 ? 'text-emerald-400' : item.pcr < 0.85 ? 'text-red-400' : 'text-gray-300'}`}>
                    {item.pcr.toFixed(2)}
                  </td>
                  <td className="py-2.5 px-1">
                    <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${regimeColors[item.regime] || 'text-gray-400 bg-gray-800'}`}>
                      {item.regime}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
