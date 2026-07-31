"""
indicators/technical.py
────────────────────────
TechnicalAnalysisEngine — computes common technical indicators used in
Indian F&O trading from a list of Candle objects.

Libraries used:
    pandas        — DataFrame manipulation
    pandas_ta     — TA indicators (EMA, SMA, RSI, MACD, BB, ATR, ADX, VWAP)

Custom implementations:
    SuperTrend    — adjusted for NSE requirements

Dependencies (install via pip):
    pandas  pandas_ta  numpy
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pandas_ta as ta  # type: ignore[import]

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Shared Candle model (mirrors market_data.historical.Candle)
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
    """
    Most recent (scalar) value of every computed indicator.
    None means insufficient data for calculation.
    """
    # Trend
    ema_9: Optional[float] = None
    ema_21: Optional[float] = None
    ema_50: Optional[float] = None
    ema_200: Optional[float] = None
    sma_20: Optional[float] = None
    sma_50: Optional[float] = None

    # Momentum
    rsi_14: Optional[float] = None
    macd: Optional[float] = None          # MACD line
    macd_signal: Optional[float] = None   # Signal line
    macd_hist: Optional[float] = None     # Histogram

    # Volatility
    bb_upper: Optional[float] = None
    bb_mid: Optional[float] = None
    bb_lower: Optional[float] = None
    bb_bandwidth: Optional[float] = None
    atr_14: Optional[float] = None

    # Strength
    adx_14: Optional[float] = None
    di_plus: Optional[float] = None
    di_minus: Optional[float] = None

    # Volume / Price
    vwap: Optional[float] = None          # Cumulative daily VWAP

    # SuperTrend
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
    """
    Convert a list of Candle objects to a pandas DataFrame suitable for
    pandas_ta. Columns: timestamp, open, high, low, close, volume, oi.
    Index: DatetimeIndex (sorted ascending).
    """
    if not candles:
        raise ValueError("Cannot build DataFrame from an empty candles list.")

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
# SuperTrend (custom NSE-adjusted implementation)
# ─────────────────────────────────────────────────────────────────────────────

def _calculate_supertrend_series(
    df: pd.DataFrame,
    period: int = 10,
    multiplier: float = 3.0,
) -> Tuple[pd.Series, pd.Series]:
    """
    Compute SuperTrend indicator.

    Returns:
        (supertrend_line, direction_series)
        direction: 1 = uptrend (price above ST), -1 = downtrend.

    Algorithm:
        1. Compute ATR(period)
        2. UpperBand = (H+L)/2 + multiplier * ATR
        3. LowerBand = (H+L)/2 - multiplier * ATR
        4. Apply trailing-stop logic to produce the SuperTrend line.
    """
    hl2 = (df["high"] + df["low"]) / 2.0

    # True Range and ATR (manual to avoid pandas_ta dependency within here)
    high = df["high"]
    low  = df["low"]
    close_prev = df["close"].shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - close_prev).abs(),
            (low  - close_prev).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()

    basic_upper = hl2 + multiplier * atr
    basic_lower = hl2 - multiplier * atr

    n = len(df)
    final_upper = basic_upper.copy()
    final_lower = basic_lower.copy()
    supertrend  = pd.Series(index=df.index, dtype=float)
    direction   = pd.Series(index=df.index, dtype=int)

    for i in range(1, n):
        idx     = df.index[i]
        idx_p   = df.index[i - 1]
        close_i = df["close"].iloc[i]

        # Final UpperBand: only tighten (lower)
        if (
            basic_upper.iloc[i] < final_upper.iloc[i - 1]
            or df["close"].iloc[i - 1] > final_upper.iloc[i - 1]
        ):
            final_upper.iloc[i] = basic_upper.iloc[i]
        else:
            final_upper.iloc[i] = final_upper.iloc[i - 1]

        # Final LowerBand: only tighten (higher)
        if (
            basic_lower.iloc[i] > final_lower.iloc[i - 1]
            or df["close"].iloc[i - 1] < final_lower.iloc[i - 1]
        ):
            final_lower.iloc[i] = basic_lower.iloc[i]
        else:
            final_lower.iloc[i] = final_lower.iloc[i - 1]

        # SuperTrend line
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

    return supertrend, direction


# ─────────────────────────────────────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────────────────────────────────────

class TechnicalAnalysisEngine:
    """
    Computes technical indicators for F&O trading.

    All public methods operate on List[Candle] and return scalar (latest) values.
    Internally converts to pandas DataFrame.

    Usage::

        engine = TechnicalAnalysisEngine()
        indicators = engine.calculate_all(candles)
        print(indicators.rsi_14, indicators.ema_21)
    """

    # ── DataFrame Builder ─────────────────────────────────────────────────────

    @staticmethod
    def to_dataframe(candles: List[Candle]) -> pd.DataFrame:
        """Public helper to convert candles to DataFrame."""
        return _candles_to_df(candles)

    # ── Individual Indicators ─────────────────────────────────────────────────

    def ema(self, df: pd.DataFrame, period: int) -> Optional[float]:
        """Exponential Moving Average — latest value."""
        result = ta.ema(df["close"], length=period)
        return _latest(result)

    def sma(self, df: pd.DataFrame, period: int) -> Optional[float]:
        """Simple Moving Average — latest value."""
        result = ta.sma(df["close"], length=period)
        return _latest(result)

    def rsi(self, df: pd.DataFrame, period: int = 14) -> Optional[float]:
        """RSI — latest value."""
        result = ta.rsi(df["close"], length=period)
        return _latest(result)

    def macd(
        self, df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9
    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """
        MACD — returns (macd_line, signal_line, histogram) latest values.
        """
        result = ta.macd(df["close"], fast=fast, slow=slow, signal=signal)
        if result is None or result.empty:
            return None, None, None
        # pandas_ta returns columns: MACD_12_26_9, MACDh_12_26_9, MACDs_12_26_9
        cols = result.columns.tolist()
        macd_col   = next((c for c in cols if c.startswith("MACD_")), None)
        signal_col = next((c for c in cols if c.startswith("MACDs_")), None)
        hist_col   = next((c for c in cols if c.startswith("MACDh_")), None)
        return (
            _latest(result[macd_col])   if macd_col   else None,
            _latest(result[signal_col]) if signal_col else None,
            _latest(result[hist_col])   if hist_col   else None,
        )

    def bollinger_bands(
        self, df: pd.DataFrame, period: int = 20, std: float = 2.0
    ) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
        """
        Bollinger Bands — returns (upper, mid, lower, bandwidth) latest values.
        """
        result = ta.bbands(df["close"], length=period, std=std)
        if result is None or result.empty:
            return None, None, None, None
        cols = result.columns.tolist()

        def _get(prefix: str) -> Optional[float]:
            col = next((c for c in cols if c.startswith(prefix)), None)
            return _latest(result[col]) if col else None

        upper = _get("BBU_")
        mid   = _get("BBM_")
        lower = _get("BBL_")
        bw    = _get("BBB_")
        return upper, mid, lower, bw

    def atr(self, df: pd.DataFrame, period: int = 14) -> Optional[float]:
        """Average True Range — latest value."""
        result = ta.atr(df["high"], df["low"], df["close"], length=period)
        return _latest(result)

    def adx(
        self, df: pd.DataFrame, period: int = 14
    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """
        ADX + DI — returns (adx, di_plus, di_minus) latest values.
        """
        result = ta.adx(df["high"], df["low"], df["close"], length=period)
        if result is None or result.empty:
            return None, None, None
        cols = result.columns.tolist()

        def _get(prefix: str) -> Optional[float]:
            col = next((c for c in cols if c.startswith(prefix)), None)
            return _latest(result[col]) if col else None

        return _get("ADX_"), _get("DMP_"), _get("DMN_")

    def vwap(self, df: pd.DataFrame) -> Optional[float]:
        """
        Cumulative daily VWAP.

        Computed as:  VWAP = cumsum(typical_price * volume) / cumsum(volume)
        where typical_price = (high + low + close) / 3.

        This is the intraday VWAP used by traders; it resets each day.
        For daily OHLCV, treat the entire series as one "day".
        """
        df = df.copy()
        df["typical"] = (df["high"] + df["low"] + df["close"]) / 3.0
        df["tp_vol"]  = df["typical"] * df["volume"]

        # If index is a DatetimeIndex, group by date for proper daily reset
        if isinstance(df.index, pd.DatetimeIndex):
            df["date"] = df.index.date
            df["cum_tp_vol"] = df.groupby("date")["tp_vol"].cumsum()
            df["cum_vol"]    = df.groupby("date")["volume"].cumsum()
        else:
            df["cum_tp_vol"] = df["tp_vol"].cumsum()
            df["cum_vol"]    = df["volume"].cumsum()

        df["vwap"] = df["cum_tp_vol"] / df["cum_vol"].replace(0, np.nan)
        return _latest(df["vwap"])

    # ── SuperTrend ────────────────────────────────────────────────────────────

    def calculate_supertrend(
        self,
        df: pd.DataFrame,
        period: int = 10,
        multiplier: float = 3.0,
    ) -> Tuple[Optional[float], Optional[int]]:
        """
        Compute NSE-adjusted SuperTrend indicator.

        Args:
            df:         OHLCV DataFrame.
            period:     ATR period (default 10).
            multiplier: ATR multiplier (default 3.0).

        Returns:
            (supertrend_value, direction)
            direction: 1 = uptrend, -1 = downtrend, None = insufficient data.
        """
        if len(df) < period + 1:
            logger.warning(
                "Insufficient data for SuperTrend: need %d rows, got %d.",
                period + 1, len(df),
            )
            return None, None

        st_series, dir_series = _calculate_supertrend_series(df, period, multiplier)
        st_val  = _latest(st_series)
        dir_val = _latest(dir_series)
        return st_val, (int(dir_val) if dir_val is not None else None)

    # ── Main Public Method ────────────────────────────────────────────────────

    def calculate_all(self, candles: List[Candle]) -> TechnicalIndicators:
        """
        Compute all technical indicators and return the latest scalar values.

        Args:
            candles: List of Candle objects (sorted ascending by timestamp).

        Returns:
            TechnicalIndicators dataclass with latest values.

        Raises:
            ValueError: If candles list is empty.
        """
        df = _candles_to_df(candles)
        result = TechnicalIndicators()

        # ── EMA ───────────────────────────────────────────────────────────────
        result.ema_9   = self.ema(df, 9)
        result.ema_21  = self.ema(df, 21)
        result.ema_50  = self.ema(df, 50)
        result.ema_200 = self.ema(df, 200)

        # ── SMA ───────────────────────────────────────────────────────────────
        result.sma_20 = self.sma(df, 20)
        result.sma_50 = self.sma(df, 50)

        # ── RSI ───────────────────────────────────────────────────────────────
        result.rsi_14 = self.rsi(df, 14)

        # ── MACD ──────────────────────────────────────────────────────────────
        result.macd, result.macd_signal, result.macd_hist = self.macd(df)

        # ── Bollinger Bands ───────────────────────────────────────────────────
        (
            result.bb_upper, result.bb_mid,
            result.bb_lower, result.bb_bandwidth,
        ) = self.bollinger_bands(df)

        # ── ATR ───────────────────────────────────────────────────────────────
        result.atr_14 = self.atr(df, 14)

        # ── ADX ───────────────────────────────────────────────────────────────
        result.adx_14, result.di_plus, result.di_minus = self.adx(df)

        # ── VWAP ──────────────────────────────────────────────────────────────
        result.vwap = self.vwap(df)

        # ── SuperTrend ────────────────────────────────────────────────────────
        result.supertrend, result.supertrend_direction = self.calculate_supertrend(df)

        return result

    # ── Utility Methods ───────────────────────────────────────────────────────

    @staticmethod
    def detect_crossover(
        series1: pd.Series,
        series2: pd.Series,
        lookback: int = 3,
    ) -> Optional[str]:
        """
        Detect if series1 has crossed series2 within the last `lookback` bars.

        Returns:
            'bullish_cross'  — series1 crossed above series2
            'bearish_cross'  — series1 crossed below series2
            None             — no crossover detected in lookback window
        """
        # Align and trim to lookback + 1 rows
        diff = (series1 - series2).dropna()
        if len(diff) < lookback + 1:
            return None

        recent = diff.iloc[-(lookback + 1):]

        for i in range(1, len(recent)):
            prev = recent.iloc[i - 1]
            curr = recent.iloc[i]
            if prev < 0 and curr >= 0:
                return "bullish_cross"
            if prev > 0 and curr <= 0:
                return "bearish_cross"

        return None

    @staticmethod
    def detect_crossover_from_candles(
        candles: List[Candle],
        fast_period: int = 9,
        slow_period: int = 21,
        lookback: int = 3,
    ) -> Optional[str]:
        """
        Convenience wrapper: compute EMA fast/slow from candles and detect crossover.

        Returns:
            'bullish_cross', 'bearish_cross', or None.
        """
        df = _candles_to_df(candles)
        fast = ta.ema(df["close"], length=fast_period)
        slow = ta.ema(df["close"], length=slow_period)
        if fast is None or slow is None:
            return None

        diff = (fast - slow).dropna()
        return TechnicalAnalysisEngine.detect_crossover(fast, slow, lookback)

    @staticmethod
    def is_above_vwap(candle: Candle, vwap: float) -> bool:
        """
        Returns True if the candle's close is above the VWAP.

        Commonly used to confirm long bias for intraday F&O trades.
        """
        return candle.close > vwap

    @staticmethod
    def is_below_vwap(candle: Candle, vwap: float) -> bool:
        """
        Returns True if the candle's close is below the VWAP.

        Commonly used to confirm short bias for intraday F&O trades.
        """
        return candle.close < vwap
