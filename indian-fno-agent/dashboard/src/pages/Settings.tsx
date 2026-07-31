import { Shield, AlertTriangle } from 'lucide-react';

export default function SettingsPage() {
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

      {/* Broker Config */}
      <div className="glass-card p-6">
        <h3 className="text-sm font-semibold text-gray-300 mb-4">Broker Configuration</h3>
        <p className="text-xs text-gray-500 mb-3">Broker credentials are managed via .env file. Restart the service after changing.</p>
        <div className="flex items-center gap-2 p-3 rounded-lg bg-navy-900/50">
          <span className="pulse-dot connected" />
          <span className="text-sm text-gray-300">Angel One SmartAPI</span>
          <span className="badge badge-approved ml-auto">Connected</span>
        </div>
      </div>
    </div>
  );
}
