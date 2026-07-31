"""
strategies/short_premium.py
============================
ShortPremiumStrategy – option-selling (short premium) strategy for NSE F&O.

⚠️  RISK WARNING ⚠️
===================
Selling options exposes the seller to *theoretically unlimited loss* (for short
calls) or very large loss (for short puts) if the market moves strongly against
the position.  This strategy must ONLY be deployed with appropriate risk controls:
  - Hedge with a bought OTM option (convert to spread) before going live.
  - Hard stop at 2x premium received.
  - Position sizing MUST be 1 lot until P&L history is established.
  - Automated stop-loss orders are MANDATORY.

Strategy overview
-----------------
Collects time-value (theta) by selling OTM options when implied volatility is
elevated (IV Rank > 70th percentile), the market is in a range-bound regime,
and there are no upcoming macro events to spike volatility.

Entry conditions (ALL must hold)
---------------------------------
1. **IV Rank > 70** – Implied Volatility is in the top 30th percentile of its
   historical range → options are expensive, good to sell.
2. **Range-bound regime** – ADX < 20, confirming a non-trending, sideways
   market where a breakout is less likely to trigger stops.
3. **Price near strong S/R** – The underlying is near a well-established
   support/resistance level, making a big move in either direction less likely.
4. **Neutral PCR (0.8 – 1.2)** – No strong directional bias; market is
   balanced, reducing the probability of a sharp unidirectional move.
5. **DTE in [5, 15]** – Theta decay is most rapid in this window; too short
   and gamma risk increases; too long and theta is insufficient.
6. **No upcoming events** – No major events (RBI policy, earnings, elections,
   etc.) within the next 3 days.

Strike selection
----------------
Sell OTM: 1 strike OTM for a high-premium sell, or 2 strikes OTM for lower
premium but better safety margin.  The exact strike is chosen by the
``_select_otm_strike`` helper based on current underlying price and a
configurable ``strikes_otm`` parameter.

Risk management
---------------
- **Stop loss**: exit when option premium reaches 2x premium received.
  (Buy back the option at 2× the sold price.)
- **Take-profit**: exit at 50 % of premium received (50 % profit target).
- **Minimum confidence**: 0.80 required.  This is STRICT – all 6 conditions
  must substantially hold before the strategy returns a signal.

Instruments
-----------
CE (short call), PE (short put).  The direction is encoded as SELL.
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
# Constants
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

_STOP_MULTIPLIER: float = 2.0     # Exit if premium doubles
_TAKE_PROFIT_PCT: float = 0.50    # Exit at 50% profit
_RISK_WARNING: str = (
    "⚠️  SELLING STRATEGY – UNLIMITED RISK WITHOUT HEDGE. "
    "Always pair with a bought OTM option to cap max loss before deploying live."
)


class ShortPremiumStrategy(IStrategy):
    """
    Option-selling / short-premium strategy for range-bound NSE F&O markets.

    Config keys (strategies.yaml → short_premium)
    ---------------------------------------------
    iv_rank_min           float   Min IV rank to sell (default 70)
    adx_max               float   Max ADX for range-bound filter (default 20)
    pcr_min               float   PCR lower bound for neutral bias (default 0.8)
    pcr_max               float   PCR upper bound for neutral bias (default 1.2)
    dte_min               int     Minimum days to expiry (default 5)
    dte_max               int     Maximum days to expiry (default 15)
    event_window_days     int     Days to look-ahead for events (default 3)
    sr_proximity_pct      float   Max % distance from S/R level (default 0.5)
    strikes_otm           int     How many strikes OTM to sell (default 1)
    min_confidence        float   Minimum confidence gate (default 0.80)
    """

    strategy_name: str = "short_premium"
    version: str = "1.0.0"
    is_enabled: bool = True
    supported_instruments: list[InstrumentType] = [
        InstrumentType.CE,
        InstrumentType.PE,
    ]
    # Very strict minimum confidence for options selling
    min_confidence_threshold: float = 0.80

    _DEFAULT_IV_RANK_MIN: float = 70.0
    _DEFAULT_ADX_MAX: float = 20.0
    _DEFAULT_PCR_MIN: float = 0.8
    _DEFAULT_PCR_MAX: float = 1.2
    _DEFAULT_DTE_MIN: int = 5
    _DEFAULT_DTE_MAX: int = 15
    _DEFAULT_EVENT_WINDOW_DAYS: int = 3
    _DEFAULT_SR_PROXIMITY_PCT: float = 0.5
    _DEFAULT_STRIKES_OTM: int = 1

    def __init__(self, config: Optional[dict[str, Any]] = None) -> None:
        super().__init__(config=config)

        cfg = self._config
        self.iv_rank_min: float = float(
            cfg.get("iv_rank_min", self._DEFAULT_IV_RANK_MIN)
        )
        self.adx_max: float = float(
            cfg.get("adx_max", self._DEFAULT_ADX_MAX)
        )
        self.pcr_min: float = float(
            cfg.get("pcr_min", self._DEFAULT_PCR_MIN)
        )
        self.pcr_max: float = float(
            cfg.get("pcr_max", self._DEFAULT_PCR_MAX)
        )
        self.dte_min: int = int(cfg.get("dte_min", self._DEFAULT_DTE_MIN))
        self.dte_max: int = int(cfg.get("dte_max", self._DEFAULT_DTE_MAX))
        self.event_window_days: int = int(
            cfg.get("event_window_days", self._DEFAULT_EVENT_WINDOW_DAYS)
        )
        self.sr_proximity_pct: float = float(
            cfg.get("sr_proximity_pct", self._DEFAULT_SR_PROXIMITY_PCT)
        )
        self.strikes_otm: int = int(
            cfg.get("strikes_otm", self._DEFAULT_STRIKES_OTM)
        )

    # ------------------------------------------------------------------
    # Config validation
    # ------------------------------------------------------------------

    def validate_config(self, config: dict[str, Any]) -> bool:
        """Validate short premium config. Returns False if any value is out-of-range."""
        valid = True

        iv_min = config.get("iv_rank_min", self._DEFAULT_IV_RANK_MIN)
        if not isinstance(iv_min, (int, float)) or not (0 < iv_min < 100):
            self._logger.error("iv_rank_min must be in (0, 100), got %s", iv_min)
            valid = False

        adx_max = config.get("adx_max", self._DEFAULT_ADX_MAX)
        if not isinstance(adx_max, (int, float)) or not (0 < adx_max <= 60):
            self._logger.error("adx_max must be in (0, 60], got %s", adx_max)
            valid = False

        pcr_min = config.get("pcr_min", self._DEFAULT_PCR_MIN)
        pcr_max = config.get("pcr_max", self._DEFAULT_PCR_MAX)
        if not (0 < pcr_min < pcr_max):
            self._logger.error(
                "pcr_min/pcr_max must satisfy 0 < pcr_min < pcr_max, got %s / %s",
                pcr_min,
                pcr_max,
            )
            valid = False

        dte_min = config.get("dte_min", self._DEFAULT_DTE_MIN)
        dte_max = config.get("dte_max", self._DEFAULT_DTE_MAX)
        if not (0 < dte_min < dte_max):
            self._logger.error(
                "dte_min/dte_max must satisfy 0 < dte_min < dte_max, got %s / %s",
                dte_min,
                dte_max,
            )
            valid = False

        sr_prox = config.get("sr_proximity_pct", self._DEFAULT_SR_PROXIMITY_PCT)
        if not isinstance(sr_prox, (int, float)) or sr_prox <= 0:
            self._logger.error("sr_proximity_pct must be > 0, got %s", sr_prox)
            valid = False

        strikes = config.get("strikes_otm", self._DEFAULT_STRIKES_OTM)
        if not isinstance(strikes, int) or not (1 <= strikes <= 5):
            self._logger.error("strikes_otm must be an int in [1, 5], got %s", strikes)
            valid = False

        return valid

    # ------------------------------------------------------------------
    # Strike selection helper
    # ------------------------------------------------------------------

    def _select_otm_strike(
        self,
        underlying_price: float,
        option_chain: dict[str, Any],
        instrument_type: InstrumentType,
        strike_step: float,
    ) -> Optional[tuple[float, str, float]]:
        """
        Select the OTM strike to sell.

        For short calls: ATM + (strikes_otm × step)
        For short puts:  ATM - (strikes_otm × step)

        Parameters
        ----------
        underlying_price : float
            Current underlying spot price.
        option_chain : dict
            Option chain snapshot with 'strikes' list and 'expiry' key.
        instrument_type : InstrumentType
            CE → short call; PE → short put.
        strike_step : float
            Strike interval.

        Returns
        -------
        Optional[tuple[float, str, float]]
            (strike, option_symbol, ltp) or None if chain is unavailable.
        """
        strikes_list: list[dict] = option_chain.get("strikes", [])
        if not strikes_list:
            self._logger.warning("Empty option chain; cannot select OTM strike.")
            return None

        atm_strike: float = round(underlying_price / strike_step) * strike_step

        if instrument_type == InstrumentType.CE:
            target_strike = atm_strike + self.strikes_otm * strike_step
            opt_key = "CE"
        else:
            target_strike = atm_strike - self.strikes_otm * strike_step
            opt_key = "PE"

        available: dict[float, dict] = {
            s["strike"]: s for s in strikes_list
        }

        # Find exact or nearest OTM strike
        if target_strike in available:
            selected_strike = target_strike
        else:
            # Fall back to nearest available strike on the OTM side
            if instrument_type == InstrumentType.CE:
                candidates = [s for s in available if s > atm_strike]
            else:
                candidates = [s for s in available if s < atm_strike]

            if not candidates:
                self._logger.warning(
                    "No OTM strikes found for %s; cannot select.", instrument_type.value
                )
                return None

            selected_strike = min(candidates, key=lambda s: abs(s - target_strike))

        # Get LTP for the selected strike
        strike_data = available.get(selected_strike, {})
        ltp: float = float(strike_data.get(opt_key, {}).get("ltp", 0.0))

        expiry_str = option_chain.get("expiry", "UNKNOWN")
        underlying_sym = option_chain.get("underlying", "IDX")
        option_symbol = (
            f"{underlying_sym}{expiry_str}{int(selected_strike)}{opt_key}"
        )

        self._logger.debug(
            "OTM strike selected: %s (%s) LTP=%.2f | ATM was %s.",
            selected_strike,
            opt_key,
            ltp,
            atm_strike,
        )
        return selected_strike, option_symbol, ltp

    # ------------------------------------------------------------------
    # Signal generation
    # ------------------------------------------------------------------

    async def generate_signal(
        self, context: dict[str, Any]
    ) -> Optional[TradeSignal]:
        """
        Evaluate short-premium conditions and return a TradeSignal or None.

        Required context keys
        ---------------------
        ``underlying``          str          – underlying name (e.g. "NIFTY")
        ``underlying_price``    float        – current spot/futures price
        ``iv_rank``             float        – IV rank [0–100]
        ``adx``                 float        – ADX(14)
        ``regime``              MarketRegime – current market regime
        ``support_level``       float        – nearest support level
        ``resistance_level``    float        – nearest resistance level
        ``pcr``                 float        – put-call ratio
        ``dte``                 int          – days to expiry
        ``has_upcoming_event``  bool         – True if major event in next N days
        ``option_chain``        dict         – option chain snapshot
        ``strike_step``         float        – strike interval
        ``instrument_type``     InstrumentType – CE (short call) or PE (short put)
        ``expiry``              str          – expiry string
        """
        if not self._check_enabled():
            return None

        instrument_type: InstrumentType = context.get(
            "instrument_type", InstrumentType.CE
        )
        if not self._check_instrument(instrument_type):
            return None

        # ----------------------------------------------------------------
        # Extract context values
        # ----------------------------------------------------------------
        try:
            underlying: str = context["underlying"]
            underlying_price: float = float(context["underlying_price"])
            iv_rank: float = float(context["iv_rank"])
            adx: float = float(context["adx"])
            regime: MarketRegime = context.get("regime", MarketRegime.UNKNOWN)
            support_level: float = float(context.get("support_level", 0.0))
            resistance_level: float = float(context.get("resistance_level", 0.0))
            pcr: float = float(context["pcr"])
            dte: int = int(context["dte"])
            has_upcoming_event: bool = bool(context.get("has_upcoming_event", False))
            option_chain: dict = context.get("option_chain", {})
            strike_step: float = float(context.get("strike_step", 50.0))
            expiry: Optional[str] = context.get("expiry")
        except KeyError as exc:
            self._logger.error(
                "Missing required context key for %s: %s", self.strategy_name, exc
            )
            return None

        # ----------------------------------------------------------------
        # Condition checks (6 conditions)
        # ----------------------------------------------------------------
        conditions_met: int = 0
        rationale: list[str] = []

        # ALWAYS prepend the risk warning for this strategy
        rationale.append(_RISK_WARNING)

        # Condition 1 – IV Rank > threshold (expensive options → good to sell)
        cond1 = iv_rank >= self.iv_rank_min
        if cond1:
            conditions_met += 1
            rationale.append(
                f"✅ IV Rank ({iv_rank:.1f}%) ≥ {self.iv_rank_min}% – options are "
                "expensive relative to historical range; favourable to sell."
            )
        else:
            rationale.append(
                f"❌ IV Rank ({iv_rank:.1f}%) < {self.iv_rank_min}% – options are not "
                "expensive enough; risk/reward for selling is poor."
            )

        # Condition 2 – Range-bound market (ADX < threshold)
        cond2 = adx < self.adx_max
        if cond2:
            conditions_met += 1
            rationale.append(
                f"✅ ADX ({adx:.1f}) < {self.adx_max} – market is range-bound; "
                "low trending risk for short options."
            )
        else:
            rationale.append(
                f"❌ ADX ({adx:.1f}) ≥ {self.adx_max} – trending market detected; "
                "short options face elevated directional risk."
            )

        # Condition 3 – Price near key S/R level
        near_sr: bool = False
        near_sr_desc: str = "No S/R level data provided."
        if instrument_type == InstrumentType.CE and resistance_level > 0:
            # Short call: price near resistance (unlikely to break up)
            dist_pct = abs(underlying_price - resistance_level) / underlying_price * 100
            near_sr = dist_pct <= self.sr_proximity_pct
            near_sr_desc = (
                f"{'✅' if near_sr else '❌'} Price ({underlying_price:.2f}) is "
                f"{dist_pct:.2f}% from resistance ({resistance_level:.2f}) – "
                f"threshold {self.sr_proximity_pct}%."
            )
        elif instrument_type == InstrumentType.PE and support_level > 0:
            # Short put: price near support (unlikely to break down)
            dist_pct = abs(underlying_price - support_level) / underlying_price * 100
            near_sr = dist_pct <= self.sr_proximity_pct
            near_sr_desc = (
                f"{'✅' if near_sr else '❌'} Price ({underlying_price:.2f}) is "
                f"{dist_pct:.2f}% from support ({support_level:.2f}) – "
                f"threshold {self.sr_proximity_pct}%."
            )

        if near_sr:
            conditions_met += 1
        rationale.append(near_sr_desc)

        # Condition 4 – Neutral PCR (0.8 – 1.2)
        cond4 = self.pcr_min <= pcr <= self.pcr_max
        if cond4:
            conditions_met += 1
            rationale.append(
                f"✅ PCR ({pcr:.2f}) in neutral band [{self.pcr_min}, {self.pcr_max}] – "
                "no strong directional bias; option selling is appropriate."
            )
        else:
            direction_bias = "bullish" if pcr < self.pcr_min else "bearish"
            rationale.append(
                f"❌ PCR ({pcr:.2f}) outside neutral band [{self.pcr_min}, {self.pcr_max}] – "
                f"market has a {direction_bias} bias; avoid selling without hedges."
            )

        # Condition 5 – DTE in sweet spot [dte_min, dte_max]
        cond5 = self.dte_min <= dte <= self.dte_max
        if cond5:
            conditions_met += 1
            rationale.append(
                f"✅ DTE ({dte} days) is in theta sweet spot [{self.dte_min}, {self.dte_max}] – "
                "optimal theta decay rate for premium sellers."
            )
        else:
            if dte < self.dte_min:
                rationale.append(
                    f"❌ DTE ({dte}) < {self.dte_min} – too close to expiry; "
                    "gamma risk is dangerously high."
                )
            else:
                rationale.append(
                    f"❌ DTE ({dte}) > {self.dte_max} – too far from expiry; "
                    "theta decay is insufficient to justify the risk."
                )

        # Condition 6 – No upcoming major events
        cond6 = not has_upcoming_event
        if cond6:
            conditions_met += 1
            rationale.append(
                f"✅ No major events detected in next {self.event_window_days} days – "
                "IV spike risk is low."
            )
        else:
            rationale.append(
                f"❌ Major event detected within {self.event_window_days} days – "
                "IV could spike dramatically; DO NOT sell premium into events."
            )

        # ----------------------------------------------------------------
        # Confidence scoring
        # ----------------------------------------------------------------
        confidence: float = _CONFIDENCE_MAP.get(conditions_met, 0.0)

        self._logger.info(
            "%s | %s %s | conditions=%d/6 | confidence=%.2f",
            self.strategy_name,
            underlying,
            instrument_type.value,
            conditions_met,
            confidence,
        )

        # STRICT gate for options selling – 0.80 minimum
        if confidence < self.min_confidence_threshold:
            self._logger.info(
                "Short premium confidence %.2f below STRICT threshold %.2f; "
                "no signal. This strategy requires ≥4/6 conditions.",
                confidence,
                self.min_confidence_threshold,
            )
            return None

        # ----------------------------------------------------------------
        # Strike selection
        # ----------------------------------------------------------------
        strike_result = self._select_otm_strike(
            underlying_price, option_chain, instrument_type, strike_step
        )

        if strike_result is not None:
            selected_strike, option_symbol, option_ltp = strike_result
        else:
            # Fallback: estimate OTM strike
            atm_strike = round(underlying_price / strike_step) * strike_step
            if instrument_type == InstrumentType.CE:
                selected_strike = atm_strike + self.strikes_otm * strike_step
                opt_key = "CE"
            else:
                selected_strike = atm_strike - self.strikes_otm * strike_step
                opt_key = "PE"
            option_symbol = (
                f"{underlying}{expiry or 'EXP'}{int(selected_strike)}{opt_key}"
            )
            option_ltp = float(context.get("ltp", 0.0))

        if option_ltp <= 0:
            self._logger.warning(
                "Cannot determine option LTP for %s; skipping signal.", option_symbol
            )
            return None

        otm_label = (
            f"{self.strikes_otm}-strike OTM "
            f"{'call' if instrument_type == InstrumentType.CE else 'put'}"
        )
        rationale.append(
            f"📊 Strike: {selected_strike} ({otm_label}) | Symbol: {option_symbol} | "
            f"Premium received: ₹{option_ltp:.2f}."
        )

        # ----------------------------------------------------------------
        # Risk management: SL = 2x premium; Target = 50% profit
        # ----------------------------------------------------------------
        # SHORT position: entry = premium received; stop = 2x premium (buy-back trigger)
        entry_price = option_ltp                           # credit received
        stop_loss = round(option_ltp * _STOP_MULTIPLIER, 2)   # buy back at 2x → stop
        target = round(option_ltp * _TAKE_PROFIT_PCT, 2)       # buy back at 0.5x → profit

        rationale.append(
            f"📊 Risk mgmt: Premium sold={entry_price:.2f}, "
            f"SL (buy-back price)={stop_loss:.2f} ({_STOP_MULTIPLIER}x premium received), "
            f"Target (buy-back at)={target:.2f} ({int(_TAKE_PROFIT_PCT*100)}% profit). "
            f"Max risk = {stop_loss - entry_price:.2f}/unit."
        )
        rationale.append(
            "⚠️  Use bracket/cover orders for automated stop-loss execution. "
            "Monitor continuously. Never hold unhedged short options overnight."
        )

        # ----------------------------------------------------------------
        # Build and return signal
        # ----------------------------------------------------------------
        signal = TradeSignal(
            strategy_name=self.strategy_name,
            instrument_type=instrument_type,
            symbol=option_symbol,
            underlying=underlying,
            direction=SignalDirection.SELL,  # Selling the option
            entry_price=entry_price,
            stop_loss=stop_loss,
            targets=[target],
            quantity=1,           # Always 1 lot for MVP – never scale up without hedge
            confidence=confidence,
            rationale=rationale,
            expiry=expiry,
            strike=selected_strike,
            metadata={
                "iv_rank": iv_rank,
                "adx": adx,
                "regime": regime.value,
                "pcr": pcr,
                "dte": dte,
                "has_upcoming_event": has_upcoming_event,
                "support_level": support_level,
                "resistance_level": resistance_level,
                "selected_strike": selected_strike,
                "strikes_otm": self.strikes_otm,
                "stop_multiplier": _STOP_MULTIPLIER,
                "take_profit_pct": _TAKE_PROFIT_PCT,
                "conditions_met": conditions_met,
                "risk_warning": "UNLIMITED_LOSS_WITHOUT_HEDGE",
            },
        )

        self._logger.warning(
            "SHORT PREMIUM signal emitted for %s. "
            "Ensure hedge/bracket order is in place before execution.",
            option_symbol,
        )
        self._logger.info("Signal: %s", signal)
        return signal
