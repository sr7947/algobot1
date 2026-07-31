import { Shield, AlertTriangle, Coins } from 'lucide-react';
import { useAppStore } from '../store/useAppStore';

export default function SettingsPage() {
  const assetClass = useAppStore((s) => s.assetClass);
  const isCrypto = assetClass === 'CRYPTO';

  return (
    <div className="space-y-6">
      <h2 className="text-xl font-bold">Settings</h2>

      {/* Trading Mode */}
      <div className="glass-card p-6">
        <h3 className="text-sm font-semibold text-gray-300 mb-4 flex items-center gap-2">
          <Shield className="w-4 h-4 text-blue-400" /> Trading Mode
        </h3>
        <div className="flex gap-3">
          {['PAPER', 'SHADOW', 'LIVE'].map((mode) => (
            <button
              key={mode}
              className={`px-4 py-2 rounded-lg text-sm font-medium border transition-all ${
                mode === 'PAPER'
                  ? 'bg-blue-500/20 text-blue-400 border-blue-500/40'
                  : mode === 'LIVE'
                  ? 'bg-navy-800 text-red-400 border-gray-700 hover:border-red-500/40'
                  : 'bg-navy-800 text-gray-400 border-gray-700 hover:border-gray-500'
              }`}
            >
              {mode}
            </button>
          ))}
        </div>
        {
          <div className="mt-3 flex items-center gap-2 text-xs text-yellow-400 bg-yellow-500/10 px-3 py-2 rounded-lg">
            <AlertTriangle className="w-3.5 h-3.5" />
            LIVE mode requires env variable change and service restart.
          </div>
        }
      </div>

      {/* Risk Limits */}
      <div className="glass-card p-6">
        <h3 className="text-sm font-semibold text-gray-300 mb-4">Risk Limits</h3>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          {[
            { label: 'Max Risk Per Trade (%)', value: '1.0' },
            { label: 'Max Daily Loss (%)', value: '3.0' },
            { label: 'Max Open Positions', value: '5' },
            { label: 'Max Consecutive Losses', value: '3' },
            { label: 'Slippage (%)', value: '0.1' },
            { label: 'Min Liquidity Volume', value: '1000' },
          ].map(({ label, value }) => (
            <div key={label}>
              <label className="block text-xs text-gray-400 mb-1">{label}</label>
              <input
                type="text"
                defaultValue={value}
                className="w-full px-3 py-2 rounded-lg bg-navy-900 border border-gray-700 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
              />
            </div>
          ))}
        </div>
        <button className="mt-4 px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 text-sm font-medium text-white">Save Changes</button>
      </div>

      {/* Delta Crypto Leverage */}
      <div className="glass-card p-6">
        <h3 className="text-sm font-semibold text-gray-300 mb-4 flex items-center gap-2">
          <Coins className="w-4 h-4 text-yellow-400" /> Delta Crypto Default Leverage
        </h3>
        <p className="text-xs text-gray-500 mb-3">
          Order leverage applied on Delta Exchange before placing crypto futures orders.
          Default is <span className="text-yellow-400 font-medium">25x</span> (initial margin ≈ 4% of notional).
          Change via <code className="text-gray-400">DELTA_DEFAULT_LEVERAGE</code> in .env and restart.
        </p>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label className="block text-xs text-gray-400 mb-1">Default Leverage (x)</label>
            <input
              type="text"
              defaultValue="25"
              readOnly
              className="w-full px-3 py-2 rounded-lg bg-navy-900 border border-gray-700 text-sm text-gray-200 focus:outline-none focus:border-yellow-500"
            />
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">Initial Margin Fraction</label>
            <input
              type="text"
              defaultValue="4% (1/25)"
              readOnly
              className="w-full px-3 py-2 rounded-lg bg-navy-900 border border-gray-700 text-sm text-gray-200 focus:outline-none"
            />
          </div>
        </div>
        {isCrypto && (
          <div className="mt-3 flex items-center gap-2 text-xs text-yellow-400 bg-yellow-500/10 px-3 py-2 rounded-lg">
            <Coins className="w-3.5 h-3.5" />
            Crypto mode active — new Delta orders use 25x leverage by default.
          </div>
        )}
      </div>

      {/* Broker Config */}
      <div className="glass-card p-6">
        <h3 className="text-sm font-semibold text-gray-300 mb-4">Broker Configuration</h3>
        <p className="text-xs text-gray-500 mb-3">Broker credentials are managed via .env file. Restart the service after changing.</p>
        <div className="flex items-center gap-2 p-3 rounded-lg bg-navy-900/50">
          <span className="pulse-dot connected" />
          <span className="text-sm text-gray-300">
            {isCrypto ? 'Delta Exchange Testnet' : 'Angel One SmartAPI'}
          </span>
          <span className="badge badge-approved ml-auto">Connected</span>
        </div>
      </div>
    </div>
  );
}
