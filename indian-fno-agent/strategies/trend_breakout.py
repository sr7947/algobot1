"""
strategies/trend_breakout.py
=============================
TrendBreakoutStrategy – momentum / breakout strategy for NSE F&O Futures.

Strategy overview
-----------------
Captures strong directional breakouts confirmed by multiple technical filters.
Primarily designed for Futures (FUT) on liquid NSE F&O names (NIFTY, BANKNIFTY,
large-cap individual stocks).

Entry conditions (ALL must be True for maximum confidence)
-----------------------------------------------------------
1. **Price breakout** – Current price > 20-day rolling high (intraday candle
   close basis on 15-minute timeframe).
2. **Volume confirmation** – Current bar volume > 1.5x 20-period average volume.
3. **Trend filter (ADX)** – ADX(14) > 25 (confirms trending market, filters
   sideways noise).
4. **EMA stack** – EMA(21) > EMA(50) > EMA(200) (bullish alignment across
   short / medium / long-term).
5. **VWAP filter** – Price above session VWAP (institutional bias is bullish).
6. **RSI filter** – RSI(14) between 50 and 70 (momentum present, not overbought).
7. **News block absent** – No major news event blocking fresh positions.

Risk management
---------------
- **Stop loss**: lower of (breakout candle low) or (entry − 1 ATR).
- **Target 1**: entry + 2.5 × risk  (R:R = 2.5:1).
- **Position size**: 1 lot (base); 2 lots when confidence = 1.0 and daily
  loss budget is not exhausted.

Confidence scoring
------------------
6 binary conditions → 6/6 = 1.00, 5/6 = 0.85, 4/6 = 0.70, 3/6 = 0.55 …
Signals with confidence < min_confidence_threshold (default 0.70) are dropped.
"""

from __future__ import annotations

import logging
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
# Confidence score map: conditions_met → confidence
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

# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class TrendBreakoutStrategy(IStrategy):
    """
    Trend-following breakout strategy for NSE F&O Futures.

    Config keys (strategies.yaml → trend_breakout)
    -----------------------------------------------
    breakout_lookback       int     Lookback period for 20-day high (default 20)
    volume_multiplier       float   Volume threshold multiplier (default 1.5)
    adx_threshold           float   ADX minimum (default 25)
    rsi_min                 float   RSI lower bound (default 50)
    rsi_max                 float   RSI upper bound (default 70)
    reward_to_risk          float   R:R multiple for target (default 2.5)
    min_confidence          float   Minimum confidence gate (default 0.70)
    base_lots               int     Default position size in lots (default 1)
    max_lots                int     Max position size in lots (default 2)
    timeframe               str     Expected candle timeframe string (default "15m")
    """

    strategy_name: str = "trend_breakout"
    version: str = "1.0.0"
    is_enabled: bool = True
    supported_instruments: list[InstrumentType] = [InstrumentType.FUT]
    min_confidence_threshold: float = 0.70  # require at least 4/6 conditions

    # Default parameter values (overridden by config YAML)
    _DEFAULT_BREAKOUT_LOOKBACK: int = 20
    _DEFAULT_VOLUME_MULTIPLIER: float = 1.5
    _DEFAULT_ADX_THRESHOLD: float = 25.0
    _DEFAULT_RSI_MIN: float = 50.0
    _DEFAULT_RSI_MAX: float = 70.0
    _DEFAULT_REWARD_TO_RISK: float = 2.5
    _DEFAULT_BASE_LOTS: int = 1
    _DEFAULT_MAX_LOTS: int = 2

    def __init__(self, config: Optional[dict[str, Any]] = None) -> None:
        super().__init__(config=config)

        # Bind config values to typed instance attributes for fast access
        cfg = self._config
        self.breakout_lookback: int = int(
            cfg.get("breakout_lookback", self._DEFAULT_BREAKOUT_LOOKBACK)
        )
        self.volume_multiplier: float = float(
            cfg.get("volume_multiplier", self._DEFAULT_VOLUME_MULTIPLIER)
        )
        self.adx_threshold: float = float(
            cfg.get("adx_threshold", self._DEFAULT_ADX_THRESHOLD)
        )
        self.rsi_min: float = float(cfg.get("rsi_min", self._DEFAULT_RSI_MIN))
        self.rsi_max: float = float(cfg.get("rsi_max", self._DEFAULT_RSI_MAX))
        self.reward_to_risk: float = float(
            cfg.get("reward_to_risk", self._DEFAULT_REWARD_TO_RISK)
        )
        self.base_lots: int = int(cfg.get("base_lots", self._DEFAULT_BASE_LOTS))
        self.max_lots: int = int(cfg.get("max_lots", self._DEFAULT_MAX_LOTS))

    # ------------------------------------------------------------------
    # Config validation
    # ------------------------------------------------------------------

    def validate_config(self, config: dict[str, Any]) -> bool:
        """
        Validate that numeric config values are within sensible ranges.

        Returns False (and logs each issue) if any value is out-of-range.
        """
        valid = True

        lookback = config.get("breakout_lookback", self._DEFAULT_BREAKOUT_LOOKBACK)
        if not isinstance(lookback, int) or lookback < 5:
            self._logger.error(
                "breakout_lookback must be an int >= 5, got %s", lookback
            )
            valid = False

        vol_mult = config.get("volume_multiplier", self._DEFAULT_VOLUME_MULTIPLIER)
        if not isinstance(vol_mult, (int, float)) or vol_mult < 1.0:
            self._logger.error(
                "volume_multiplier must be >= 1.0, got %s", vol_mult
            )
            valid = False

        adx = config.get("adx_threshold", self._DEFAULT_ADX_THRESHOLD)
        if not isinstance(adx, (int, float)) or not (10 <= adx <= 60):
            self._logger.error(
                "adx_threshold must be in [10, 60], got %s", adx
            )
            valid = False

        rsi_min = config.get("rsi_min", self._DEFAULT_RSI_MIN)
        rsi_max = config.get("rsi_max", self._DEFAULT_RSI_MAX)
        if not (0 < rsi_min < rsi_max < 100):
            self._logger.error(
                "rsi_min/rsi_max must satisfy 0 < rsi_min < rsi_max < 100, "
                "got %s / %s",
                rsi_min,
                rsi_max,
            )
            valid = False

        rr = config.get("reward_to_risk", self._DEFAULT_REWARD_TO_RISK)
        if not isinstance(rr, (int, float)) or rr < 1.0:
            self._logger.error("reward_to_risk must be >= 1.0, got %s", rr)
            valid = False

        return valid

    # ------------------------------------------------------------------
    # Signal generation
    # ------------------------------------------------------------------

    async def generate_signal(
        self, context: dict[str, Any]
    ) -> Optional[TradeSignal]:
        """
        Evaluate trend-breakout conditions and return a TradeSignal or None.

        Parameters
        ----------
        context : dict
            Market context bundle.  Required keys for this strategy:

            ``ohlcv``        list[dict]   – candle list (each dict has keys:
                                            open, high, low, close, volume)
            ``ltp``          float        – last traded price (current close)
            ``volume``       float        – current bar volume
            ``avg_volume``   float        – rolling average volume
            ``adx``          float        – ADX(14)
            ``ema_21``       float        – EMA(21)
            ``ema_50``       float        – EMA(50)
            ``ema_200``      float        – EMA(200)
            ``vwap``         float        – session VWAP
            ``rsi``          float        – RSI(14)
            ``atr``          float        – ATR(14)
            ``has_news_block`` bool       – True if news event blocks trading
            ``symbol``       str          – trading symbol
            ``underlying``   str          – underlying asset
            ``instrument_type`` InstrumentType
            ``expiry``       str          – futures expiry string

        Returns
        -------
        Optional[TradeSignal]
            Populated signal or None.
        """
        # --- Guard: strategy enabled? ---
        if not self._check_enabled():
            return None

        # --- Guard: correct instrument? ---
        instrument_type: InstrumentType = context.get(
            "instrument_type", InstrumentType.FUT
        )
        if not self._check_instrument(instrument_type):
            return None

        # --- Extract required context values ---
        try:
            ohlcv: list[dict[str, float]] = context["ohlcv"]
            ltp: float = float(context["ltp"])
            volume: float = float(context["volume"])
            avg_volume: float = float(context["avg_volume"])
            adx: float = float(context["adx"])
            ema_21: float = float(context["ema_21"])
            ema_50: float = float(context["ema_50"])
            ema_200: float = float(context["ema_200"])
            vwap: float = float(context["vwap"])
            rsi: float = float(context["rsi"])
            atr: float = float(context["atr"])
            has_news_block: bool = bool(context.get("has_news_block", False))
            symbol: str = context["symbol"]
            underlying: str = context["underlying"]
            expiry: Optional[str] = context.get("expiry")
        except KeyError as exc:
            self._logger.error(
                "Missing required context key for %s: %s", self.strategy_name, exc
            )
            return None

        # Validate minimum number of candles for lookback
        if len(ohlcv) < self.breakout_lookback + 1:
            self._logger.warning(
                "Insufficient OHLCV data: need %d candles, got %d.",
                self.breakout_lookback + 1,
                len(ohlcv),
            )
            return None

        # --- Compute 20-period high (exclude current bar) ---
        lookback_candles = ohlcv[-(self.breakout_lookback + 1) : -1]
        period_high: float = max(c["high"] for c in lookback_candles)
        current_candle_low: float = ohlcv[-1]["low"]

        # ----------------------------------------------------------------
        # Condition checks
        # ----------------------------------------------------------------
        conditions_met: int = 0
        rationale: list[str] = []

        # Condition 1 – Price breaks above N-period high
        cond1 = ltp > period_high
        if cond1:
            conditions_met += 1
            rationale.append(
                f"✅ Price ({ltp:.2f}) broke above {self.breakout_lookback}-period high "
                f"({period_high:.2f})."
            )
        else:
            rationale.append(
                f"❌ Price ({ltp:.2f}) has NOT broken above {self.breakout_lookback}-period "
                f"high ({period_high:.2f})."
            )

        # Condition 2 – Volume confirmation (> 1.5x avg)
        cond2 = avg_volume > 0 and volume >= self.volume_multiplier * avg_volume
        if cond2:
            conditions_met += 1
            rationale.append(
                f"✅ Volume ({volume:,.0f}) is {volume / avg_volume:.1f}x average "
                f"({avg_volume:,.0f}) – breakout confirmed by volume."
            )
        else:
            vol_ratio = volume / avg_volume if avg_volume > 0 else 0
            rationale.append(
                f"❌ Volume ratio {vol_ratio:.1f}x below threshold "
                f"{self.volume_multiplier}x – weak breakout volume."
            )

        # Condition 3 – ADX > threshold (trending market)
        cond3 = adx >= self.adx_threshold
        if cond3:
            conditions_met += 1
            rationale.append(
                f"✅ ADX ({adx:.1f}) > {self.adx_threshold} – trending market confirmed."
            )
        else:
            rationale.append(
                f"❌ ADX ({adx:.1f}) < {self.adx_threshold} – market may be ranging; "
                "breakout is less reliable."
            )

        # Condition 4 – Bullish EMA stack (EMA21 > EMA50 > EMA200)
        cond4 = ema_21 > ema_50 > ema_200
        if cond4:
            conditions_met += 1
            rationale.append(
                f"✅ Bullish EMA stack: EMA21 ({ema_21:.2f}) > EMA50 ({ema_50:.2f}) > "
                f"EMA200 ({ema_200:.2f})."
            )
        else:
            rationale.append(
                f"❌ EMA stack not bullish: EMA21={ema_21:.2f}, EMA50={ema_50:.2f}, "
                f"EMA200={ema_200:.2f}."
            )

        # Condition 5 – Price above VWAP
        cond5 = ltp > vwap
        if cond5:
            conditions_met += 1
            pct_above = (ltp - vwap) / vwap * 100
            rationale.append(
                f"✅ Price ({ltp:.2f}) is {pct_above:.2f}% above VWAP ({vwap:.2f}) – "
                "institutional bias is bullish."
            )
        else:
            pct_below = (vwap - ltp) / vwap * 100
            rationale.append(
                f"❌ Price ({ltp:.2f}) is {pct_below:.2f}% below VWAP ({vwap:.2f}) – "
                "caution: price below VWAP."
            )

        # Condition 6 – RSI in momentum zone [50, 70]
        cond6 = self.rsi_min <= rsi <= self.rsi_max
        if cond6:
            conditions_met += 1
            rationale.append(
                f"✅ RSI ({rsi:.1f}) in momentum zone [{self.rsi_min}, {self.rsi_max}] – "
                "good momentum, not overbought."
            )
        else:
            if rsi > self.rsi_max:
                rationale.append(
                    f"❌ RSI ({rsi:.1f}) > {self.rsi_max} – overbought; risk of pullback."
                )
            else:
                rationale.append(
                    f"❌ RSI ({rsi:.1f}) < {self.rsi_min} – insufficient bullish momentum."
                )

        # Special veto: news block overrides all signals
        if has_news_block:
            rationale.append(
                "⛔ NEWS BLOCK active – no new positions during major event window."
            )
            self._logger.info(
                "News block active for %s; signal vetoed.", symbol
            )
            return None

        # ----------------------------------------------------------------
        # Confidence scoring
        # ----------------------------------------------------------------
        confidence: float = _CONFIDENCE_MAP.get(conditions_met, 0.0)

        self._logger.info(
            "%s | %s | conditions met: %d/6 | confidence: %.2f",
            self.strategy_name,
            symbol,
            conditions_met,
            confidence,
        )

        # Drop weak signals
        if confidence < self.min_confidence_threshold:
            self._logger.info(
                "Signal confidence %.2f below threshold %.2f; no signal emitted.",
                confidence,
                self.min_confidence_threshold,
            )
            return None

        # ----------------------------------------------------------------
        # Risk management calculations
        # ----------------------------------------------------------------
        # Stop loss: min(breakout candle low, entry − 1 ATR)
        sl_by_candle_low: float = current_candle_low
        sl_by_atr: float = ltp - atr
        stop_loss: float = min(sl_by_candle_low, sl_by_atr)

        risk_per_unit: float = ltp - stop_loss
        if risk_per_unit <= 0:
            self._logger.warning(
                "Computed risk_per_unit <= 0 for %s; skipping signal.", symbol
            )
            return None

        target_price: float = round(ltp + self.reward_to_risk * risk_per_unit, 2)

        # Position sizing: 2 lots for perfect score, else 1 lot
        quantity: int = self.max_lots if confidence == 1.0 else self.base_lots

        rationale.append(
            f"📊 Risk mgmt: Entry={ltp:.2f}, SL={stop_loss:.2f} "
            f"(candle_low={sl_by_candle_low:.2f}, ATR_SL={sl_by_atr:.2f}), "
            f"T1={target_price:.2f} ({self.reward_to_risk}:1 R:R), "
            f"Lots={quantity}."
        )

        # ----------------------------------------------------------------
        # Build and return signal
        # ----------------------------------------------------------------
        signal = TradeSignal(
            strategy_name=self.strategy_name,
            instrument_type=InstrumentType.FUT,
            symbol=symbol,
            underlying=underlying,
            direction=SignalDirection.BUY,
            entry_price=ltp,
            stop_loss=round(stop_loss, 2),
            targets=[target_price],
            quantity=quantity,
            confidence=confidence,
            rationale=rationale,
            expiry=expiry,
            metadata={
                "period_high": period_high,
                "volume_ratio": round(volume / avg_volume, 2) if avg_volume > 0 else 0,
                "adx": adx,
                "ema_21": ema_21,
                "ema_50": ema_50,
                "ema_200": ema_200,
                "vwap": vwap,
                "rsi": rsi,
                "atr": atr,
                "conditions_met": conditions_met,
                "timeframe": "15m",
            },
        )

        self._logger.info("Signal emitted: %s", signal)
        return signal
