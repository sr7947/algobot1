"""
risk/position_sizing.py
-----------------------
Position sizing algorithms for the Indian F&O trading agent.

Provides three independent sizing methods:
  1. Fixed-fraction (risk-based) sizing
  2. Kelly Criterion sizing
  3. Margin-constrained maximum sizing

The public ``recommended_lots`` method aggregates all three and returns
the most conservative (minimum) result, clamped to configured bounds.

Author: F&O Trading Agent
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from models.market import MarginInfo
from models.risk import RiskState
from models.signals import TradeSignal
from config.settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class SizingResult:
    """Detailed breakdown of every sizing method for audit / logging."""

    fixed_fraction_lots: int
    kelly_lots: int
    margin_max_lots: int
    recommended_lots: int
    method_chosen: str
    details: dict


class PositionSizer:
    """
    Multi-method position sizer that always recommends the most conservative
    lot count across all applicable sizing strategies.

    Parameters
    ----------
    settings : Settings
        Application settings providing capital, lot limits, margin fraction.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        logger.info(
            "PositionSizer initialised | min_lots=%d max_lots=%d",
            settings.MIN_LOTS,
            settings.MAX_LOTS,
        )

    # ------------------------------------------------------------------
    # Individual sizing strategies
    # ------------------------------------------------------------------

    def fixed_fraction_size(
        self,
        capital: float,
        risk_pct: float,
        entry: float,
        stop_loss: float,
        lot_size: int,
    ) -> int:
        """
        Fixed-fraction (risk-based) position sizing.

        Sizes the position so that the maximum loss on a single trade
        does not exceed ``risk_pct`` percent of trading capital.

        Formula::

            risk_amount = capital * (risk_pct / 100)
            risk_per_lot = abs(entry - stop_loss) * lot_size
            lots = floor(risk_amount / risk_per_lot)

        Parameters
        ----------
        capital : float
            Total trading capital in INR.
        risk_pct : float
            Maximum acceptable loss as a percentage of capital (e.g. 1.0 = 1%).
        entry : float
            Planned entry price per unit.
        stop_loss : float
            Planned stop-loss price per unit.
        lot_size : int
            Number of units per lot (NSE contract specification).

        Returns
        -------
        int
            Number of lots (>= 0). Returns 0 if the trade risk is undefined.
        """
        if capital <= 0 or risk_pct <= 0:
            logger.warning("fixed_fraction_size: invalid capital=%.2f or risk_pct=%.2f", capital, risk_pct)
            return 0

        price_risk = abs(entry - stop_loss)
        if price_risk == 0 or lot_size == 0:
            logger.warning(
                "fixed_fraction_size: zero price_risk (entry=%.2f stop=%.2f) or lot_size=%d",
                entry,
                stop_loss,
                lot_size,
            )
            return 0

        risk_amount = capital * (risk_pct / 100.0)
        risk_per_lot = price_risk * lot_size
        lots = math.floor(risk_amount / risk_per_lot)

        logger.debug(
            "fixed_fraction | capital=%.2f risk_pct=%.2f%% entry=%.2f stop=%.2f "
            "lot_size=%d -> risk_amount=%.2f risk_per_lot=%.2f lots=%d",
            capital,
            risk_pct,
            entry,
            stop_loss,
            lot_size,
            risk_amount,
            risk_per_lot,
            lots,
        )
        return max(0, lots)

    def kelly_size(
        self,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
        capital: float,
        lot_size: int,
        entry: float,
        kelly_fraction: float = 0.25,
    ) -> int:
        """
        Kelly Criterion position sizing (fractional Kelly).

        Full Kelly formula::

            b   = avg_win / avg_loss       (win/loss ratio)
            K   = (b * p - q) / b          (p = win_rate, q = 1 - p)
            K_f = K * kelly_fraction       (fractional Kelly for safety)

        The fractional Kelly (default 25%) is applied to avoid the
        volatility of full-Kelly sizing which can cause large drawdowns.

        Parameters
        ----------
        win_rate : float
            Historical win rate as a fraction in [0, 1].
        avg_win : float
            Average profit per winning trade in INR (positive).
        avg_loss : float
            Average loss per losing trade in INR (positive magnitude).
        capital : float
            Total trading capital in INR.
        lot_size : int
            Number of units per lot.
        entry : float
            Entry price per unit (used to convert capital fraction to lots).
        kelly_fraction : float
            Fraction of full Kelly to use (default 0.25 = quarter-Kelly).

        Returns
        -------
        int
            Number of lots (>= 0). Returns 0 if Kelly fraction is negative
            (edge-negative strategy – should not trade).
        """
        if win_rate <= 0 or win_rate >= 1:
            logger.warning("kelly_size: win_rate=%.4f out of valid range (0, 1)", win_rate)
            return 0
        if avg_loss <= 0:
            logger.warning("kelly_size: avg_loss must be positive, got %.2f", avg_loss)
            return 0
        if entry <= 0 or lot_size <= 0:
            return 0

        p = win_rate
        q = 1.0 - p
        b = avg_win / avg_loss  # odds ratio

        # Full Kelly fraction of capital to risk
        full_kelly = (b * p - q) / b

        if full_kelly <= 0:
            logger.warning(
                "kelly_size: negative Kelly fraction (%.4f) – strategy has no edge; "
                "win_rate=%.2f avg_win=%.2f avg_loss=%.2f",
                full_kelly,
                win_rate,
                avg_win,
                avg_loss,
            )
            return 0

        fractional_kelly = full_kelly * kelly_fraction
        capital_to_risk = capital * fractional_kelly
        notional_per_lot = entry * lot_size

        if notional_per_lot == 0:
            return 0

        lots = math.floor(capital_to_risk / notional_per_lot)

        logger.debug(
            "kelly | win_rate=%.2f avg_win=%.2f avg_loss=%.2f b=%.4f "
            "full_kelly=%.4f frac_kelly=%.4f capital_to_risk=%.2f lots=%d",
            win_rate,
            avg_win,
            avg_loss,
            b,
            full_kelly,
            fractional_kelly,
            capital_to_risk,
            lots,
        )
        return max(0, lots)

    def max_lots_by_margin(
        self,
        available_margin: float,
        price: float,
        lot_size: int,
        margin_pct: float = 0.20,
    ) -> int:
        """
        Maximum affordable lots given available margin.

        Uses the broker's standard margin requirement:
            margin_per_lot = price * lot_size * margin_pct

        Parameters
        ----------
        available_margin : float
            Funds available for margin in INR.
        price : float
            Current market price per unit.
        lot_size : int
            Number of units per lot.
        margin_pct : float
            Margin fraction required (default 0.20 = 20% of notional).

        Returns
        -------
        int
            Maximum number of lots affordable given the margin.
        """
        if price <= 0 or lot_size <= 0 or margin_pct <= 0:
            logger.warning(
                "max_lots_by_margin: invalid inputs price=%.2f lot_size=%d margin_pct=%.2f",
                price,
                lot_size,
                margin_pct,
            )
            return 0

        margin_per_lot = price * lot_size * margin_pct
        lots = math.floor(available_margin / margin_per_lot)

        logger.debug(
            "margin_max | available=%.2f price=%.2f lot_size=%d margin_pct=%.2f "
            "margin_per_lot=%.2f -> lots=%d",
            available_margin,
            price,
            lot_size,
            margin_pct,
            margin_per_lot,
            lots,
        )
        return max(0, lots)

    # ------------------------------------------------------------------
    # Aggregated recommendation
    # ------------------------------------------------------------------

    def recommended_lots(
        self,
        signal: TradeSignal,
        capital: float,
        risk_state: RiskState,
        margins: MarginInfo,
    ) -> SizingResult:
        """
        Compute the recommended lot count using all three sizing methods
        and return the most conservative (minimum) result.

        The final recommendation is additionally clamped to:
            [settings.MIN_LOTS, settings.MAX_LOTS]

        Parameters
        ----------
        signal : TradeSignal
            The trade signal containing entry, stop-loss, lot size, etc.
        capital : float
            Current total capital in INR.
        risk_state : RiskState
            Runtime risk state containing historical win rate stats.
        margins : MarginInfo
            Current margin snapshot from the broker.

        Returns
        -------
        SizingResult
            A rich result object with individual method outputs and the final
            recommended lot count.
        """
        # ── Fixed-fraction sizing ─────────────────────────────────────
        ff_lots = self.fixed_fraction_size(
            capital=capital,
            risk_pct=self.settings.RISK_PCT_PER_TRADE,
            entry=signal.entry_price,
            stop_loss=signal.stop_loss,
            lot_size=signal.lot_size,
        )

        # ── Kelly sizing ──────────────────────────────────────────────
        # Derive historical stats from risk_state; fall back to defaults
        win_rate = (
            risk_state.winning_trades / risk_state.total_trades
            if risk_state.total_trades >= self.settings.KELLY_MIN_TRADES
            else self.settings.KELLY_DEFAULT_WIN_RATE
        )
        avg_win = risk_state.avg_win_inr or self.settings.KELLY_DEFAULT_AVG_WIN
        avg_loss = risk_state.avg_loss_inr or self.settings.KELLY_DEFAULT_AVG_LOSS

        kelly_lots = self.kelly_size(
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            capital=capital,
            lot_size=signal.lot_size,
            entry=signal.entry_price,
            kelly_fraction=self.settings.KELLY_FRACTION,
        )

        # ── Margin-constrained sizing ─────────────────────────────────
        margin_lots = self.max_lots_by_margin(
            available_margin=margins.available_margin,
            price=signal.entry_price,
            lot_size=signal.lot_size,
            margin_pct=self.settings.MARGIN_FRACTION,
        )

        # ── Conservative minimum ──────────────────────────────────────
        candidates = {
            "fixed_fraction": ff_lots,
            "kelly": kelly_lots,
            "margin_max": margin_lots,
        }
        # Filter out zeros from methods that couldn't compute (avoid clamping to 0 unfairly)
        non_zero_candidates = {k: v for k, v in candidates.items() if v > 0}

        if non_zero_candidates:
            method_chosen = min(non_zero_candidates, key=lambda k: non_zero_candidates[k])
            raw_lots = non_zero_candidates[method_chosen]
        else:
            method_chosen = "default_minimum"
            raw_lots = 0

        # Apply global min/max bounds
        final_lots = max(self.settings.MIN_LOTS, min(self.settings.MAX_LOTS, raw_lots))

        logger.info(
            "PositionSizer | symbol=%s ff=%d kelly=%d margin=%d -> raw=%d final=%d (method=%s)",
            signal.symbol,
            ff_lots,
            kelly_lots,
            margin_lots,
            raw_lots,
            final_lots,
            method_chosen,
        )

        return SizingResult(
            fixed_fraction_lots=ff_lots,
            kelly_lots=kelly_lots,
            margin_max_lots=margin_lots,
            recommended_lots=final_lots,
            method_chosen=method_chosen,
            details={
                "capital": capital,
                "risk_pct_per_trade": self.settings.RISK_PCT_PER_TRADE,
                "entry_price": signal.entry_price,
                "stop_loss": signal.stop_loss,
                "lot_size": signal.lot_size,
                "win_rate_used": win_rate,
                "avg_win_used": avg_win,
                "avg_loss_used": avg_loss,
                "available_margin": margins.available_margin,
                "margin_fraction": self.settings.MARGIN_FRACTION,
                "kelly_fraction": self.settings.KELLY_FRACTION,
                "min_lots": self.settings.MIN_LOTS,
                "max_lots": self.settings.MAX_LOTS,
            },
        )
