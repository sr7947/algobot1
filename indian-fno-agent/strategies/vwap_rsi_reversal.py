"""
strategies/vwap_rsi_reversal.py
================================
VwapRsiReversalStrategy – intraday mean-reversion strategy for NSE F&O.

Strategy overview
-----------------
Designed for range-bound or reversing market conditions.  When price deviates
significantly from VWAP and oscillators show extreme readings that are turning,
the strategy anticipates a reversion back toward the VWAP mean.

Suitable for both Futures (FUT) and Options (CE/PE).  When trading options,
buy a CE on bullish reversion and a PE on bearish reversion.

Entry conditions – BUY (price overly sold vs VWAP)
----------------------------------------------------
1. **VWAP deviation** – Price is > 0.5 % *below* VWAP  (bearish extreme).
2. **RSI oversold + turning** – RSI(14) < 35 AND current RSI > previous RSI
   (momentum inflection point).
3. **MACD histogram positive** – MACD histogram (this bar) > 0, confirming
   bullish momentum shift.
4. **Volume spike** – Current bar volume > 2x average volume (smart money
   entering on dip).
5. **Near support** – Price within 1 ATR of a known support level.
6. **Market regime** – Regime is RANGE_BOUND or REVERSAL.

SELL (bearish, price overly extended above VWAP) – mirror of all conditions.

Risk management
---------------
- **Entry**: next candle open after signal bar closes.
- **Stop loss**: BUY → most-recent swing low; SELL → most-recent swing high.
  If swing level not available, falls back to 1 ATR beyond entry.
- **Target 1**: VWAP level (primary mean-reversion target).
- **Target 2**: 1st standard deviation band above (BUY) / below (SELL) VWAP.

Time filter
-----------
Only generate signals between 09:30 and 14:30 IST to avoid excessive theta
decay risk in the last trading hour and the volatile open auction period.
"""

from __future__ import annotations

import logging
from datetime import time
from typing import Any, Optional

from .base_strategy import (
    IStrategy,
    InstrumentType,
    MarketRegime,
    SignalDirection,
    TradeSignal,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Confidence map – 6 conditions
# ---------------------------------------------------------------------------
_CONFIDENCE_MAP: dict[int, float] = {
    6: 1.00,
    5: 0.85,
    4: 0.70,
    3: 0.55,
    2: 0.40,
    1: 0.20,
    0: 0.00,
}

# IST trading window: 09:30 – 14:30
_WINDOW_START = time(9, 30)
_WINDOW_END = time(14, 30)


class VwapRsiReversalStrategy(IStrategy):
    """
    Intraday mean-reversion strategy based on VWAP deviation + RSI + MACD.

    Config keys (strategies.yaml → vwap_rsi_reversal)
    --------------------------------------------------
    vwap_deviation_pct    float   Price deviation from VWAP trigger (default 0.5%)
    rsi_oversold          float   RSI threshold for oversold (default 35)
    rsi_overbought        float   RSI threshold for overbought (default 65)
    volume_multiplier     float   Volume spike multiplier (default 2.0)
    atr_support_window    float   Max distance from support in ATR multiples (default 1.0)
    min_confidence        float   Minimum confidence gate (default 0.65)
    """

    strategy_name: str = "vwap_rsi_reversal"
    version: str = "1.0.0"
    is_enabled: bool = True
    supported_instruments: list[InstrumentType] = [
        InstrumentType.FUT,
        InstrumentType.CE,
        InstrumentType.PE,
    ]
    min_confidence_threshold: float = 0.65

    _DEFAULT_VWAP_DEVIATION_PCT: float = 0.5
    _DEFAULT_RSI_OVERSOLD: float = 35.0
    _DEFAULT_RSI_OVERBOUGHT: float = 65.0
    _DEFAULT_VOLUME_MULTIPLIER: float = 2.0
    _DEFAULT_ATR_SUPPORT_WINDOW: float = 1.0

    def __init__(self, config: Optional[dict[str, Any]] = None) -> None:
        super().__init__(config=config)

        cfg = self._config
        self.vwap_deviation_pct: float = float(
            cfg.get("vwap_deviation_pct", self._DEFAULT_VWAP_DEVIATION_PCT)
        )
        self.rsi_oversold: float = float(
            cfg.get("rsi_oversold", self._DEFAULT_RSI_OVERSOLD)
        )
        self.rsi_overbought: float = float(
            cfg.get("rsi_overbought", self._DEFAULT_RSI_OVERBOUGHT)
        )
        self.volume_multiplier: float = float(
            cfg.get("volume_multiplier", self._DEFAULT_VOLUME_MULTIPLIER)
        )
        self.atr_support_window: float = float(
            cfg.get("atr_support_window", self._DEFAULT_ATR_SUPPORT_WINDOW)
        )

    # ------------------------------------------------------------------
    # Config validation
    # ------------------------------------------------------------------

    def validate_config(self, config: dict[str, Any]) -> bool:
        """Validate config numeric ranges."""
        valid = True

        dev = config.get("vwap_deviation_pct", self._DEFAULT_VWAP_DEVIATION_PCT)
        if not isinstance(dev, (int, float)) or dev <= 0:
            self._logger.error("vwap_deviation_pct must be > 0, got %s", dev)
            valid = False

        rsi_os = config.get("rsi_oversold", self._DEFAULT_RSI_OVERSOLD)
        rsi_ob = config.get("rsi_overbought", self._DEFAULT_RSI_OVERBOUGHT)
        if not (0 < rsi_os < rsi_ob < 100):
            self._logger.error(
                "rsi_oversold/rsi_overbought must satisfy 0 < os < ob < 100, "
                "got %s / %s",
                rsi_os,
                rsi_ob,
            )
            valid = False

        vol_mult = config.get("volume_multiplier", self._DEFAULT_VOLUME_MULTIPLIER)
        if not isinstance(vol_mult, (int, float)) or vol_mult < 1.0:
            self._logger.error("volume_multiplier must be >= 1.0, got %s", vol_mult)
            valid = False

        return valid

    # ------------------------------------------------------------------
    # Signal generation
    # ------------------------------------------------------------------

    async def generate_signal(
        self, context: dict[str, Any]
    ) -> Optional[TradeSignal]:
        """
        Evaluate VWAP-reversion conditions and return a TradeSignal or None.

        Required context keys
        ---------------------
        ``ltp``             float        – last traded price
        ``prev_rsi``        float        – RSI of previous bar (for turning check)
        ``rsi``             float        – current RSI(14)
        ``vwap``            float        – session VWAP
        ``vwap_std_dev``    float        – 1-sigma VWAP band width
        ``macd_histogram``  float        – current MACD histogram value
        ``volume``          float        – current bar volume
        ``avg_volume``      float        – rolling average volume
        ``atr``             float        – ATR(14)
        ``support_level``   float        – nearest support level (0 if unknown)
        ``resistance_level`` float       – nearest resistance level (0 if unknown)
        ``swing_low``       float        – recent swing low (for BUY stop)
        ``swing_high``      float        – recent swing high (for SELL stop)
        ``regime``          MarketRegime – current market regime
        ``symbol``          str
        ``underlying``      str
        ``instrument_type`` InstrumentType
        ``timestamp``       datetime     – bar close timestamp (IST)
        ``expiry``          Optional[str]
        """
        if not self._check_enabled():
            return None

        instrument_type: InstrumentType = context.get(
            "instrument_type", InstrumentType.FUT
        )
        if not self._check_instrument(instrument_type):
            return None

        # --- Extract values ---
        try:
            ltp: float = float(context["ltp"])
            rsi: float = float(context["rsi"])
            prev_rsi: float = float(context["prev_rsi"])
            vwap: float = float(context["vwap"])
            vwap_std_dev: float = float(context.get("vwap_std_dev", 0.0))
            macd_histogram: float = float(context["macd_histogram"])
            volume: float = float(context["volume"])
            avg_volume: float = float(context["avg_volume"])
            atr: float = float(context["atr"])
            support_level: float = float(context.get("support_level", 0.0))
            resistance_level: float = float(context.get("resistance_level", 0.0))
            swing_low: float = float(context.get("swing_low", 0.0))
            swing_high: float = float(context.get("swing_high", 0.0))
            regime: MarketRegime = context.get("regime", MarketRegime.UNKNOWN)
            symbol: str = context["symbol"]
            underlying: str = context["underlying"]
            expiry: Optional[str] = context.get("expiry")
            timestamp = context.get("timestamp")
        except KeyError as exc:
            self._logger.error(
                "Missing required context key for %s: %s", self.strategy_name, exc
            )
            return None

        # --- Time-window filter ---
        if timestamp is not None:
            bar_time = timestamp.time() if hasattr(timestamp, "time") else None
            if bar_time and not (_WINDOW_START <= bar_time <= _WINDOW_END):
                self._logger.debug(
                    "Outside trading window (%s); signal suppressed.", bar_time
                )
                return None

        # ----------------------------------------------------------------
        # Determine direction: BUY (oversold below VWAP) or SELL (overbought above VWAP)
        # ----------------------------------------------------------------
        vwap_pct_diff: float = (ltp - vwap) / vwap * 100  # negative = below VWAP

        is_bullish_setup = vwap_pct_diff <= -self.vwap_deviation_pct
        is_bearish_setup = vwap_pct_diff >= self.vwap_deviation_pct

        if not is_bullish_setup and not is_bearish_setup:
            self._logger.debug(
                "Price deviation %.2f%% does not meet ±%.2f%% threshold; no signal.",
                vwap_pct_diff,
                self.vwap_deviation_pct,
            )
            return None

        direction: SignalDirection = (
            SignalDirection.BUY if is_bullish_setup else SignalDirection.SELL
        )

        # ----------------------------------------------------------------
        # Evaluate conditions
        # ----------------------------------------------------------------
        conditions_met: int = 0
        rationale: list[str] = []

        # Condition 1 – VWAP deviation
        # (already confirmed above as the entry gate)
        conditions_met += 1
        rationale.append(
            f"✅ VWAP deviation: Price ({ltp:.2f}) is {abs(vwap_pct_diff):.2f}% "
            f"{'below' if is_bullish_setup else 'above'} VWAP ({vwap:.2f}) – "
            f"threshold {self.vwap_deviation_pct}% met."
        )

        # Condition 2 – RSI extreme AND turning
        if is_bullish_setup:
            rsi_extreme = rsi < self.rsi_oversold
            rsi_turning = rsi > prev_rsi
        else:
            rsi_extreme = rsi > self.rsi_overbought
            rsi_turning = rsi < prev_rsi

        cond2 = rsi_extreme and rsi_turning
        if cond2:
            conditions_met += 1
            threshold = self.rsi_oversold if is_bullish_setup else self.rsi_overbought
            direction_str = "oversold" if is_bullish_setup else "overbought"
            turn_str = "up" if is_bullish_setup else "down"
            rationale.append(
                f"✅ RSI ({rsi:.1f}) is {direction_str} (< {threshold} threshold) "
                f"and turning {turn_str} (prev={prev_rsi:.1f}) – momentum inflection."
            )
        else:
            if not rsi_extreme:
                threshold = self.rsi_oversold if is_bullish_setup else self.rsi_overbought
                rationale.append(
                    f"❌ RSI ({rsi:.1f}) has not reached "
                    f"{'oversold' if is_bullish_setup else 'overbought'} "
                    f"threshold ({threshold})."
                )
            else:
                rationale.append(
                    f"❌ RSI ({rsi:.1f}) is extreme but not yet turning "
                    f"({'prev' if is_bullish_setup else 'next'}={prev_rsi:.1f}) – "
                    "wait for inflection."
                )

        # Condition 3 – MACD histogram confirms direction
        cond3 = (
            macd_histogram > 0 if is_bullish_setup else macd_histogram < 0
        )
        if cond3:
            conditions_met += 1
            rationale.append(
                f"✅ MACD histogram ({macd_histogram:+.4f}) confirms "
                f"{'bullish' if is_bullish_setup else 'bearish'} momentum shift."
            )
        else:
            rationale.append(
                f"❌ MACD histogram ({macd_histogram:+.4f}) does NOT confirm "
                f"{'bullish' if is_bullish_setup else 'bearish'} direction."
            )

        # Condition 4 – Volume spike (> 2x avg)
        cond4 = avg_volume > 0 and volume >= self.volume_multiplier * avg_volume
        if cond4:
            conditions_met += 1
            vol_ratio = volume / avg_volume
            rationale.append(
                f"✅ Volume spike: {volume:,.0f} = {vol_ratio:.1f}x avg ({avg_volume:,.0f}) – "
                "institutional activity detected."
            )
        else:
            vol_ratio = volume / avg_volume if avg_volume > 0 else 0
            rationale.append(
                f"❌ Volume ({volume:,.0f}) is only {vol_ratio:.1f}x avg – "
                f"no significant spike (need {self.volume_multiplier}x)."
            )

        # Condition 5 – Near support (BUY) / resistance (SELL)
        if is_bullish_setup:
            if support_level > 0:
                dist_from_support = abs(ltp - support_level)
                cond5 = dist_from_support <= self.atr_support_window * atr
            else:
                cond5 = False
        else:
            if resistance_level > 0:
                dist_from_resistance = abs(ltp - resistance_level)
                cond5 = dist_from_resistance <= self.atr_support_window * atr
            else:
                cond5 = False

        if cond5:
            conditions_met += 1
            level = support_level if is_bullish_setup else resistance_level
            level_name = "support" if is_bullish_setup else "resistance"
            dist = abs(ltp - level)
            rationale.append(
                f"✅ Price within {dist:.2f} pts ({dist / atr:.2f} ATR) of key "
                f"{level_name} level ({level:.2f}) – within {self.atr_support_window} ATR window."
            )
        else:
            level_name = "support" if is_bullish_setup else "resistance"
            level = support_level if is_bullish_setup else resistance_level
            if level > 0:
                dist = abs(ltp - level)
                rationale.append(
                    f"❌ Price not near key {level_name} level ({level:.2f}); "
                    f"distance {dist:.2f} pts > {self.atr_support_window} ATR ({atr:.2f})."
                )
            else:
                rationale.append(
                    f"❌ No known {level_name} level provided in context."
                )

        # Condition 6 – Market regime is RANGE_BOUND or REVERSAL
        cond6 = regime in (MarketRegime.RANGE_BOUND, MarketRegime.REVERSAL)
        if cond6:
            conditions_met += 1
            rationale.append(
                f"✅ Market regime '{regime.value}' is conducive to mean reversion."
            )
        else:
            rationale.append(
                f"❌ Market regime '{regime.value}' is not ideal for mean reversion "
                "(expect RANGE_BOUND or REVERSAL)."
            )

        # ----------------------------------------------------------------
        # Confidence scoring
        # ----------------------------------------------------------------
        confidence: float = _CONFIDENCE_MAP.get(conditions_met, 0.0)

        self._logger.info(
            "%s | %s | direction=%s | conditions=%d/6 | confidence=%.2f",
            self.strategy_name,
            symbol,
            direction.value,
            conditions_met,
            confidence,
        )

        if confidence < self.min_confidence_threshold:
            self._logger.info(
                "Confidence %.2f below threshold %.2f; no signal.",
                confidence,
                self.min_confidence_threshold,
            )
            return None

        # ----------------------------------------------------------------
        # Risk management
        # ----------------------------------------------------------------
        if is_bullish_setup:
            # Stop: swing low, fallback to 1 ATR below entry
            stop_loss: float = (
                swing_low if swing_low > 0 and swing_low < ltp
                else ltp - atr
            )
            # Targets: VWAP → VWAP + 1 std dev
            target_1: float = round(vwap, 2)
            target_2: float = round(vwap + vwap_std_dev, 2) if vwap_std_dev > 0 else round(vwap * 1.003, 2)
        else:
            # Stop: swing high, fallback to 1 ATR above entry
            stop_loss = (
                swing_high if swing_high > 0 and swing_high > ltp
                else ltp + atr
            )
            # Targets: VWAP → VWAP − 1 std dev
            target_1 = round(vwap, 2)
            target_2 = round(vwap - vwap_std_dev, 2) if vwap_std_dev > 0 else round(vwap * 0.997, 2)

        stop_loss = round(stop_loss, 2)
        risk_per_unit: float = abs(ltp - stop_loss)

        rationale.append(
            f"📊 Risk mgmt: Entry={ltp:.2f}, SL={stop_loss:.2f} "
            f"({'swing_low' if (is_bullish_setup and swing_low > 0) else '1 ATR fallback'}), "
            f"T1 (VWAP)={target_1:.2f}, T2 (VWAP±1σ)={target_2:.2f}. "
            f"Risk/unit={risk_per_unit:.2f} pts."
        )
        rationale.append(
            f"⏰ Valid trading window: {_WINDOW_START.strftime('%H:%M')} – "
            f"{_WINDOW_END.strftime('%H:%M')} IST."
        )

        # ----------------------------------------------------------------
        # Build and return signal
        # ----------------------------------------------------------------
        signal = TradeSignal(
            strategy_name=self.strategy_name,
            instrument_type=instrument_type,
            symbol=symbol,
            underlying=underlying,
            direction=direction,
            entry_price=ltp,
            stop_loss=stop_loss,
            targets=[target_1, target_2],
            quantity=1,
            confidence=confidence,
            rationale=rationale,
            expiry=expiry,
            metadata={
                "vwap": vwap,
                "vwap_std_dev": vwap_std_dev,
                "vwap_pct_diff": round(vwap_pct_diff, 4),
                "rsi": rsi,
                "prev_rsi": prev_rsi,
                "macd_histogram": macd_histogram,
                "volume_ratio": round(volume / avg_volume, 2) if avg_volume > 0 else 0,
                "atr": atr,
                "regime": regime.value,
                "conditions_met": conditions_met,
                "timeframe": "intraday",
            },
        )

        self._logger.info("Signal emitted: %s", signal)
        return signal
