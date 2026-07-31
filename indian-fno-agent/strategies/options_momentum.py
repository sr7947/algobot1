"""
strategies/options_momentum.py
===============================
OptionsMomentumStrategy – directional options-buying strategy for NSE F&O.

Strategy overview
-----------------
Buys Call or Put options on NIFTY, BANKNIFTY, and FINNIFTY (occasionally
large-cap liquid names) when a strong directional move in the underlying is
confirmed by Open Interest (OI) buildup, PCR bias, and cheap implied volatility.

The strategy favours ATM or 1-strike OTM options for the best balance of
leverage and delta sensitivity.

Entry conditions
----------------
1. **Underlying move** – The underlying has moved > 0.5 % up (for CE) or down
   (for PE) in the last 15 minutes.
2. **OI buildup** – Calls are being bought with *rising* OI (CE momentum case);
   Puts being bought with rising OI (PE case).
3. **PCR confirms bias** – Put-Call Ratio < 0.8 confirms call-buying pressure;
   PCR > 1.2 confirms put-buying pressure.
4. **IV percentile** – Option IV is at or below the 50th percentile of its
   historical range (cheap to buy).
5. **Strike selection** – ATM or 1-strike OTM option is selected from the
   live option chain.
6. **Time filter** – Entry only before 14:00 IST to avoid accelerating theta
   decay in the final hour.

Risk management
---------------
- **Stop loss**: 30 % loss on option premium  (e.g. buy at ₹100 → SL at ₹70).
- **Target 1**: 50 % profit for normal confidence  (₹100 → ₹150).
- **Target 2**: 80 % profit for high confidence (₹100 → ₹180).
- **Quantity**: 1 lot for MVP (configurable).

Supported underlyings
---------------------
  NIFTY, BANKNIFTY, FINNIFTY (hardcoded set, configurable via YAML).
"""

from __future__ import annotations

import logging
from datetime import time
from typing import Any, Optional

from .base_strategy import (
    IStrategy,
    InstrumentType,
    SignalDirection,
    TradeSignal,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_CONFIDENCE_MAP: dict[int, float] = {
    5: 1.00,
    4: 0.85,
    3: 0.65,
    2: 0.45,
    1: 0.25,
    0: 0.00,
}

# Time filter: only enter before 14:00 IST
_ENTRY_CUTOFF_TIME = time(14, 0)

# Supported underlyings for options momentum
_DEFAULT_ALLOWED_UNDERLYINGS: set[str] = {"NIFTY", "BANKNIFTY", "FINNIFTY"}

# Stop and target percentages on option premium
_STOP_PCT: float = 0.30    # 30 % loss → exit
_TARGET_NORMAL_PCT: float = 0.50   # 50 % profit
_TARGET_HIGH_CONF_PCT: float = 0.80  # 80 % profit (confidence ≥ 0.85)


class OptionsMomentumStrategy(IStrategy):
    """
    Directional options-buying strategy for NIFTY / BANKNIFTY / FINNIFTY.

    Config keys (strategies.yaml → options_momentum)
    -------------------------------------------------
    underlying_move_pct   float   Min 15-min underlying move pct (default 0.5)
    pcr_call_threshold    float   PCR upper bound for call bias (default 0.8)
    pcr_put_threshold     float   PCR lower bound for put bias (default 1.2)
    iv_percentile_max     float   Max IV percentile to buy options (default 50)
    allowed_underlyings   list    Underlyings to trade (default NIFTY/BANKNIFTY/FINNIFTY)
    base_lots             int     Default number of lots (default 1)
    min_confidence        float   Minimum confidence gate (default 0.65)
    """

    strategy_name: str = "options_momentum"
    version: str = "1.0.0"
    is_enabled: bool = True
    supported_instruments: list[InstrumentType] = [
        InstrumentType.CE,
        InstrumentType.PE,
    ]
    min_confidence_threshold: float = 0.65

    _DEFAULT_UNDERLYING_MOVE_PCT: float = 0.5
    _DEFAULT_PCR_CALL_THRESHOLD: float = 0.8
    _DEFAULT_PCR_PUT_THRESHOLD: float = 1.2
    _DEFAULT_IV_PERCENTILE_MAX: float = 50.0
    _DEFAULT_BASE_LOTS: int = 1

    def __init__(self, config: Optional[dict[str, Any]] = None) -> None:
        super().__init__(config=config)

        cfg = self._config
        self.underlying_move_pct: float = float(
            cfg.get("underlying_move_pct", self._DEFAULT_UNDERLYING_MOVE_PCT)
        )
        self.pcr_call_threshold: float = float(
            cfg.get("pcr_call_threshold", self._DEFAULT_PCR_CALL_THRESHOLD)
        )
        self.pcr_put_threshold: float = float(
            cfg.get("pcr_put_threshold", self._DEFAULT_PCR_PUT_THRESHOLD)
        )
        self.iv_percentile_max: float = float(
            cfg.get("iv_percentile_max", self._DEFAULT_IV_PERCENTILE_MAX)
        )
        self.base_lots: int = int(
            cfg.get("base_lots", self._DEFAULT_BASE_LOTS)
        )
        self.allowed_underlyings: set[str] = set(
            cfg.get("allowed_underlyings", list(_DEFAULT_ALLOWED_UNDERLYINGS))
        )

    # ------------------------------------------------------------------
    # Config validation
    # ------------------------------------------------------------------

    def validate_config(self, config: dict[str, Any]) -> bool:
        """Validate options momentum config."""
        valid = True

        move_pct = config.get("underlying_move_pct", self._DEFAULT_UNDERLYING_MOVE_PCT)
        if not isinstance(move_pct, (int, float)) or move_pct <= 0:
            self._logger.error("underlying_move_pct must be > 0, got %s", move_pct)
            valid = False

        pcr_call = config.get("pcr_call_threshold", self._DEFAULT_PCR_CALL_THRESHOLD)
        pcr_put = config.get("pcr_put_threshold", self._DEFAULT_PCR_PUT_THRESHOLD)
        if not (0 < pcr_call < pcr_put):
            self._logger.error(
                "pcr thresholds must satisfy 0 < pcr_call < pcr_put, got %s / %s",
                pcr_call,
                pcr_put,
            )
            valid = False

        iv_max = config.get("iv_percentile_max", self._DEFAULT_IV_PERCENTILE_MAX)
        if not isinstance(iv_max, (int, float)) or not (0 < iv_max <= 100):
            self._logger.error(
                "iv_percentile_max must be in (0, 100], got %s", iv_max
            )
            valid = False

        lots = config.get("base_lots", self._DEFAULT_BASE_LOTS)
        if not isinstance(lots, int) or lots < 1:
            self._logger.error("base_lots must be an int >= 1, got %s", lots)
            valid = False

        return valid

    # ------------------------------------------------------------------
    # Strike selection helper
    # ------------------------------------------------------------------

    def _select_option_strike(
        self,
        underlying_price: float,
        option_chain: dict[str, Any],
        direction: SignalDirection,
        strike_step: float,
    ) -> Optional[tuple[float, str]]:
        """
        Select the ATM or 1-strike OTM option from the option chain.

        Parameters
        ----------
        underlying_price : float
            Current spot/futures price of the underlying.
        option_chain : dict
            Option chain snapshot.  Expected structure::

                {
                  "strikes": [
                    {
                      "strike": 24000.0,
                      "CE": {"ltp": 120.0, "oi": 50000, "iv": 18.5},
                      "PE": {"ltp": 80.0,  "oi": 30000, "iv": 19.0}
                    },
                    ...
                  ]
                }

        direction : SignalDirection
            BUY → we want CE; SELL-direction → we want PE.
        strike_step : float
            Strike interval (e.g. 50 for NIFTY, 100 for BANKNIFTY).

        Returns
        -------
        Optional[tuple[float, str]]
            (selected_strike, option_symbol) or None if chain is unavailable.
        """
        strikes: list[dict] = option_chain.get("strikes", [])
        if not strikes:
            self._logger.warning("Empty option chain; cannot select strike.")
            return None

        opt_type = "CE" if direction == SignalDirection.BUY else "PE"

        # Find ATM: nearest strike to underlying price
        atm_strike: float = round(underlying_price / strike_step) * strike_step

        # OTM is 1 strike further out of the money
        # CE OTM = ATM + step; PE OTM = ATM - step
        otm_strike: float = (
            atm_strike + strike_step
            if direction == SignalDirection.BUY
            else atm_strike - strike_step
        )

        # Prefer ATM if it exists in chain; fall back to OTM
        available_strikes = {s["strike"] for s in strikes}

        selected_strike: float
        if atm_strike in available_strikes:
            selected_strike = atm_strike
        elif otm_strike in available_strikes:
            selected_strike = otm_strike
        else:
            # Pick the closest available strike
            selected_strike = min(
                available_strikes, key=lambda s: abs(s - underlying_price)
            )
            self._logger.warning(
                "ATM (%s) and OTM (%s) strikes not in chain. Picked closest: %s.",
                atm_strike,
                otm_strike,
                selected_strike,
            )

        # Build the option symbol string (simplified – actual format varies by broker)
        expiry_str = option_chain.get("expiry", "UNKNOWN")
        option_symbol = (
            f"{option_chain.get('underlying', 'IDX')}"
            f"{expiry_str}"
            f"{int(selected_strike)}"
            f"{opt_type}"
        )

        self._logger.debug(
            "Selected strike: %s (%s) for underlying @ %s. ATM was %s.",
            selected_strike,
            opt_type,
            underlying_price,
            atm_strike,
        )
        return selected_strike, option_symbol

    # ------------------------------------------------------------------
    # Signal generation
    # ------------------------------------------------------------------

    async def generate_signal(
        self, context: dict[str, Any]
    ) -> Optional[TradeSignal]:
        """
        Evaluate options-momentum conditions and return a TradeSignal or None.

        Required context keys
        ---------------------
        ``underlying``              str          – underlying name (e.g. "NIFTY")
        ``underlying_price``        float        – current underlying spot/futures price
        ``underlying_15m_change_pct`` float      – % change in last 15 minutes
        ``oi_direction``            str          – "CALL_BUILDING" | "PUT_BUILDING" | "NEUTRAL"
        ``pcr``                     float        – current put-call ratio
        ``iv_percentile``           float        – IV percentile [0–100]
        ``option_chain``            dict         – option chain snapshot
        ``strike_step``             float        – strike interval for this underlying
        ``ltp``                     float        – LTP of the option (after strike selection)
        ``timestamp``               datetime     – current IST datetime
        ``instrument_type``         InstrumentType
        ``expiry``                  str          – option expiry string
        """
        if not self._check_enabled():
            return None

        # ----------------------------------------------------------------
        # Extract and validate context
        # ----------------------------------------------------------------
        try:
            underlying: str = context["underlying"]
            underlying_price: float = float(context["underlying_price"])
            change_15m_pct: float = float(context["underlying_15m_change_pct"])
            oi_direction: str = context.get("oi_direction", "NEUTRAL")
            pcr: float = float(context["pcr"])
            iv_percentile: float = float(context["iv_percentile"])
            option_chain: dict = context.get("option_chain", {})
            strike_step: float = float(context.get("strike_step", 50.0))
            timestamp = context.get("timestamp")
            expiry: Optional[str] = context.get("expiry")
        except KeyError as exc:
            self._logger.error(
                "Missing required context key for %s: %s", self.strategy_name, exc
            )
            return None

        # --- Underlying filter ---
        if underlying.upper() not in self.allowed_underlyings:
            self._logger.info(
                "Underlying '%s' not in allowed set %s; skipping.",
                underlying,
                self.allowed_underlyings,
            )
            return None

        # --- Time filter: before 14:00 IST ---
        if timestamp is not None:
            bar_time = timestamp.time() if hasattr(timestamp, "time") else None
            if bar_time and bar_time >= _ENTRY_CUTOFF_TIME:
                self._logger.info(
                    "Past entry cutoff %s; no new options positions. Current: %s.",
                    _ENTRY_CUTOFF_TIME,
                    bar_time,
                )
                return None

        # ----------------------------------------------------------------
        # Determine direction from 15-min underlying move
        # ----------------------------------------------------------------
        is_call_setup = change_15m_pct >= self.underlying_move_pct
        is_put_setup = change_15m_pct <= -self.underlying_move_pct

        if not is_call_setup and not is_put_setup:
            self._logger.debug(
                "15-min move %.2f%% below threshold ±%.2f%%; no signal.",
                change_15m_pct,
                self.underlying_move_pct,
            )
            return None

        direction: SignalDirection = (
            SignalDirection.BUY if is_call_setup else SignalDirection.SELL
        )
        instrument_type: InstrumentType = (
            InstrumentType.CE if is_call_setup else InstrumentType.PE
        )

        if not self._check_instrument(instrument_type):
            return None

        # ----------------------------------------------------------------
        # Condition checks (5 conditions after directional gate)
        # ----------------------------------------------------------------
        conditions_met: int = 0
        rationale: list[str] = []

        # Condition 1 – Underlying move (gate condition, already passed)
        conditions_met += 1
        move_dir = "up" if is_call_setup else "down"
        rationale.append(
            f"✅ Underlying {underlying} moved {change_15m_pct:+.2f}% in last 15 min "
            f"(threshold ±{self.underlying_move_pct}%) – strong {move_dir}side momentum."
        )

        # Condition 2 – OI buildup in direction
        expected_oi = "CALL_BUILDING" if is_call_setup else "PUT_BUILDING"
        cond2 = oi_direction == expected_oi
        if cond2:
            conditions_met += 1
            rationale.append(
                f"✅ OI direction is '{oi_direction}' – confirms "
                f"{'call' if is_call_setup else 'put'} buying pressure."
            )
        else:
            rationale.append(
                f"❌ OI direction is '{oi_direction}' (expected '{expected_oi}') – "
                "OI not confirming directional bias."
            )

        # Condition 3 – PCR confirms bias
        if is_call_setup:
            cond3 = pcr <= self.pcr_call_threshold
            threshold_str = f"< {self.pcr_call_threshold}"
            pcr_desc = "bullish (call bias)"
        else:
            cond3 = pcr >= self.pcr_put_threshold
            threshold_str = f"> {self.pcr_put_threshold}"
            pcr_desc = "bearish (put bias)"

        if cond3:
            conditions_met += 1
            rationale.append(
                f"✅ PCR ({pcr:.2f}) {threshold_str} – {pcr_desc} confirmed."
            )
        else:
            rationale.append(
                f"❌ PCR ({pcr:.2f}) does not confirm {pcr_desc} (need {threshold_str})."
            )

        # Condition 4 – IV percentile below max (cheap options)
        cond4 = iv_percentile <= self.iv_percentile_max
        if cond4:
            conditions_met += 1
            rationale.append(
                f"✅ IV percentile ({iv_percentile:.1f}%) ≤ {self.iv_percentile_max}% – "
                "options are relatively cheap; good to buy."
            )
        else:
            rationale.append(
                f"❌ IV percentile ({iv_percentile:.1f}%) > {self.iv_percentile_max}% – "
                "options are expensive; unfavourable to buy."
            )

        # Condition 5 – Strike selection from option chain
        strike_result = self._select_option_strike(
            underlying_price, option_chain, direction, strike_step
        )
        cond5 = strike_result is not None
        if cond5:
            conditions_met += 1
            selected_strike, option_symbol = strike_result
            atm_strike = round(underlying_price / strike_step) * strike_step
            strike_label = (
                "ATM" if selected_strike == atm_strike else "1-strike OTM"
            )
            rationale.append(
                f"✅ Strike selected: {selected_strike} ({strike_label}) → "
                f"option symbol: {option_symbol}."
            )
        else:
            # Use placeholder if chain not available
            selected_strike = round(underlying_price / strike_step) * strike_step
            opt_suffix = "CE" if is_call_setup else "PE"
            option_symbol = f"{underlying}{expiry or 'EXP'}{int(selected_strike)}{opt_suffix}"
            rationale.append(
                "❌ Option chain unavailable; used estimated ATM strike "
                f"({selected_strike}). Verify before placing order."
            )

        # ----------------------------------------------------------------
        # Confidence scoring
        # ----------------------------------------------------------------
        confidence: float = _CONFIDENCE_MAP.get(conditions_met, 0.0)

        self._logger.info(
            "%s | %s %s | conditions=%d/5 | confidence=%.2f",
            self.strategy_name,
            underlying,
            instrument_type.value,
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
        # Risk management on option premium
        # ----------------------------------------------------------------
        # We need the option LTP to compute SL / targets
        option_ltp: float = float(context.get("ltp", 0.0))
        if option_ltp <= 0:
            # Try to read from option chain
            strikes_list: list[dict] = option_chain.get("strikes", [])
            for strike_entry in strikes_list:
                if strike_entry.get("strike") == selected_strike:
                    opt_key = "CE" if is_call_setup else "PE"
                    option_ltp = float(
                        strike_entry.get(opt_key, {}).get("ltp", 0.0)
                    )
                    break

        if option_ltp <= 0:
            self._logger.warning(
                "Could not determine option LTP for %s; skipping signal.",
                option_symbol,
            )
            return None

        stop_loss: float = round(option_ltp * (1 - _STOP_PCT), 2)

        # Higher target for high-confidence signals
        target_pct = (
            _TARGET_HIGH_CONF_PCT if confidence >= 0.85 else _TARGET_NORMAL_PCT
        )
        target_1: float = round(option_ltp * (1 + target_pct), 2)

        rationale.append(
            f"📊 Options risk mgmt: Buy premium={option_ltp:.2f}, "
            f"SL={stop_loss:.2f} (−{_STOP_PCT*100:.0f}%), "
            f"Target={target_1:.2f} (+{target_pct*100:.0f}%). "
            f"Lots={self.base_lots}."
        )
        rationale.append(
            f"⏰ Only entered before {_ENTRY_CUTOFF_TIME.strftime('%H:%M')} IST "
            "to limit theta decay impact."
        )

        # ----------------------------------------------------------------
        # Build and return signal
        # ----------------------------------------------------------------
        signal = TradeSignal(
            strategy_name=self.strategy_name,
            instrument_type=instrument_type,
            symbol=option_symbol,
            underlying=underlying,
            direction=SignalDirection.BUY,  # options buying → always BUY the option
            entry_price=option_ltp,
            stop_loss=stop_loss,
            targets=[target_1],
            quantity=self.base_lots,
            confidence=confidence,
            rationale=rationale,
            expiry=expiry,
            strike=selected_strike,
            metadata={
                "underlying_price": underlying_price,
                "change_15m_pct": change_15m_pct,
                "oi_direction": oi_direction,
                "pcr": pcr,
                "iv_percentile": iv_percentile,
                "strike_step": strike_step,
                "selected_strike": selected_strike,
                "atm_strike": round(underlying_price / strike_step) * strike_step,
                "stop_pct": _STOP_PCT,
                "target_pct": target_pct,
                "conditions_met": conditions_met,
            },
        )

        self._logger.info("Signal emitted: %s", signal)
        return signal
