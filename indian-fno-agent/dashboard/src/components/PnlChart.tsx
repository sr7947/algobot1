import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts';

const initialData = [
  { time: '9:15', pnl: 0 },
  { time: '9:30', pnl: 0 },
  { time: '10:00', pnl: 0 },
  { time: '10:30', pnl: 0 },
  { time: '11:00', pnl: 0 },
  { time: '11:30', pnl: 0 },
  { time: '12:00', pnl: 0 },
  { time: '12:30', pnl: 0 },
  { time: '1:00', pnl: 0 },
];

export default function PnlChart() {
  const gradientColor = '#10b981';

  return (
    <ResponsiveContainer width="100%" height={220}>
      <AreaChart data={initialData} margin={{ top: 5, right: 5, left: -20, bottom: 5 }}>
        <defs>
          <linearGradient id="pnlGradient" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={gradientColor} stopOpacity={0.3} />
            <stop offset="100%" stopColor={gradientColor} stopOpacity={0.0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis
          dataKey="time"
          stroke="#6b7280"
          fontSize={10}
          tickLine={false}
          axisLine={false}
        />
        <YAxis
          stroke="#6b7280"
          fontSize={10}
          tickLine={false}
          axisLine={false}
          tickFormatter={(v: number) => `₹${v}`}
        />
        <Tooltip
          contentStyle={{
            background: '#111827',
            border: '1px solid #374151',
            borderRadius: 8,
            fontSize: 12,
            color: '#f9fafb',
          }}
          formatter={(value: number) => [
            `₹${value.toLocaleString('en-IN')}`,
            'P&L',
          ]}
          labelStyle={{ color: '#9ca3af' }}
        />
        <Area
          type="monotone"
          dataKey="pnl"
          stroke={gradientColor}
          strokeWidth={2}
          fill="url(#pnlGradient)"
          dot={false}
          activeDot={{ r: 4, fill: gradientColor, stroke: '#0a0e1a', strokeWidth: 2 }}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
