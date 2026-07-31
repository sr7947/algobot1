import { create } from 'zustand';
import axios from 'axios';

const API_BASE = '/api/v1';

interface Signal {
  id: string;
  symbol: string;
  strategy_name: string;
  direction: string;
  entry_price: number;
  stop_loss: number;
  target: number;
  confidence_score: number;
  status: string;
  regime: string;
  rationale: string[];
  created_at: string;
}

interface Position {
  id: string;
  symbol: string;
  direction: string;
  quantity: number;
  entry_price: number;
  current_price: number;
  unrealized_pnl: number;
  stop_loss: number;
  target: number;
}

interface AppState {
  // State
  signals: Signal[];
  positions: Position[];
  dailyPnl: number;
  isConnected: boolean;
  tradingMode: string;
  killSwitchActive: boolean;
  assetClass: 'FNO' | 'CRYPTO';
  watchlist: string[];

  // Actions
  setSignals: (signals: Signal[]) => void;
  addSignal: (signal: Signal) => void;
  updateSignal: (id: string, updates: Partial<Signal>) => void;
  setPositions: (positions: Position[]) => void;
  setDailyPnl: (pnl: number) => void;
  setConnected: (connected: boolean) => void;
  setMode: (mode: string) => void;
  setAssetClass: (assetClass: 'FNO' | 'CRYPTO') => void;
  toggleKillSwitch: () => void;

  // API calls
  fetchSignals: () => Promise<void>;
  fetchPositions: () => Promise<void>;
  fetchRiskState: () => Promise<void>;
}

export const useAppStore = create<AppState>((set) => ({
  signals: [],
  positions: [],
  dailyPnl: 0,
  isConnected: false,
  tradingMode: 'PAPER',
  killSwitchActive: false,
  assetClass: 'FNO',
  watchlist: ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'RELIANCE', 'HDFCBANK'],

  setSignals: (signals) => set({ signals }),
  addSignal: (signal) => set((s) => ({ signals: [signal, ...s.signals] })),
  updateSignal: (id, updates) =>
    set((s) => ({
      signals: s.signals.map((sig) => (sig.id === id ? { ...sig, ...updates } : sig)),
    })),
  setPositions: (positions) => set({ positions }),
  setDailyPnl: (dailyPnl) => set({ dailyPnl }),
  setConnected: (isConnected) => set({ isConnected }),
  setMode: (tradingMode) => set({ tradingMode }),
  setAssetClass: (assetClass) => set({ assetClass }),
  toggleKillSwitch: () => set((s) => ({ killSwitchActive: !s.killSwitchActive })),

  fetchSignals: async () => {
    try {
      const res = await axios.get(`${API_BASE}/signals?limit=50`);
      set({ signals: res.data.signals || [] });
    } catch (err) {
      console.error('Failed to fetch signals:', err);
    }
  },

  fetchPositions: async () => {
    try {
      const res = await axios.get(`${API_BASE}/positions`);
      set({ positions: res.data.positions || [] });
    } catch (err) {
      console.error('Failed to fetch positions:', err);
    }
  },

  fetchRiskState: async () => {
    try {
      const res = await axios.get(`${API_BASE}/risk/state`);
      const data = res.data;
      set({
        dailyPnl: data.daily_pnl || 0,
        killSwitchActive: data.kill_switch_active || false,
      });
    } catch (err) {
      console.error('Failed to fetch risk state:', err);
    }
  },
}));
