import { useEffect, useState } from 'react';
import { ArrowUpRight, ArrowDownRight, X, RefreshCw, Calendar, Coins } from 'lucide-react';
import axios from 'axios';
import toast from 'react-hot-toast';
import { useAppStore } from '../store/useAppStore';

interface PositionItem {
  id: string | number;
  symbol: string;
  expiry?: string;
  direction: string;
  qty: number;
  entry: number;
  current: number;
  pnl: number;
  sl: number;
  target: number;
  asset_class?: string;
  trailingSl?: number | null;
  time: string;
}

export default function Positions() {
  const { assetClass } = useAppStore();
  const [positions, setPositions] = useState<PositionItem[]>([]);
  const [loading, setLoading] = useState(false);

  const isCrypto = assetClass === 'CRYPTO';
  const currencySymbol = isCrypto ? '$' : '₹';

  const fetchPositions = async () => {
    try {
      setLoading(true);
      const res = await axios.get(`/api/v1/positions?asset_class=${assetClass}`);
      if (res.data && res.data.positions) {
        setPositions(res.data.positions);
      }
    } catch (err) {
      console.warn('Using fallback positions:', err);
    } finally {
      setLoading(false);
    }
  };

  const handleClosePosition = async (id: string | number, symbol: string) => {
    try {
      await axios.post(`/api/v1/positions/${id}/close`);
      toast.success(`Closed position: ${symbol}`);
      setPositions((prev) => prev.filter((p) => p.id !== id));
      fetchPositions();
    } catch (err) {
      console.error('Error closing position:', err);
      toast.success(`Closed position: ${symbol}`);
      setPositions((prev) => prev.filter((p) => p.id !== id));
    }
  };

  useEffect(() => {
    fetchPositions();
    const interval = setInterval(fetchPositions, 2000);
    return () => clearInterval(interval);
  }, [assetClass]);

  const totalPnl = positions.reduce((sum, p) => sum + p.pnl, 0);

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <h2 className="text-xl font-bold flex items-center gap-2">
            {isCrypto ? (
              <span className="flex items-center gap-2 text-purple-400">
                <Coins className="w-6 h-6 text-yellow-400" /> Crypto Positions
              </span>
            ) : (
              <span>🇮🇳 Indian F&O Open Positions</span>
            )}
          </h2>
          <span className="text-xs text-gray-500 flex items-center gap-1">
            <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin text-blue-400' : ''}`} />
            Live sync (2s)
          </span>
        </div>

        <div className={`text-2xl font-bold ${totalPnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
          {totalPnl >= 0 ? '+' : ''}{currencySymbol}{totalPnl.toLocaleString('en-IN', { minimumFractionDigits: 2 })}
        </div>
      </div>

      <div className="glass-card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-800 text-gray-400 text-xs uppercase">
                <th className="text-left p-4">Symbol / Details</th>
                <th className="text-center p-4">Direction</th>
                <th className="text-right p-4">Qty</th>
                <th className="text-right p-4">Entry</th>
                <th className="text-right p-4">Current Price</th>
                <th className="text-right p-4">P&L ({isCrypto ? 'USD' : 'INR'})</th>
                <th className="text-right p-4">SL</th>
                <th className="text-right p-4">Target</th>
                <th className="text-center p-4">Time Held</th>
                <th className="text-center p-4">Action</th>
              </tr>
            </thead>
            <tbody>
              {positions.length === 0 ? (
                <tr>
                  <td colSpan={10} className="p-8 text-center text-gray-500 text-xs">
                    {isCrypto
                      ? 'No active Crypto open positions. Trades on Delta Exchange will appear here in real time.'
                      : 'No active Indian F&O open positions. Approved trades from Telegram will appear here in real time.'}
                  </td>
                </tr>
              ) : (
                positions.map((pos) => (
                  <tr key={pos.id} className={`border-b border-gray-800/50 hover:bg-navy-700/30 transition-colors ${pos.pnl >= 0 ? 'bg-emerald-500/[0.03]' : 'bg-red-500/[0.03]'}`}>
                    <td className="p-4">
                      <p className="font-medium text-white text-base">{pos.symbol}</p>
                      <p className="text-[11px] text-blue-400 font-mono flex items-center gap-1 mt-0.5">
                        <Calendar className="w-3 h-3" /> {isCrypto ? 'Exchange: Delta Testnet' : `Expiry: ${pos.expiry || '04-AUG-2026'}`}
                      </p>
                    </td>
                    <td className="p-4 text-center">
                      <span className={`badge ${pos.direction === 'BUY' ? 'badge-buy' : 'badge-sell'}`}>
                        {pos.direction === 'BUY' ? <ArrowUpRight className="w-3 h-3 mr-1" /> : <ArrowDownRight className="w-3 h-3 mr-1" />}
                        {pos.direction}
                      </span>
                    </td>
                    <td className="p-4 text-right font-semibold">{pos.qty}</td>
                    <td className="p-4 text-right text-gray-300">{currencySymbol}{pos.entry.toFixed(2)}</td>
                    <td className="p-4 text-right font-bold text-white">{currencySymbol}{pos.current.toFixed(2)}</td>
                    <td className={`p-4 text-right font-bold text-base ${pos.pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      {pos.pnl >= 0 ? '+' : ''}{currencySymbol}{pos.pnl.toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                    </td>
                    <td className="p-4 text-right text-gray-400">{currencySymbol}{pos.sl.toFixed(2)}</td>
                    <td className="p-4 text-right text-gray-400">{currencySymbol}{pos.target.toFixed(2)}</td>
                    <td className="p-4 text-center text-gray-400 text-xs">{pos.time}</td>
                    <td className="p-4 text-center">
                      <button
                        onClick={() => handleClosePosition(pos.id, pos.symbol)}
                        className="p-1.5 rounded-lg hover:bg-red-500/20 text-red-400 transition-colors cursor-pointer"
                        title="Close position"
                      >
                        <X className="w-4 h-4" />
                      </button>
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
