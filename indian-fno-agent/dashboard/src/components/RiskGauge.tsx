interface RiskGaugeProps {
  value: number;
  max: number;
  label: string;
}

export default function RiskGauge({ value, max, label }: RiskGaugeProps) {
  const pct = Math.min((value / max) * 100, 100);
  const color = pct >= 90 ? '#ef4444' : pct >= 70 ? '#f59e0b' : '#10b981';
  const circumference = 2 * Math.PI * 36;
  const dashOffset = circumference - (pct / 100) * circumference;

  return (
    <div className="flex items-center gap-4">
      <div className="relative w-20 h-20 flex-shrink-0">
        <svg className="w-20 h-20 -rotate-90" viewBox="0 0 80 80">
          {/* Background circle */}
          <circle cx="40" cy="40" r="36" fill="none" stroke="#1f2937" strokeWidth="6" />
          {/* Value arc */}
          <circle
            cx="40" cy="40" r="36" fill="none"
            stroke={color}
            strokeWidth="6"
            strokeLinecap="round"
            strokeDasharray={circumference}
            strokeDashoffset={dashOffset}
            style={{ transition: 'stroke-dashoffset 0.8s ease' }}
          />
        </svg>
        <div className="absolute inset-0 flex items-center justify-center">
          <span className="text-sm font-bold" style={{ color }}>{value}<span className="text-[10px] text-gray-500">/{max}</span></span>
        </div>
      </div>
      <div>
        <p className="text-xs text-gray-400">{label}</p>
        <p className="text-sm font-semibold" style={{ color }}>{pct.toFixed(0)}%</p>
      </div>
    </div>
  );
}
