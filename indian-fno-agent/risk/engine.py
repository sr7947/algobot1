"""
risk/engine.py
--------------
Core risk management engine for the Indian F&O trading agent.

Performs hard-gated pre-trade validation checks and post-trade state updates.
All checks are sequential; the first critical failure short-circuits further
evaluation unless the check is advisory (WARN level).

Author: F&O Trading Agent
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from typing import TYPE_CHECKING

import pytz

from models.market import MarginInfo, Position
from models.risk import RiskState
from models.signals import TradeSignal
from config.settings import Settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Indian Standard Time zone
IST = pytz.timezone("Asia/Kolkata")

# Market session boundaries (IST)
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


class RiskEngine:
    """
    Hard-gated pre-trade risk validator and post-trade state tracker.

    All checks return a (passed: bool, reasons: list[str]) tuple.
    Checks are run in priority order; kill-switch and time checks
    are evaluated first before margin-intensive lookups.

    Parameters
    ----------
    settings : Settings
        Application-wide configuration (limits, thresholds, flags).
    risk_state : RiskState
        Mutable in-memory risk state (P&L, consecutive losses, etc.).
    """

    def __init__(self, settings: Settings, risk_state: RiskState) -> None:
        self.settings = settings
        self.risk_state = risk_state
        logger.info(
            "RiskEngine initialised | max_daily_loss_pct=%.2f%% capital=%.2f "
            "max_positions=%d max_consecutive_losses=%d",
            settings.MAX_DAILY_LOSS_PCT,
            settings.CAPITAL,
            settings.MAX_OPEN_POSITIONS,
            settings.MAX_CONSECUTIVE_LOSSES,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def validate_signal(
        self,
        signal: TradeSignal,
        positions: list[Position],
        margins: MarginInfo,
    ) -> tuple[bool, list[str]]:
        """
        Run all pre-trade risk checks against the incoming signal.

        Checks are ordered by severity / cost-of-evaluation:
          1. Kill switch
          2. Market hours
          3. Daily loss limit
          4. Max open positions
          5. Consecutive losses
          6. Duplicate symbol (configurable WARN vs FAIL)
          7. Signal expiry
          8. Minimum confidence
          9. Liquidity (bid-ask spread) for options
         10. Margin adequacy

        Parameters
        ----------
        signal : TradeSignal
            The proposed trade signal to validate.
        positions : list[Position]
            Currently open positions held by the account.
        margins : MarginInfo
            Current margin snapshot from the broker.

        Returns
        -------
        tuple[bool, list[str]]
            (True, []) if all checks pass; (False, [reasons]) on any failure.
            Advisory warnings are included in the reasons list but do NOT
            flip the boolean to False unless configured to do so.
        """
        failures: list[str] = []
        warnings: list[str] = []
        now_ist = datetime.now(IST)

        # ── 1. Kill switch ────────────────────────────────────────────
        if self.risk_state.kill_switch_active:
            reason = "KILL_SWITCH: Emergency kill switch is currently active."
            logger.warning("Signal rejected – %s", reason)
            return False, [reason]

        # ── 2. Market hours ───────────────────────────────────────────
        current_time = now_ist.time().replace(tzinfo=None)
        if not (MARKET_OPEN <= current_time <= MARKET_CLOSE):
            reason = (
                f"MARKET_HOURS: Current IST time {current_time.strftime('%H:%M')} "
                f"is outside trading window {MARKET_OPEN.strftime('%H:%M')}–"
                f"{MARKET_CLOSE.strftime('%H:%M')}."
            )
            failures.append(reason)
            logger.warning("Signal rejected – %s", reason)
            return False, failures

        # ── 3. Daily loss limit ───────────────────────────────────────
        max_loss_amount = -(self.settings.MAX_DAILY_LOSS_PCT / 100.0) * self.settings.CAPITAL
        if self.risk_state.daily_pnl < max_loss_amount:
            reason = (
                f"DAILY_LOSS: Daily P&L Rs.{self.risk_state.daily_pnl:,.2f} has breached "
                f"the limit of Rs.{max_loss_amount:,.2f} "
                f"({self.settings.MAX_DAILY_LOSS_PCT}% of Rs.{self.settings.CAPITAL:,.2f} capital)."
            )
            failures.append(reason)
            logger.warning("Signal rejected – %s", reason)
            return False, failures

        # ── 4. Max open positions ─────────────────────────────────────
        if len(positions) >= self.settings.MAX_OPEN_POSITIONS:
            reason = (
                f"MAX_POSITIONS: {len(positions)} open positions already at "
                f"configured limit of {self.settings.MAX_OPEN_POSITIONS}."
            )
            failures.append(reason)
            logger.warning("Signal rejected – %s", reason)
            return False, failures

        # ── 5. Consecutive losses ─────────────────────────────────────
        if self.risk_state.consecutive_losses >= self.settings.MAX_CONSECUTIVE_LOSSES:
            reason = (
                f"CONSECUTIVE_LOSSES: {self.risk_state.consecutive_losses} consecutive "
                f"losses reached limit of {self.settings.MAX_CONSECUTIVE_LOSSES}. "
                "Manual review required before resuming."
            )
            failures.append(reason)
            logger.warning("Signal rejected – %s", reason)
            return False, failures

        # ── 6. Duplicate symbol ───────────────────────────────────────
        open_symbols = {p.symbol for p in positions}
        if signal.symbol in open_symbols:
            msg = (
                f"DUPLICATE_SYMBOL: An open position already exists for "
                f"{signal.symbol}."
            )
            if self.settings.FAIL_ON_DUPLICATE_SYMBOL:
                failures.append(msg)
                logger.warning("Signal rejected – %s", msg)
                return False, failures
            else:
                warnings.append(f"WARN – {msg}")
                logger.warning(msg)

        # ── 7. Signal expiry ──────────────────────────────────────────
        if signal.expires_at and now_ist > signal.expires_at:
            reason = (
                f"SIGNAL_EXPIRED: Signal for {signal.symbol} expired at "
                f"{signal.expires_at.strftime('%Y-%m-%d %H:%M:%S %Z')}; "
                f"current time is {now_ist.strftime('%H:%M:%S %Z')}."
            )
            failures.append(reason)
            logger.warning("Signal rejected – %s", reason)
            return False, failures + warnings

        # ── 8. Minimum confidence threshold ──────────────────────────
        min_confidence = self.settings.MIN_SIGNAL_CONFIDENCE  # e.g. 0.60
        if signal.confidence < min_confidence:
            reason = (
                f"LOW_CONFIDENCE: Signal confidence {signal.confidence:.2%} is below "
                f"minimum threshold of {min_confidence:.2%}."
            )
            failures.append(reason)
            logger.warning("Signal rejected – %s", reason)
            return False, failures + warnings

        # ── 9. Liquidity – bid-ask spread (options only) ──────────────
        if signal.instrument_type.upper() in {"CE", "PE", "CALL", "PUT"}:
            spread_check_passed, spread_reason = self._check_bid_ask_spread(signal)
            if not spread_check_passed:
                failures.append(spread_reason)
                logger.warning("Signal rejected – %s", spread_reason)
                return False, failures + warnings

        # ── 10. Margin adequacy ───────────────────────────────────────
        required_margin = self.calculate_required_margin(signal)
        if margins.available_margin <= required_margin:
            reason = (
                f"INSUFFICIENT_MARGIN: Required Rs.{required_margin:,.2f} but only "
                f"Rs.{margins.available_margin:,.2f} available "
                f"(shortfall Rs.{required_margin - margins.available_margin:,.2f})."
            )
            failures.append(reason)
            logger.warning("Signal rejected – %s", reason)
            return False, failures + warnings

        # ── All checks passed ─────────────────────────────────────────
        if warnings:
            logger.info(
                "Signal for %s passed with %d warning(s): %s",
                signal.symbol,
                len(warnings),
                "; ".join(warnings),
            )
        else:
            logger.info("Signal for %s passed all risk checks.", signal.symbol)

        return True, warnings  # Return warnings as informational reasons

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    def calculate_required_margin(self, signal: TradeSignal) -> float:
        """
        Estimate the margin required to execute a signal.

        Uses the simplified SPAN-like approximation:
            required_margin = lot_size x entry_price x MARGIN_FRACTION x quantity

        The default MARGIN_FRACTION is 0.20 (20% of notional value),
        matching typical NSE F&O initial margin requirements.

        Parameters
        ----------
        signal : TradeSignal
            The signal whose margin requirement is to be estimated.

        Returns
        -------
        float
            Estimated margin in INR.
        """
        margin_fraction = self.settings.MARGIN_FRACTION  # default 0.20
        required = signal.lot_size * signal.entry_price * margin_fraction * signal.quantity
        logger.debug(
            "Margin estimate for %s: lots=%d entry=%.2f fraction=%.2f -> Rs.%.2f",
            signal.symbol,
            signal.quantity,
            signal.entry_price,
            margin_fraction,
            required,
        )
        return required

    def _check_bid_ask_spread(self, signal: TradeSignal) -> tuple[bool, str]:
        """
        Check that the option bid-ask spread does not exceed the threshold.

        The spread percentage is computed as:
            spread_pct = (ask - bid) / mid_price * 100

        Parameters
        ----------
        signal : TradeSignal
            Must have bid_price and ask_price populated for options.

        Returns
        -------
        tuple[bool, str]
            (True, '') if spread is acceptable; (False, reason) otherwise.
        """
        max_spread_pct = self.settings.MAX_BID_ASK_SPREAD_PCT  # e.g. 2.0

        bid = getattr(signal, "bid_price", None)
        ask = getattr(signal, "ask_price", None)

        if bid is None or ask is None or bid <= 0 or ask <= 0:
            # Cannot verify – log warning but do not block
            logger.warning(
                "Bid/ask data unavailable for %s; skipping spread check.", signal.symbol
            )
            return True, ""

        mid_price = (bid + ask) / 2.0
        if mid_price == 0:
            return True, ""

        spread_pct = ((ask - bid) / mid_price) * 100.0

        if spread_pct > max_spread_pct:
            reason = (
                f"LIQUIDITY: Bid-ask spread {spread_pct:.2f}% on {signal.symbol} "
                f"exceeds max allowed {max_spread_pct:.2f}%. "
                f"(bid={bid:.2f}, ask={ask:.2f})"
            )
            return False, reason

        logger.debug(
            "Spread check OK for %s: %.2f%% (max %.2f%%)",
            signal.symbol,
            spread_pct,
            max_spread_pct,
        )
        return True, ""

    # ------------------------------------------------------------------
    # Post-trade updates
    # ------------------------------------------------------------------

    async def post_trade_update(self, pnl: float, win: bool) -> None:
        """
        Update the shared RiskState after a trade closes.

        This should be called once for every completed trade (both wins and
        losses) so that daily P&L and consecutive-loss counters remain accurate.

        Parameters
        ----------
        pnl : float
            Realised P&L of the completed trade in INR (negative = loss).
        win : bool
            True if the trade was profitable, False otherwise.
        """
        self.risk_state.daily_pnl += pnl
        self.risk_state.total_trades += 1

        if win:
            self.risk_state.winning_trades += 1
            self.risk_state.consecutive_losses = 0  # Reset streak on win
            logger.info(
                "Trade WIN | P&L Rs.%.2f | Daily P&L Rs.%.2f | Consecutive losses reset to 0",
                pnl,
                self.risk_state.daily_pnl,
            )
        else:
            self.risk_state.consecutive_losses += 1
            self.risk_state.max_consecutive_losses_seen = max(
                self.risk_state.max_consecutive_losses_seen,
                self.risk_state.consecutive_losses,
            )
            logger.warning(
                "Trade LOSS | P&L Rs.%.2f | Daily P&L Rs.%.2f | Consecutive losses -> %d",
                pnl,
                self.risk_state.daily_pnl,
                self.risk_state.consecutive_losses,
            )

        # Auto-activate kill switch if daily loss threshold is breached
        max_loss = -(self.settings.MAX_DAILY_LOSS_PCT / 100.0) * self.settings.CAPITAL
        if self.risk_state.daily_pnl < max_loss and not self.risk_state.kill_switch_active:
            logger.critical(
                "Daily loss limit breached (Rs.%.2f < Rs.%.2f). Activating kill switch.",
                self.risk_state.daily_pnl,
                max_loss,
            )
            self.risk_state.kill_switch_active = True

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def get_risk_summary(self) -> dict:
        """
        Return a snapshot of all current risk metrics.

        Returns
        -------
        dict
            Dictionary containing key risk indicators for dashboards,
            logging, and Telegram alerts.
        """
        capital = self.settings.CAPITAL
        max_loss_pct = self.settings.MAX_DAILY_LOSS_PCT
        max_loss_amount = -(max_loss_pct / 100.0) * capital

        win_rate = (
            self.risk_state.winning_trades / self.risk_state.total_trades
            if self.risk_state.total_trades > 0
            else 0.0
        )

        daily_pnl_pct = (self.risk_state.daily_pnl / capital) * 100.0 if capital else 0.0

        return {
            # Identity
            "timestamp_ist": datetime.now(IST).isoformat(),
            # Kill switch
            "kill_switch_active": self.risk_state.kill_switch_active,
            # P&L
            "daily_pnl_inr": round(self.risk_state.daily_pnl, 2),
            "daily_pnl_pct": round(daily_pnl_pct, 4),
            "daily_loss_limit_inr": round(max_loss_amount, 2),
            "daily_loss_limit_pct": max_loss_pct,
            "capital_inr": capital,
            "pnl_headroom_inr": round(self.risk_state.daily_pnl - max_loss_amount, 2),
            # Loss streaks
            "consecutive_losses": self.risk_state.consecutive_losses,
            "max_consecutive_losses_allowed": self.settings.MAX_CONSECUTIVE_LOSSES,
            "max_consecutive_losses_seen_today": self.risk_state.max_consecutive_losses_seen,
            # Trade stats
            "total_trades_today": self.risk_state.total_trades,
            "winning_trades_today": self.risk_state.winning_trades,
            "win_rate_today": round(win_rate, 4),
            # Limits
            "max_open_positions": self.settings.MAX_OPEN_POSITIONS,
            "min_signal_confidence": self.settings.MIN_SIGNAL_CONFIDENCE,
            "max_bid_ask_spread_pct": self.settings.MAX_BID_ASK_SPREAD_PCT,
            "margin_fraction": self.settings.MARGIN_FRACTION,
        }
