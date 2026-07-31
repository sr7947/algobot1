"""
agents/regime_agent.py
----------------------
Classifies the current market regime using a multi-factor rule engine.

Expected context keys
---------------------
candles : dict[str, list[dict]]
    Multi-timeframe OHLCV candles, keyed by timeframe string.
    e.g. {"15m": [...], "1h": [...], "1d": [...]}
    Each candle dict: {open, high, low, close, volume, timestamp}

indicators : dict[str, Any]
    Pre-computed indicator values (from the data pipeline), expected keys:
        adx          : float
        ema_200      : float
        bb_upper     : float
        bb_lower     : float
        bb_mid       : float
        atr          : float
        atr_20_avg   : float   — rolling 20-bar average of ATR
        rsi          : float
        rsi_divergence: bool   — True if bullish/bearish divergence detected
        price        : float   — latest close / spot price

news_events : list[dict]  (optional)
    Each event dict must have a "severity" key with values:
    LOW | MEDIUM | HIGH | CRITICAL
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any

import structlog

from agents.base_agent import BaseAgent

logger = structlog.get_logger(__name__)


class MarketRegime(str, Enum):
    TRENDING_BULL = "TRENDING_BULL"
    TRENDING_BEAR = "TRENDING_BEAR"
    RANGE_BOUND = "RANGE_BOUND"
    VOLATILE_BREAKOUT = "VOLATILE_BREAKOUT"
    REVERSAL = "REVERSAL"
    NEWS_DRIVEN = "NEWS_DRIVEN"
    UNDEFINED = "UNDEFINED"


# ------------------------------------------------------------------ #
# Regime classification thresholds                                    #
# ------------------------------------------------------------------ #
ADX_TREND_THRESHOLD: float = 25.0       # above this → trending
ADX_RANGE_THRESHOLD: float = 20.0       # below this → range-bound
ATR_SPIKE_MULTIPLIER: float = 2.0       # ATR > 2× avg → breakout
RSI_OVERBOUGHT: float = 70.0
RSI_OVERSOLD: float = 30.0


class RegimeAgent(BaseAgent):
    """
    Classifies the current market regime into one of six categories:

    TRENDING_BULL       — ADX > 25 and price above EMA-200
    TRENDING_BEAR       — ADX > 25 and price below EMA-200
    RANGE_BOUND         — ADX < 20 and Bollinger Band squeeze active
    VOLATILE_BREAKOUT   — ATR > 2× its 20-bar average
    REVERSAL            — RSI divergence combined with price pattern signal
    NEWS_DRIVEN         — Any HIGH or CRITICAL news event is active
    UNDEFINED           — Conditions ambiguous; no clear regime
    """

    agent_name: str = "regime_agent"
    weight: float = 0.20

    # ------------------------------------------------------------------ #
    # Public interface                                                     #
    # ------------------------------------------------------------------ #

    async def analyze(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        Determine the current market regime.

        Returns
        -------
        dict
            {
              "agent"      : "regime_agent",
              "regime"     : MarketRegime (str value),
              "confidence" : float  (0–1),
              "reasons"    : list[str],
              "latency_ms" : float,
            }
        """
        start = time.monotonic()
        self._log.info("regime_analysis_start")

        indicators: dict[str, Any] = context.get("indicators", {})
        news_events: list[dict] = context.get("news_events", [])

        # Extract individual indicator values with safe defaults
        adx: float = float(indicators.get("adx", 0.0))
        ema_200: float = float(indicators.get("ema_200", 0.0))
        price: float = float(indicators.get("price", 0.0))
        bb_upper: float = float(indicators.get("bb_upper", 0.0))
        bb_lower: float = float(indicators.get("bb_lower", 0.0))
        bb_mid: float = float(indicators.get("bb_mid", 0.0))
        atr: float = float(indicators.get("atr", 0.0))
        atr_20_avg: float = float(indicators.get("atr_20_avg", 1.0))  # avoid /0
        rsi: float = float(indicators.get("rsi", 50.0))
        rsi_divergence: bool = bool(indicators.get("rsi_divergence", False))

        regime, confidence, reasons = self._classify(
            adx=adx,
            ema_200=ema_200,
            price=price,
            bb_upper=bb_upper,
            bb_lower=bb_lower,
            bb_mid=bb_mid,
            atr=atr,
            atr_20_avg=atr_20_avg,
            rsi=rsi,
            rsi_divergence=rsi_divergence,
            news_events=news_events,
        )

        self._log.info(
            "regime_classified",
            regime=regime.value,
            confidence=round(confidence, 3),
            reasons=reasons,
        )

        result: dict[str, Any] = {
            "regime": regime.value,
            "confidence": self._clamp(confidence),
            "reasons": reasons,
        }
        return self._timed_result(result, start)

    async def health_check(self) -> bool:
        """Always healthy — pure rule-based logic, no external dependencies."""
        return True

    # ------------------------------------------------------------------ #
    # Internal classification logic                                        #
    # ------------------------------------------------------------------ #

    def _classify(
        self,
        adx: float,
        ema_200: float,
        price: float,
        bb_upper: float,
        bb_lower: float,
        bb_mid: float,
        atr: float,
        atr_20_avg: float,
        rsi: float,
        rsi_divergence: bool,
        news_events: list[dict],
    ) -> tuple[MarketRegime, float, list[str]]:
        """
        Rule engine that evaluates regime conditions in priority order.

        Priority (highest → lowest):
          1. NEWS_DRIVEN    — hard override when HIGH/CRITICAL news present
          2. VOLATILE_BREAKOUT — ATR spike overrides trend labels
          3. TRENDING_BULL
          4. TRENDING_BEAR
          5. REVERSAL
          6. RANGE_BOUND
          7. UNDEFINED
        """
        reasons: list[str] = []
        confidence: float = 0.0

        # ---- 1. NEWS_DRIVEN (highest priority) -------------------------
        high_severity_news = [
            e for e in news_events
            if e.get("severity", "").upper() in ("HIGH", "CRITICAL")
        ]
        if high_severity_news:
            severities = [e["severity"] for e in high_severity_news]
            reasons.append(
                f"Active high-impact news detected (severities: {severities})"
            )
            # Confidence scales with severity count, capped at 0.95
            confidence = self._clamp(0.70 + 0.05 * len(high_severity_news), 0.0, 0.95)
            return MarketRegime.NEWS_DRIVEN, confidence, reasons

        # ---- 2. VOLATILE_BREAKOUT (ATR spike) --------------------------
        if atr_20_avg > 0 and atr > (ATR_SPIKE_MULTIPLIER * atr_20_avg):
            ratio = atr / atr_20_avg
            reasons.append(
                f"ATR spike: {atr:.2f} is {ratio:.1f}× the 20-bar avg ({atr_20_avg:.2f})"
            )
            confidence = self._clamp(0.60 + 0.05 * (ratio - ATR_SPIKE_MULTIPLIER))
            return MarketRegime.VOLATILE_BREAKOUT, confidence, reasons

        # ---- 3. TRENDING_BULL ------------------------------------------
        if adx > ADX_TREND_THRESHOLD and price > ema_200 and ema_200 > 0:
            reasons.append(
                f"ADX {adx:.1f} > {ADX_TREND_THRESHOLD} (trending strength)"
            )
            reasons.append(
                f"Price {price:.2f} above EMA-200 ({ema_200:.2f})"
            )
            # Extra confidence if ADX is strongly trending
            confidence = self._clamp(0.65 + 0.01 * (adx - ADX_TREND_THRESHOLD))
            # Boost if RSI is in the bullish zone
            if 40 < rsi < RSI_OVERBOUGHT:
                reasons.append(f"RSI {rsi:.1f} supports bullish momentum")
                confidence = self._clamp(confidence + 0.05)
            return MarketRegime.TRENDING_BULL, confidence, reasons

        # ---- 4. TRENDING_BEAR ------------------------------------------
        if adx > ADX_TREND_THRESHOLD and price < ema_200 and ema_200 > 0:
            reasons.append(
                f"ADX {adx:.1f} > {ADX_TREND_THRESHOLD} (trending strength)"
            )
            reasons.append(
                f"Price {price:.2f} below EMA-200 ({ema_200:.2f})"
            )
            confidence = self._clamp(0.65 + 0.01 * (adx - ADX_TREND_THRESHOLD))
            if RSI_OVERSOLD < rsi < 60:
                reasons.append(f"RSI {rsi:.1f} supports bearish momentum")
                confidence = self._clamp(confidence + 0.05)
            return MarketRegime.TRENDING_BEAR, confidence, reasons

        # ---- 5. REVERSAL (RSI divergence + pattern) --------------------
        if rsi_divergence:
            reasons.append("RSI divergence detected (potential reversal signal)")
            confidence = 0.60
            if rsi < RSI_OVERSOLD:
                reasons.append(
                    f"RSI {rsi:.1f} in oversold territory — bullish reversal candidate"
                )
                confidence = self._clamp(confidence + 0.10)
            elif rsi > RSI_OVERBOUGHT:
                reasons.append(
                    f"RSI {rsi:.1f} in overbought territory — bearish reversal candidate"
                )
                confidence = self._clamp(confidence + 0.10)
            return MarketRegime.REVERSAL, confidence, reasons

        # ---- 6. RANGE_BOUND (BB squeeze) --------------------------------
        bb_width = bb_upper - bb_lower
        bb_squeeze = bb_width > 0 and (bb_width / bb_mid if bb_mid > 0 else 1.0) < 0.04
        if adx < ADX_RANGE_THRESHOLD:
            reasons.append(
                f"ADX {adx:.1f} < {ADX_RANGE_THRESHOLD} — low directional strength"
            )
            confidence = 0.55
            if bb_squeeze:
                reasons.append(
                    f"Bollinger Band squeeze detected "
                    f"(width={bb_width:.2f}, mid={bb_mid:.2f})"
                )
                confidence = self._clamp(confidence + 0.15)
            return MarketRegime.RANGE_BOUND, confidence, reasons

        # ---- 7. UNDEFINED (no clear regime) ----------------------------
        reasons.append(
            f"No dominant regime signal: ADX={adx:.1f}, "
            f"price vs EMA200 diff={price - ema_200:.2f}, "
            f"RSI={rsi:.1f}"
        )
        return MarketRegime.UNDEFINED, 0.30, reasons

    # ------------------------------------------------------------------ #
    # Bollinger Band squeeze helper (also usable from outside)            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def is_bb_squeeze(
        bb_upper: float, bb_lower: float, bb_mid: float, threshold: float = 0.04
    ) -> bool:
        """
        Returns True if the Bollinger Band width is below *threshold* fraction
        of the midline — indicating a squeeze / low-volatility consolidation.
        """
        if bb_mid <= 0:
            return False
        return (bb_upper - bb_lower) / bb_mid < threshold
