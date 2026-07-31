"""
Candlestick pattern detection, support/resistance, and market structure analysis.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from core.models import Candle

logger = logging.getLogger(__name__)


@dataclass
class SupportResistance:
    """A detected support or resistance level."""
    price: float
    strength: int          # Number of touches
    level_type: str        # 'support' or 'resistance'
    first_seen_idx: int
    last_seen_idx: int


@dataclass
class BreakoutSignal:
    """Result of breakout detection."""
    is_breakout: bool
    direction: Optional[str] = None    # 'up' or 'down'
    level: Optional[float] = None
    strength: Optional[int] = None
    volume_confirmed: bool = False


@dataclass
class CandlestickPattern:
    """A detected candlestick pattern."""
    name: str
    direction: str        # 'bullish' or 'bearish'
    confidence: float     # 0-1
    bar_index: int


@dataclass
class TrendStructure:
    """Market trend structure analysis."""
    trend: str           # 'uptrend', 'downtrend', 'sideways'
    swing_highs: list[tuple[int, float]] = field(default_factory=list)
    swing_lows: list[tuple[int, float]] = field(default_factory=list)
    higher_highs: int = 0
    higher_lows: int = 0
    lower_highs: int = 0
    lower_lows: int = 0


def _candles_to_df(candles: list[Candle]) -> pd.DataFrame:
    """Convert Candle list to pandas DataFrame."""
    records = [
        {
            "time": c.time,
            "open": float(c.open),
            "high": float(c.high),
            "low": float(c.low),
            "close": float(c.close),
            "volume": int(c.volume) if c.volume else 0,
        }
        for c in candles
    ]
    df = pd.DataFrame(records)
    if not df.empty:
        df.set_index("time", inplace=True)
        df.sort_index(inplace=True)
    return df


class PatternDetector:
    """
    Detects candlestick patterns, support/resistance levels,
    breakouts, swing structure, and volume profiles.
    """

    # ── Support & Resistance ─────────────────────────────────────────

    @staticmethod
    def detect_support_resistance(
        candles: list[Candle],
        lookback: int = 20,
        tolerance_pct: float = 0.3,
    ) -> list[SupportResistance]:
        """
        Detect support and resistance levels using pivot high/low clustering.

        Args:
            candles: OHLCV candle data.
            lookback: Window for pivot detection.
            tolerance_pct: Levels within this % are merged.

        Returns:
            Sorted list of SupportResistance with strongest first.
        """
        if len(candles) < lookback * 2:
            return []

        df = _candles_to_df(candles)
        highs = df["high"].values
        lows = df["low"].values
        close_last = df["close"].iloc[-1]

        # Find pivot highs and lows
        pivot_levels: list[tuple[float, int, str]] = []  # (price, idx, type)
        order = max(lookback // 4, 2)

        for i in range(order, len(highs) - order):
            # Pivot high: highest in the window
            if highs[i] == max(highs[i - order: i + order + 1]):
                pivot_levels.append((highs[i], i, "resistance"))
            # Pivot low: lowest in the window
            if lows[i] == min(lows[i - order: i + order + 1]):
                pivot_levels.append((lows[i], i, "support"))

        if not pivot_levels:
            return []

        # Cluster nearby levels
        pivot_levels.sort(key=lambda x: x[0])
        clusters: list[SupportResistance] = []
        tolerance = close_last * tolerance_pct / 100.0

        visited = [False] * len(pivot_levels)
        for i, (price, idx, ltype) in enumerate(pivot_levels):
            if visited[i]:
                continue
            cluster_prices = [price]
            cluster_indices = [idx]
            visited[i] = True

            for j in range(i + 1, len(pivot_levels)):
                if visited[j]:
                    continue
                if abs(pivot_levels[j][0] - price) <= tolerance:
                    cluster_prices.append(pivot_levels[j][0])
                    cluster_indices.append(pivot_levels[j][1])
                    visited[j] = True

            avg_price = float(np.mean(cluster_prices))
            level_type = "resistance" if avg_price > close_last else "support"
            clusters.append(SupportResistance(
                price=round(avg_price, 2),
                strength=len(cluster_prices),
                level_type=level_type,
                first_seen_idx=min(cluster_indices),
                last_seen_idx=max(cluster_indices),
            ))

        clusters.sort(key=lambda x: x.strength, reverse=True)
        return clusters[:10]  # Return top 10 levels

    # ── Breakout Detection ───────────────────────────────────────────

    @staticmethod
    def detect_breakout(
        candles: list[Candle],
        sr_levels: list[SupportResistance],
        volume_threshold: float = 1.5,
    ) -> BreakoutSignal:
        """
        Detect if the latest candle breaks through a support/resistance level.

        Args:
            candles: Recent candles (last candle is evaluated).
            sr_levels: Detected S/R levels.
            volume_threshold: Volume must be X times avg for confirmation.
        """
        if not candles or not sr_levels:
            return BreakoutSignal(is_breakout=False)

        df = _candles_to_df(candles)
        if len(df) < 20:
            return BreakoutSignal(is_breakout=False)

        last = df.iloc[-1]
        prev = df.iloc[-2]
        avg_volume = df["volume"].rolling(20).mean().iloc[-1]
        vol_confirmed = last["volume"] > avg_volume * volume_threshold

        for level in sr_levels:
            if level.strength < 2:
                continue
            lp = level.price

            # Upward breakout through resistance
            if level.level_type == "resistance":
                if prev["close"] < lp and last["close"] > lp:
                    return BreakoutSignal(
                        is_breakout=True,
                        direction="up",
                        level=lp,
                        strength=level.strength,
                        volume_confirmed=vol_confirmed,
                    )

            # Downward breakout through support
            if level.level_type == "support":
                if prev["close"] > lp and last["close"] < lp:
                    return BreakoutSignal(
                        is_breakout=True,
                        direction="down",
                        level=lp,
                        strength=level.strength,
                        volume_confirmed=vol_confirmed,
                    )

        return BreakoutSignal(is_breakout=False)

    # ── Candlestick Patterns ─────────────────────────────────────────

    @staticmethod
    def detect_candlestick_patterns(
        candles: list[Candle],
        lookback: int = 5,
    ) -> list[CandlestickPattern]:
        """
        Detect common candlestick patterns in recent candles.
        Manual implementation for reliability with NSE data.
        """
        if len(candles) < 3:
            return []

        df = _candles_to_df(candles)
        patterns: list[CandlestickPattern] = []
        n = len(df)

        for i in range(max(2, n - lookback), n):
            o, h, l, c = df.iloc[i][["open", "high", "low", "close"]]
            body = abs(c - o)
            total_range = h - l
            if total_range == 0:
                continue

            body_pct = body / total_range
            upper_wick = h - max(o, c)
            lower_wick = min(o, c) - l

            # ── Doji ──
            if body_pct < 0.1:
                patterns.append(CandlestickPattern("doji", "neutral", 0.6, i))

            # ── Hammer (bullish, at bottom) ──
            if lower_wick > body * 2 and upper_wick < body * 0.5 and c > o:
                patterns.append(CandlestickPattern("hammer", "bullish", 0.7, i))

            # ── Shooting Star (bearish, at top) ──
            if upper_wick > body * 2 and lower_wick < body * 0.5 and c < o:
                patterns.append(CandlestickPattern("shooting_star", "bearish", 0.7, i))

            # ── Bullish Engulfing (2 bar) ──
            if i >= 1:
                po, _, _, pc = df.iloc[i - 1][["open", "high", "low", "close"]]
                if pc < po and c > o and o <= pc and c >= po:
                    patterns.append(CandlestickPattern("bullish_engulfing", "bullish", 0.8, i))
                if pc > po and c < o and o >= pc and c <= po:
                    patterns.append(CandlestickPattern("bearish_engulfing", "bearish", 0.8, i))

            # ── Morning Star / Evening Star (3 bar) ──
            if i >= 2:
                o1, _, _, c1 = df.iloc[i - 2][["open", "high", "low", "close"]]
                o2, _, _, c2 = df.iloc[i - 1][["open", "high", "low", "close"]]
                body1 = abs(c1 - o1)
                body2 = abs(c2 - o2)

                # Morning Star
                if c1 < o1 and body2 < body1 * 0.3 and c > o and c > (o1 + c1) / 2:
                    patterns.append(CandlestickPattern("morning_star", "bullish", 0.85, i))

                # Evening Star
                if c1 > o1 and body2 < body1 * 0.3 and c < o and c < (o1 + c1) / 2:
                    patterns.append(CandlestickPattern("evening_star", "bearish", 0.85, i))

        return patterns

    # ── Swing Highs & Lows ───────────────────────────────────────────

    @staticmethod
    def find_swing_highs_lows(
        candles: list[Candle],
        order: int = 5,
    ) -> TrendStructure:
        """
        Find swing highs and lows and determine trend structure.

        Args:
            order: Number of bars on each side to confirm a swing.
        """
        if len(candles) < order * 2 + 1:
            return TrendStructure(trend="sideways")

        df = _candles_to_df(candles)
        highs = df["high"].values
        lows = df["low"].values

        swing_highs: list[tuple[int, float]] = []
        swing_lows: list[tuple[int, float]] = []

        for i in range(order, len(highs) - order):
            if highs[i] == max(highs[i - order: i + order + 1]):
                swing_highs.append((i, float(highs[i])))
            if lows[i] == min(lows[i - order: i + order + 1]):
                swing_lows.append((i, float(lows[i])))

        # Analyse trend structure
        hh, hl, lh, ll = 0, 0, 0, 0
        for j in range(1, len(swing_highs)):
            if swing_highs[j][1] > swing_highs[j - 1][1]:
                hh += 1
            else:
                lh += 1
        for j in range(1, len(swing_lows)):
            if swing_lows[j][1] > swing_lows[j - 1][1]:
                hl += 1
            else:
                ll += 1

        if hh >= 2 and hl >= 2:
            trend = "uptrend"
        elif lh >= 2 and ll >= 2:
            trend = "downtrend"
        else:
            trend = "sideways"

        return TrendStructure(
            trend=trend,
            swing_highs=swing_highs[-5:],
            swing_lows=swing_lows[-5:],
            higher_highs=hh,
            higher_lows=hl,
            lower_highs=lh,
            lower_lows=ll,
        )

    # ── Trend Structure ──────────────────────────────────────────────

    @staticmethod
    def detect_trend_structure(candles: list[Candle]) -> dict:
        """
        Shorthand: analyse trend using swing high/low method.
        Returns dict compatible with agent context.
        """
        ts = PatternDetector.find_swing_highs_lows(candles)
        return {
            "trend": ts.trend,
            "higher_highs": ts.higher_highs,
            "higher_lows": ts.higher_lows,
            "lower_highs": ts.lower_highs,
            "lower_lows": ts.lower_lows,
            "recent_swing_highs": [sh[1] for sh in ts.swing_highs[-3:]],
            "recent_swing_lows": [sl[1] for sl in ts.swing_lows[-3:]],
        }

    # ── Volume Profile Proxy ─────────────────────────────────────────

    @staticmethod
    def calculate_volume_profile_proxy(
        candles: list[Candle],
        bins: int = 10,
    ) -> list[dict]:
        """
        Approximate volume profile by bucketing volume into price bins.

        Returns list of {price_low, price_high, total_volume, pct} sorted by volume desc.
        """
        if not candles:
            return []

        df = _candles_to_df(candles)
        if df.empty or "volume" not in df.columns:
            return []

        price_low = df["low"].min()
        price_high = df["high"].max()
        if price_high == price_low:
            return []

        bin_edges = np.linspace(price_low, price_high, bins + 1)
        profile: list[dict] = []
        total_vol = df["volume"].sum()
        if total_vol == 0:
            return []

        for i in range(len(bin_edges) - 1):
            lo, hi = bin_edges[i], bin_edges[i + 1]
            # Volume belongs to a bin if the candle's typical price falls within
            typical = (df["high"] + df["low"] + df["close"]) / 3
            mask = (typical >= lo) & (typical < hi)
            vol = int(df.loc[mask, "volume"].sum())
            profile.append({
                "price_low": round(float(lo), 2),
                "price_high": round(float(hi), 2),
                "total_volume": vol,
                "pct": round(vol / total_vol * 100, 1) if total_vol else 0,
            })

        profile.sort(key=lambda x: x["total_volume"], reverse=True)
        return profile
