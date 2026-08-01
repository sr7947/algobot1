"""
indicators/technical.py
────────────────────────
TechnicalAnalysisEngine — computes common technical indicators used in
Indian F&O trading from a list of Candle objects.

Pure Pandas Implementation (Zero external TA C-bindings or heavy dependencies).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Shared Candle model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Candle:
    """Lightweight candle; mirrors market_data.historical.Candle."""
    symbol: str
    exchange: str
    timeframe: str
    timestamp: Any              # datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    oi: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# Result container
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TechnicalIndicators:
    """Most recent (scalar) value of every computed indicator."""
    ema_9: Optional[float] = None
    ema_21: Optional[float] = None
    ema_50: Optional[float] = None
    ema_200: Optional[float] = None
    sma_20: Optional[float] = None
    sma_50: Optional[float] = None

    rsi_14: Optional[float] = None
    macd: Optional[float] = None          # MACD line
    macd_signal: Optional[float] = None   # Signal line
    macd_hist: Optional[float] = None     # Histogram

    bb_upper: Optional[float] = None
    bb_mid: Optional[float] = None
    bb_lower: Optional[float] = None
    bb_bandwidth: Optional[float] = None
    atr_14: Optional[float] = None

    adx_14: Optional[float] = None
    di_plus: Optional[float] = None
    di_minus: Optional[float] = None

    vwap: Optional[float] = None          # Cumulative daily VWAP

    supertrend: Optional[float] = None
    supertrend_direction: Optional[int] = None  # 1=bullish, -1=bearish


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _latest(series: pd.Series) -> Optional[float]:
    """Return the last non-NaN value of a series, or None."""
    valid = series.dropna()
    if valid.empty:
        return None
    return float(valid.iloc[-1])


def _candles_to_df(candles: List[Candle]) -> pd.DataFrame:
    """Convert candles to pandas DataFrame."""
    if not candles:
        raise ValueError("Cannot build DataFrame from empty candles list.")

    records = [
        {
            "timestamp": c.timestamp,
            "open":  c.open,
            "high":  c.high,
            "low":   c.low,
            "close": c.close,
            "volume": c.volume,
            "oi": c.oi,
        }
        for c in candles
    ]
    df = pd.DataFrame(records)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df.set_index("timestamp", inplace=True)
    df.sort_index(inplace=True)
    df = df.astype({
        "open": float, "high": float, "low": float,
        "close": float, "volume": float, "oi": float,
    })
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Engine (Pure Pandas Implementation)
# ─────────────────────────────────────────────────────────────────────────────

class TechnicalAnalysisEngine:
    """Computes technical indicators with pure Pandas (100% lightweight)."""

    @staticmethod
    def to_dataframe(candles: List[Candle]) -> pd.DataFrame:
        return _candles_to_df(candles)

    def ema(self, df: pd.DataFrame, period: int) -> Optional[float]:
        res = df["close"].ewm(span=period, adjust=False).mean()
        return _latest(res)

    def sma(self, df: pd.DataFrame, period: int) -> Optional[float]:
        res = df["close"].rolling(window=period).mean()
        return _latest(res)

    def rsi(self, df: pd.DataFrame, period: int = 14) -> Optional[float]:
        delta = df["close"].diff()
        gain = (delta.where(delta > 0, 0.0)).ewm(span=period, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0.0)).ewm(span=period, adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        return _latest(rsi)

    def macd(
        self, df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9
    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
        ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        hist = macd_line - signal_line
        return _latest(macd_line), _latest(signal_line), _latest(hist)

    def bollinger_bands(
        self, df: pd.DataFrame, period: int = 20, std: float = 2.0
    ) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
        sma = df["close"].rolling(window=period).mean()
        rstd = df["close"].rolling(window=period).std()
        upper = sma + (rstd * std)
        lower = sma - (rstd * std)
        bw = (upper - lower) / sma.replace(0, np.nan)
        return _latest(upper), _latest(sma), _latest(lower), _latest(bw)

    def atr(self, df: pd.DataFrame, period: int = 14) -> Optional[float]:
        high, low, close_prev = df["high"], df["low"], df["close"].shift(1)
        tr = pd.concat([high - low, (high - close_prev).abs(), (low - close_prev).abs()], axis=1).max(axis=1)
        atr = tr.ewm(span=period, adjust=False).mean()
        return _latest(atr)

    def adx(
        self, df: pd.DataFrame, period: int = 14
    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        high, low, close_prev = df["high"], df["low"], df["close"].shift(1)
        tr = pd.concat([high - low, (high - close_prev).abs(), (low - close_prev).abs()], axis=1).max(axis=1)
        atr = tr.ewm(span=period, adjust=False).mean()

        up_move = high - high.shift(1)
        down_move = low.shift(1) - low

        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        plus_di = 100.0 * (pd.Series(plus_dm, index=df.index).ewm(span=period, adjust=False).mean() / atr.replace(0, np.nan))
        minus_di = 100.0 * (pd.Series(minus_dm, index=df.index).ewm(span=period, adjust=False).mean() / atr.replace(0, np.nan))

        dx = 100.0 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
        adx = dx.ewm(span=period, adjust=False).mean()

        return _latest(adx), _latest(plus_di), _latest(minus_di)

    def vwap(self, df: pd.DataFrame) -> Optional[float]:
        df = df.copy()
        df["typical"] = (df["high"] + df["low"] + df["close"]) / 3.0
        df["tp_vol"]  = df["typical"] * df["volume"]

        if isinstance(df.index, pd.DatetimeIndex):
            df["date"] = df.index.date
            df["cum_tp_vol"] = df.groupby("date")["tp_vol"].cumsum()
            df["cum_vol"]    = df.groupby("date")["volume"].cumsum()
        else:
            df["cum_tp_vol"] = df["tp_vol"].cumsum()
            df["cum_vol"]    = df["volume"].cumsum()

        df["vwap"] = df["cum_tp_vol"] / df["cum_vol"].replace(0, np.nan)
        return _latest(df["vwap"])

    def calculate_supertrend(
        self, df: pd.DataFrame, period: int = 10, multiplier: float = 3.0
    ) -> Tuple[Optional[float], Optional[int]]:
        if len(df) < period + 1:
            return None, None
        hl2 = (df["high"] + df["low"]) / 2.0
        tr = pd.concat([df["high"] - df["low"], (df["high"] - df["close"].shift(1)).abs(), (df["low"] - df["close"].shift(1)).abs()], axis=1).max(axis=1)
        atr = tr.ewm(span=period, adjust=False).mean()

        basic_upper = hl2 + multiplier * atr
        basic_lower = hl2 - multiplier * atr

        n = len(df)
        final_upper, final_lower = basic_upper.copy(), basic_lower.copy()
        supertrend, direction = pd.Series(index=df.index, dtype=float), pd.Series(index=df.index, dtype=int)

        for i in range(1, n):
            close_i = df["close"].iloc[i]
            if basic_upper.iloc[i] < final_upper.iloc[i - 1] or df["close"].iloc[i - 1] > final_upper.iloc[i - 1]:
                final_upper.iloc[i] = basic_upper.iloc[i]
            else:
                final_upper.iloc[i] = final_upper.iloc[i - 1]

            if basic_lower.iloc[i] > final_lower.iloc[i - 1] or df["close"].iloc[i - 1] < final_lower.iloc[i - 1]:
                final_lower.iloc[i] = basic_lower.iloc[i]
            else:
                final_lower.iloc[i] = final_lower.iloc[i - 1]

            prev_st = supertrend.iloc[i - 1] if i > 1 else final_upper.iloc[i]
            if pd.isna(prev_st):
                prev_st = final_upper.iloc[i]

            if prev_st == final_upper.iloc[i - 1]:
                if close_i <= final_upper.iloc[i]:
                    supertrend.iloc[i] = final_upper.iloc[i]
                    direction.iloc[i]  = -1
                else:
                    supertrend.iloc[i] = final_lower.iloc[i]
                    direction.iloc[i]  = 1
            else:
                if close_i >= final_lower.iloc[i]:
                    supertrend.iloc[i] = final_lower.iloc[i]
                    direction.iloc[i]  = 1
                else:
                    supertrend.iloc[i] = final_upper.iloc[i]
                    direction.iloc[i]  = -1

        st_val, dir_val = _latest(supertrend), _latest(direction)
        return st_val, (int(dir_val) if dir_val is not None else None)

    def calculate_all(self, candles: List[Candle]) -> TechnicalIndicators:
        df = _candles_to_df(candles)
        result = TechnicalIndicators()

        result.ema_9   = self.ema(df, 9)
        result.ema_21  = self.ema(df, 21)
        result.ema_50  = self.ema(df, 50)
        result.ema_200 = self.ema(df, 200)

        result.sma_20 = self.sma(df, 20)
        result.sma_50 = self.sma(df, 50)

        result.rsi_14 = self.rsi(df, 14)

        result.macd, result.macd_signal, result.macd_hist = self.macd(df)

        result.bb_upper, result.bb_mid, result.bb_lower, result.bb_bandwidth = self.bollinger_bands(df)

        result.atr_14 = self.atr(df, 14)

        result.adx_14, result.di_plus, result.di_minus = self.adx(df)

        result.vwap = self.vwap(df)

        result.supertrend, result.supertrend_direction = self.calculate_supertrend(df)

        return result

    @staticmethod
    def detect_crossover(series1: pd.Series, series2: pd.Series, lookback: int = 3) -> Optional[str]:
        diff = (series1 - series2).dropna()
        if len(diff) < lookback + 1:
            return None
        recent = diff.iloc[-(lookback + 1):]
        for i in range(1, len(recent)):
            if recent.iloc[i - 1] < 0 and recent.iloc[i] >= 0:
                return "bullish_cross"
            if recent.iloc[i - 1] > 0 and recent.iloc[i] <= 0:
                return "bearish_cross"
        return None

    @staticmethod
    def is_above_vwap(candle: Candle, vwap: float) -> bool:
        return candle.close > vwap

    @staticmethod
    def is_below_vwap(candle: Candle, vwap: float) -> bool:
        return candle.close < vwap
