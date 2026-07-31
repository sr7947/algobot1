import { Check, X, TrendingDown, ArrowUpRight, ArrowDownRight } from 'lucide-react';

interface Signal {
  id: string;
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

export default function TradeCard({ signal }: { signal: Signal }) {
  const isBuy = signal.direction === 'BUY';
  const confPct = Math.round(signal.confidence * 100);
  const confColor = confPct >= 70 ? '#10b981' : confPct >= 50 ? '#f59e0b' : '#ef4444';

  return (
    <div className="signal-card gradient-border animate-slide-in">
      {/* Header */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-3">
          <span className={`badge ${isBuy ? 'badge-buy' : 'badge-sell'} text-xs`}>
            {isBuy ? <ArrowUpRight className="w-3 h-3 mr-0.5" /> : <ArrowDownRight className="w-3 h-3 mr-0.5" />}
            {signal.direction}
          </span>
          <h4 className="text-sm font-bold">{signal.symbol}</h4>
          <span className="text-[10px] text-gray-500 bg-gray-800 px-2 py-0.5 rounded">{signal.regime}</span>
        </div>
        <span className={`badge badge-${signal.status.toLowerCase()}`}>{signal.status}</span>
      </div>

      {/* Price Info */}
      <div className="grid grid-cols-4 gap-3 mb-3">
        <div>
          <p className="text-[10px] text-gray-500 uppercase">Entry</p>
          <p className="text-sm font-semibold">₹{signal.entry.toLocaleString('en-IN')}</p>
        </div>
        <div>
          <p className="text-[10px] text-gray-500 uppercase">Stop Loss</p>
          <p className="text-sm font-semibold text-red-400">₹{signal.sl.toLocaleString('en-IN')}</p>
        </div>
        <div>
          <p className="text-[10px] text-gray-500 uppercase">Target</p>
          <p className="text-sm font-semibold text-emerald-400">₹{signal.target.toLocaleString('en-IN')}</p>
        </div>
        <div>
          <p className="text-[10px] text-gray-500 uppercase">R:R</p>
          <p className="text-sm font-semibold text-blue-400">1:{signal.rr.toFixed(1)}</p>
        </div>
      </div>

      {/* Confidence Bar */}
      <div className="mb-3">
        <div className="flex justify-between items-center mb-1">
          <span className="text-[10px] text-gray-400">Confidence</span>
          <span className="text-xs font-semibold" style={{ color: confColor }}>{confPct}%</span>
        </div>
        <div className="confidence-bar">
          <div className="confidence-fill" style={{ width: `${confPct}%`, background: confColor }} />
        </div>
      </div>

      {/* Reasons */}
      <div className="mb-3">
        <p className="text-[10px] text-gray-500 uppercase mb-1">Reasons</p>
        <ul className="space-y-0.5">
          {signal.reasons.map((r, i) => (
            <li key={i} className="text-xs text-gray-300 flex items-start gap-1.5">
              <span className="text-blue-400 mt-0.5">•</span> {r}
            </li>
          ))}
        </ul>
      </div>

      {/* News */}
      {signal.news && (
        <p className="text-xs text-gray-500 italic mb-3">📰 {signal.news}</p>
      )}

      {/* Strategy */}
      <div className="flex items-center justify-between text-[10px] text-gray-500 mb-3">
        <span>Strategy: {signal.strategy}</span>
        <span>#{signal.id}</span>
      </div>

      {/* Action Buttons */}
      {signal.status === 'PENDING' && (
        <div className="flex gap-2">
          <button className="flex-1 flex items-center justify-center gap-1.5 py-2 rounded-lg bg-emerald-500/20 text-emerald-400 hover:bg-emerald-500/30 text-xs font-medium transition-colors">
            <Check className="w-3.5 h-3.5" /> Approve
          </button>
          <button className="flex-1 flex items-center justify-center gap-1.5 py-2 rounded-lg bg-red-500/20 text-red-400 hover:bg-red-500/30 text-xs font-medium transition-colors">
            <X className="w-3.5 h-3.5" /> Reject
          </button>
          <button className="flex items-center justify-center gap-1 px-3 py-2 rounded-lg bg-yellow-500/20 text-yellow-400 hover:bg-yellow-500/30 text-xs font-medium transition-colors">
            <TrendingDown className="w-3.5 h-3.5" /> ½
          </button>
        </div>
      )}
    </div>
  );
}
