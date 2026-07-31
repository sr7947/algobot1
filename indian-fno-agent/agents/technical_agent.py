"""
agents/technical_agent.py
--------------------------
Evaluates six core technical setups and returns an ensemble technical score,
entry/stop/target zones, and a directional signal.

Expected context keys
---------------------
indicators : dict[str, Any]
    Pre-computed values from the data pipeline:
        price        : float  — latest close / LTP
        ema_9        : float
        ema_21       : float
        ema_50       : float
        ema_200      : float
        vwap         : float
        rsi          : float
        macd         : float  — MACD line value
        macd_signal  : float  — signal line value
        macd_hist    : float  — current histogram bar
        macd_hist_prev: float — previous histogram bar (for direction change)
        macd_prev    : float  — previous MACD line (for zero-cross detection)
        supertrend_dir: int   — +1 bullish, -1 bearish
        supertrend_dir_prev: int — previous bar's supertrend direction
        atr          : float
        volume       : float  — current bar volume
        avg_volume_20: float  — 20-bar average volume
        sr_levels    : list[float] — support/resistance price levels
        bb_upper     : float
        bb_lower     : float

candles : dict[str, list[dict]]  (optional — for additional confirmation)
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from agents.base_agent import BaseAgent

logger = structlog.get_logger(__name__)

# ------------------------------------------------------------------ #
# Threshold constants                                                 #
# ------------------------------------------------------------------ #
RSI_OVERSOLD: float = 30.0
RSI_OVERBOUGHT: float = 70.0
RSI_MIDLINE: float = 50.0
VOLUME_CONFIRM_MULTIPLIER: float = 1.5   # volume must be 1.5× avg for breakout
ATR_ZONE_MULTIPLIER: float = 0.5         # entry zone half-width = 0.5 × ATR
SR_PROXIMITY_PCT: float = 0.005          # 0.5% proximity to S/R level


class TechnicalAgent(BaseAgent):
    """
    Evaluates six technical setups and produces a composite score (0–1):

    1. EMA stack alignment      — bullish: 9 > 21 > 50 > 200
    2. VWAP position            — price above / below VWAP
    3. RSI level analysis       — oversold/overbought/midline cross
    4. MACD signal              — histogram direction change, zero-line cross
    5. Supertrend direction     — current direction + recent change
    6. S/R breakout with volume — breakout of key level with volume confirmation

    Each setup contributes an equal 1/6 weight to the composite score unless
    the setup is directionally opposed, in which case it subtracts weight.
    """

    agent_name: str = "technical_agent"
    weight: float = 0.35

    # ------------------------------------------------------------------ #
    # Public interface                                                     #
    # ------------------------------------------------------------------ #

    async def analyze(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        Run all technical setup checks.

        Returns
        -------
        dict
            {
              "agent"            : "technical_agent",
              "technical_score"  : float (0–1),
              "setups_triggered" : list[str],
              "signal_direction" : "BULLISH" | "BEARISH" | "NEUTRAL",
              "entry_zone"       : {"low": float, "high": float},
              "stop_zone"        : {"level": float, "atr_multiple": float},
              "target_zone"      : {"t1": float, "t2": float, "t3": float},
              "confidence"       : float (= technical_score),
              "reasons"          : list[str],
              "latency_ms"       : float,
            }
        """
        start = time.monotonic()
        self._log.info("technical_analysis_start")

        ind: dict[str, Any] = context.get("indicators", {})

        # ---- extract indicator values --------------------------------
        price: float       = float(ind.get("price", 0.0))
        ema_9: float       = float(ind.get("ema_9", 0.0))
        ema_21: float      = float(ind.get("ema_21", 0.0))
        ema_50: float      = float(ind.get("ema_50", 0.0))
        ema_200: float     = float(ind.get("ema_200", 0.0))
        vwap: float        = float(ind.get("vwap", 0.0))
        rsi: float         = float(ind.get("rsi", 50.0))
        macd: float        = float(ind.get("macd", 0.0))
        macd_signal: float = float(ind.get("macd_signal", 0.0))
        macd_hist: float   = float(ind.get("macd_hist", 0.0))
        macd_hist_prev: float = float(ind.get("macd_hist_prev", 0.0))
        macd_prev: float   = float(ind.get("macd_prev", 0.0))
        st_dir: int        = int(ind.get("supertrend_dir", 0))
        st_dir_prev: int   = int(ind.get("supertrend_dir_prev", 0))
        atr: float         = float(ind.get("atr", price * 0.005))
        volume: float      = float(ind.get("volume", 0.0))
        avg_volume: float  = float(ind.get("avg_volume_20", 1.0))
        sr_levels: list[float] = [
            float(v) for v in ind.get("sr_levels", [])
        ]

        # ---- run each setup -----------------------------------------
        bull_score: float = 0.0   # cumulative bullish score
        bear_score: float = 0.0   # cumulative bearish score
        setups_triggered: list[str] = []
        reasons: list[str] = []
        num_setups: int = 6       # total number of setups evaluated

        # Setup 1 — EMA Stack
        ema_result, ema_reasons = self._ema_stack(
            price, ema_9, ema_21, ema_50, ema_200
        )
        reasons.extend(ema_reasons)
        if ema_result > 0:
            bull_score += ema_result
            setups_triggered.append("EMA_STACK_BULLISH")
        elif ema_result < 0:
            bear_score += abs(ema_result)
            setups_triggered.append("EMA_STACK_BEARISH")

        # Setup 2 — VWAP Position
        vwap_result, vwap_reasons = self._vwap_position(price, vwap)
        reasons.extend(vwap_reasons)
        if vwap_result > 0:
            bull_score += vwap_result
            setups_triggered.append("ABOVE_VWAP")
        elif vwap_result < 0:
            bear_score += abs(vwap_result)
            setups_triggered.append("BELOW_VWAP")

        # Setup 3 — RSI Levels
        rsi_result, rsi_reasons = self._rsi_levels(rsi)
        reasons.extend(rsi_reasons)
        if rsi_result > 0:
            bull_score += rsi_result
            setups_triggered.append("RSI_BULLISH")
        elif rsi_result < 0:
            bear_score += abs(rsi_result)
            setups_triggered.append("RSI_BEARISH")

        # Setup 4 — MACD Signal
        macd_result, macd_reasons = self._macd_signal(
            macd, macd_signal, macd_hist, macd_hist_prev, macd_prev
        )
        reasons.extend(macd_reasons)
        if macd_result > 0:
            bull_score += macd_result
            setups_triggered.append("MACD_BULLISH")
        elif macd_result < 0:
            bear_score += abs(macd_result)
            setups_triggered.append("MACD_BEARISH")

        # Setup 5 — Supertrend
        st_result, st_reasons = self._supertrend(st_dir, st_dir_prev)
        reasons.extend(st_reasons)
        if st_result > 0:
            bull_score += st_result
            setups_triggered.append("SUPERTREND_BULLISH")
            if st_dir != st_dir_prev:
                setups_triggered.append("SUPERTREND_DIRECTION_CHANGE")
        elif st_result < 0:
            bear_score += abs(st_result)
            setups_triggered.append("SUPERTREND_BEARISH")
            if st_dir != st_dir_prev:
                setups_triggered.append("SUPERTREND_DIRECTION_CHANGE")

        # Setup 6 — S/R Breakout with Volume
        sr_result, sr_reasons = self._sr_breakout(
            price, sr_levels, volume, avg_volume
        )
        reasons.extend(sr_reasons)
        if sr_result > 0:
            bull_score += sr_result
            setups_triggered.append("SR_BREAKOUT_BULLISH")
        elif sr_result < 0:
            bear_score += abs(sr_result)
            setups_triggered.append("SR_BREAKDOWN_BEARISH")

        # ---- determine direction and final score --------------------
        net_score = bull_score - bear_score
        technical_score = self._clamp(
            (bull_score + bear_score) / num_setups
        )

        if net_score > 0.10:
            signal_direction = "BULLISH"
        elif net_score < -0.10:
            signal_direction = "BEARISH"
        else:
            signal_direction = "NEUTRAL"

        # ---- compute zones ------------------------------------------
        entry_zone = self._entry_zone(price, atr)
        stop_zone  = self._stop_zone(price, atr, signal_direction)
        target_zone = self._target_zone(price, atr, signal_direction)

        self._log.info(
            "technical_scored",
            technical_score=round(technical_score, 3),
            direction=signal_direction,
            setups=setups_triggered,
        )

        result: dict[str, Any] = {
            "technical_score"  : round(technical_score, 4),
            "setups_triggered" : setups_triggered,
            "signal_direction" : signal_direction,
            "entry_zone"       : entry_zone,
            "stop_zone"        : stop_zone,
            "target_zone"      : target_zone,
            "confidence"       : round(technical_score, 4),
            "reasons"          : reasons,
        }
        return self._timed_result(result, start)

    async def health_check(self) -> bool:
        """Pure rule-based — always healthy."""
        return True

    # ------------------------------------------------------------------ #
    # Setup evaluators — each returns (score: float, reasons: list[str]) #
    # score > 0  → bullish contribution                                   #
    # score < 0  → bearish contribution                                   #
    # score == 0 → neutral / setup not applicable                         #
    # ------------------------------------------------------------------ #

    def _ema_stack(
        self,
        price: float,
        ema_9: float,
        ema_21: float,
        ema_50: float,
        ema_200: float,
    ) -> tuple[float, list[str]]:
        """
        Bullish EMA stack  : price > EMA9 > EMA21 > EMA50 > EMA200
        Bearish EMA stack  : price < EMA9 < EMA21 < EMA50 < EMA200
        Returns partial score if only some conditions are met.
        """
        reasons: list[str] = []
        # Count bullish conditions (each worth 0.2 → max 1.0)
        bull_conditions = [
            price > ema_9,
            ema_9  > ema_21,
            ema_21 > ema_50,
            ema_50 > ema_200,
            price > ema_200,
        ]
        bear_conditions = [
            price < ema_9,
            ema_9  < ema_21,
            ema_21 < ema_50,
            ema_50 < ema_200,
            price < ema_200,
        ]
        bull_count = sum(bull_conditions)
        bear_count = sum(bear_conditions)

        score: float = 0.0
        if bull_count >= 4:
            score = bull_count / 5.0  # 0.8 or 1.0
            reasons.append(
                f"EMA stack bullish ({bull_count}/5 conditions met): "
                f"EMA9={ema_9:.2f} EMA21={ema_21:.2f} "
                f"EMA50={ema_50:.2f} EMA200={ema_200:.2f}"
            )
        elif bear_count >= 4:
            score = -(bear_count / 5.0)
            reasons.append(
                f"EMA stack bearish ({bear_count}/5 conditions met): "
                f"EMA9={ema_9:.2f} EMA21={ema_21:.2f} "
                f"EMA50={ema_50:.2f} EMA200={ema_200:.2f}"
            )
        else:
            reasons.append(
                f"EMA stack mixed: {bull_count} bull / {bear_count} bear conditions"
            )
        return score, reasons

    def _vwap_position(
        self, price: float, vwap: float
    ) -> tuple[float, list[str]]:
        """Price above VWAP → bullish; below → bearish."""
        reasons: list[str] = []
        if vwap <= 0:
            reasons.append("VWAP not available — skipping VWAP check")
            return 0.0, reasons

        pct_diff = (price - vwap) / vwap * 100
        if price > vwap:
            reasons.append(
                f"Price {price:.2f} is {pct_diff:.2f}% ABOVE VWAP ({vwap:.2f})"
            )
            # Score scales with distance but caps at 1.0
            score = self._clamp(0.60 + abs(pct_diff) * 0.05)
            return score, reasons
        else:
            reasons.append(
                f"Price {price:.2f} is {abs(pct_diff):.2f}% BELOW VWAP ({vwap:.2f})"
            )
            score = self._clamp(0.60 + abs(pct_diff) * 0.05)
            return -score, reasons

    def _rsi_levels(self, rsi: float) -> tuple[float, list[str]]:
        """
        Bullish signals: RSI recovering from oversold (<30) or crossing above 50.
        Bearish signals: RSI retreating from overbought (>70) or crossing below 50.
        """
        reasons: list[str] = []
        if rsi < RSI_OVERSOLD:
            reasons.append(f"RSI {rsi:.1f} is in OVERSOLD territory (<{RSI_OVERSOLD})")
            score = 0.70 + (RSI_OVERSOLD - rsi) / RSI_OVERSOLD * 0.25
            return self._clamp(score), reasons
        elif rsi > RSI_OVERBOUGHT:
            reasons.append(f"RSI {rsi:.1f} is in OVERBOUGHT territory (>{RSI_OVERBOUGHT})")
            score = 0.70 + (rsi - RSI_OVERBOUGHT) / (100 - RSI_OVERBOUGHT) * 0.25
            return -self._clamp(score), reasons
        elif rsi > RSI_MIDLINE:
            reasons.append(
                f"RSI {rsi:.1f} above midline ({RSI_MIDLINE}) — bullish momentum"
            )
            return 0.55, reasons
        else:
            reasons.append(
                f"RSI {rsi:.1f} below midline ({RSI_MIDLINE}) — bearish momentum"
            )
            return -0.55, reasons

    def _macd_signal(
        self,
        macd: float,
        macd_signal: float,
        macd_hist: float,
        macd_hist_prev: float,
        macd_prev: float,
    ) -> tuple[float, list[str]]:
        """
        Bullish: histogram turning up (prev < 0, cur > prev), or MACD crossing zero.
        Bearish: histogram turning down (prev > 0, cur < prev), or MACD crossing zero downward.
        """
        reasons: list[str] = []
        score: float = 0.0

        hist_turned_up   = macd_hist > macd_hist_prev and macd_hist_prev <= 0
        hist_turned_down = macd_hist < macd_hist_prev and macd_hist_prev >= 0
        zero_cross_up    = macd >= 0 and macd_prev < 0
        zero_cross_down  = macd <= 0 and macd_prev > 0
        signal_cross_up  = macd > macd_signal
        signal_cross_down = macd < macd_signal

        if zero_cross_up:
            reasons.append("MACD crossed above zero line — bullish crossover")
            score += 0.80
        elif hist_turned_up:
            reasons.append(
                f"MACD histogram direction change UP "
                f"(prev={macd_hist_prev:.4f} → cur={macd_hist:.4f})"
            )
            score += 0.65
        elif signal_cross_up:
            reasons.append(
                f"MACD ({macd:.4f}) above signal ({macd_signal:.4f})"
            )
            score += 0.50

        if zero_cross_down:
            reasons.append("MACD crossed below zero line — bearish crossover")
            score -= 0.80
        elif hist_turned_down:
            reasons.append(
                f"MACD histogram direction change DOWN "
                f"(prev={macd_hist_prev:.4f} → cur={macd_hist:.4f})"
            )
            score -= 0.65
        elif signal_cross_down:
            reasons.append(
                f"MACD ({macd:.4f}) below signal ({macd_signal:.4f})"
            )
            score -= 0.50

        if score == 0.0:
            reasons.append(
                f"MACD neutral: macd={macd:.4f} "
                f"signal={macd_signal:.4f} hist={macd_hist:.4f}"
            )

        return self._clamp(score, -1.0, 1.0), reasons

    def _supertrend(
        self, st_dir: int, st_dir_prev: int
    ) -> tuple[float, list[str]]:
        """
        +1 = bullish supertrend, -1 = bearish.
        A direction change this bar adds extra weight.
        """
        reasons: list[str] = []
        direction_changed = st_dir != st_dir_prev and st_dir_prev != 0

        if st_dir == 1:
            base_score = 0.65
            if direction_changed:
                base_score = 0.85
                reasons.append(
                    "Supertrend FLIPPED to BULLISH this bar — strong signal"
                )
            else:
                reasons.append("Supertrend is BULLISH (green)")
            return base_score, reasons
        elif st_dir == -1:
            base_score = 0.65
            if direction_changed:
                base_score = 0.85
                reasons.append(
                    "Supertrend FLIPPED to BEARISH this bar — strong signal"
                )
            else:
                reasons.append("Supertrend is BEARISH (red)")
            return -base_score, reasons
        else:
            reasons.append("Supertrend direction unknown — skipped")
            return 0.0, reasons

    def _sr_breakout(
        self,
        price: float,
        sr_levels: list[float],
        volume: float,
        avg_volume: float,
    ) -> tuple[float, list[str]]:
        """
        Detects if price has broken above (bullish) or below (bearish) a key
        S/R level with volume confirmation (volume > 1.5× average).
        """
        reasons: list[str] = []
        if not sr_levels:
            reasons.append("No S/R levels provided — breakout check skipped")
            return 0.0, reasons

        volume_confirmed = (
            avg_volume > 0 and volume >= VOLUME_CONFIRM_MULTIPLIER * avg_volume
        )
        vol_str = (
            f"volume {volume:.0f} ({volume/avg_volume:.1f}× avg)"
            if avg_volume > 0 else "volume unknown"
        )

        # Find the closest resistance above price and support below price
        resistances = sorted([s for s in sr_levels if s > price])
        supports    = sorted([s for s in sr_levels if s <= price], reverse=True)

        # Recent breakout: price just crossed a level (within SR_PROXIMITY_PCT)
        if resistances:
            nearest_res = resistances[0]
            proximity = abs(price - nearest_res) / nearest_res
            if proximity <= SR_PROXIMITY_PCT and volume_confirmed:
                reasons.append(
                    f"Breakout above S/R resistance {nearest_res:.2f} "
                    f"confirmed with {vol_str}"
                )
                return 0.80, reasons
            elif proximity <= SR_PROXIMITY_PCT:
                reasons.append(
                    f"Near resistance {nearest_res:.2f} but volume insufficient "
                    f"({vol_str}) — weak breakout"
                )
                return 0.40, reasons

        if supports:
            nearest_sup = supports[0]
            proximity = abs(price - nearest_sup) / nearest_sup
            if proximity <= SR_PROXIMITY_PCT and volume_confirmed:
                reasons.append(
                    f"Breakdown below S/R support {nearest_sup:.2f} "
                    f"confirmed with {vol_str}"
                )
                return -0.80, reasons
            elif proximity <= SR_PROXIMITY_PCT:
                reasons.append(
                    f"Near support {nearest_sup:.2f} but volume insufficient "
                    f"({vol_str}) — weak breakdown"
                )
                return -0.40, reasons

        reasons.append(
            f"Price {price:.2f} not near any S/R level — no breakout detected"
        )
        return 0.0, reasons

    # ------------------------------------------------------------------ #
    # Zone calculation helpers                                             #
    # ------------------------------------------------------------------ #

    def _entry_zone(self, price: float, atr: float) -> dict[str, float]:
        """Entry zone is ±0.5 ATR around current price."""
        half = ATR_ZONE_MULTIPLIER * atr
        return {
            "low" : round(price - half, 2),
            "high": round(price + half, 2),
        }

    def _stop_zone(
        self, price: float, atr: float, direction: str
    ) -> dict[str, float]:
        """
        Stop is placed 1.5 ATR from price in the opposite direction.
        Returns the stop level and the ATR multiple used.
        """
        atr_multiple = 1.5
        if direction == "BULLISH":
            level = price - atr_multiple * atr
        elif direction == "BEARISH":
            level = price + atr_multiple * atr
        else:
            level = price - atr_multiple * atr  # default to below
        return {
            "level"      : round(level, 2),
            "atr_multiple": atr_multiple,
        }

    def _target_zone(
        self, price: float, atr: float, direction: str
    ) -> dict[str, float]:
        """
        Three targets at 1.5R, 2.5R, 4R where R = 1.5 ATR (same as stop distance).
        """
        risk = 1.5 * atr
        sign = 1 if direction != "BEARISH" else -1
        return {
            "t1": round(price + sign * 1.5 * risk, 2),
            "t2": round(price + sign * 2.5 * risk, 2),
            "t3": round(price + sign * 4.0 * risk, 2),
        }
