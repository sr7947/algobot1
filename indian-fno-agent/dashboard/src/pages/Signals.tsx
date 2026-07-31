import { useState, useEffect } from 'react';
import { Search, Filter, Zap, RefreshCw } from 'lucide-react';
import axios from 'axios';
import TradeCard from '../components/TradeCard';

interface SignalItem {
  id: string;
  time: string;
  symbol: string;
  strategy: string;
  direction: 'BUY' | 'SELL';
  entry: number;
  sl: number;
  target: number;
  rr: number;
  confidence: number;
  status: string;
  regime: string;
  reasons: string[];
  news?: string;
}

export default function Signals() {
  const [signals, setSignals] = useState<SignalItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [searchTerm, setSearchTerm] = useState('');

  const fetchSignals = async () => {
    try {
      setLoading(true);
      const res = await axios.get('/api/v1/signals');
      if (res.data && res.data.signals) {
        setSignals(res.data.signals);
      }
    } catch (err) {
      console.warn('Signals fetch error:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchSignals();
    const interval = setInterval(fetchSignals, 5000);
    return () => clearInterval(interval);
  }, []);

  const filteredSignals = signals.filter((s) =>
    s.symbol.toLowerCase().includes(searchTerm.toLowerCase())
  );

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <h2 className="text-xl font-bold">Trade Signals</h2>
          <span className="text-xs text-gray-500 flex items-center gap-1">
            <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin text-blue-400' : ''}`} />
            Live sync
          </span>
        </div>
        <div className="flex gap-2">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-500" />
            <input
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              placeholder="Search symbol..."
              className="pl-9 pr-3 py-2 rounded-lg bg-navy-800 border border-gray-700 text-sm text-gray-200 focus:outline-none focus:border-blue-500 w-48"
            />
          </div>
          <button className="flex items-center gap-1.5 px-3 py-2 rounded-lg bg-navy-800 border border-gray-700 text-sm text-gray-400 hover:text-white">
            <Filter className="w-4 h-4" /> Filters
          </button>
        </div>
      </div>

      {filteredSignals.length === 0 ? (
        <div className="glass-card p-12 text-center text-gray-500">
          <Zap className="w-10 h-10 mx-auto mb-3 opacity-30 text-blue-400" />
          <p className="text-sm font-medium text-gray-300">No trade signals generated yet</p>
          <p className="text-xs text-gray-500 mt-1">
            AI strategies (Options Momentum, Trend Breakout, VWAP Reversal) are continuously scanning live NSE feeds.
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {filteredSignals.map((sig) => (
            <TradeCard key={sig.id} signal={sig} />
          ))}
        </div>
      )}
    </div>
  );
}
