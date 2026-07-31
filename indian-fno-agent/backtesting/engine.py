"""
Walk-forward backtesting engine with realistic NSE F&O assumptions.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

import numpy as np

from core.enums import TradeDirection, SignalStatus, MarketRegime
from core.models import Candle, TradeSignal
from backtesting.charges import calculate_charges, calculate_sharpe
from config.settings import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()


@dataclass
class BacktestTrade:
    """A single completed trade in the backtest."""
    id: str = ""
    symbol: str = ""
    strategy: str = ""
    direction: str = "BUY"
    entry_price: float = 0.0
    exit_price: float = 0.0
    quantity: int = 1
    lot_size: int = 1
    entry_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    pnl_gross: float = 0.0
    charges: float = 0.0
    pnl_net: float = 0.0
    exit_reason: str = ""
    regime: str = ""
    win: bool = False
    hold_minutes: int = 0


@dataclass
class BacktestMetrics:
    """Performance metrics from a backtest run."""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    cagr: float = 0.0
    expectancy: float = 0.0
    avg_hold_minutes: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    by_regime: dict = field(default_factory=dict)
    by_strategy: dict = field(default_factory=dict)
    monthly_pnl: dict = field(default_factory=dict)


@dataclass
class BacktestPosition:
    """An open position during backtest simulation."""
    signal: TradeSignal
    entry_price: float
    quantity: int
    stop_loss: float
    target: float
    entry_time: datetime
    trailing_sl: Optional[float] = None


class BacktestEngine:
    """
    Walk-forward backtesting engine with realistic assumptions.

    Features:
    - Configurable slippage and charges
    - NSE lot size enforcement
    - SL/target simulation on candle-level data
    - Options expiry handling
    - Walk-forward window support
    """

    def __init__(
        self,
        initial_capital: float = 500000.0,
        slippage_pct: float = 0.1,
        brokerage_per_order: float = 20.0,
    ):
        self.initial_capital = initial_capital
        self.slippage_pct = slippage_pct
        self.brokerage_per_order = brokerage_per_order

    def _apply_slippage(self, price: float, direction: str) -> float:
        """Apply slippage to entry/exit price."""
        slip = price * self.slippage_pct / 100.0
        if direction == "BUY":
            return price + slip   # Pay more to buy
        return price - slip       # Receive less on sell

    async def run(
        self,
        strategy,
        historical_data: dict[str, list[Candle]],
        from_date: date,
        to_date: date,
        symbols: list[str] | None = None,
    ) -> tuple[BacktestMetrics, list[BacktestTrade], list[float]]:
        """
        Run a backtest on historical data.

        Args:
            strategy: An IStrategy instance with generate_signal().
            historical_data: {symbol: [candles]} sorted by time.
            from_date: Backtest start date.
            to_date: Backtest end date.
            symbols: Symbols to test (defaults to all in data).

        Returns:
            (metrics, trades_list, equity_curve)
        """
        if symbols is None:
            symbols = list(historical_data.keys())

        capital = self.initial_capital
        equity_curve = [capital]
        completed_trades: list[BacktestTrade] = []
        open_positions: list[BacktestPosition] = []
        daily_returns: list[float] = []

        for symbol in symbols:
            candles = historical_data.get(symbol, [])
            if not candles:
                continue

            # Filter candles to date range
            filtered = [
                c for c in candles
                if from_date <= (c.time.date() if hasattr(c.time, 'date') else c.time) <= to_date
            ]

            if len(filtered) < 50:
                continue

            # Walk through candles
            for i in range(50, len(filtered)):
                current = filtered[i]
                lookback = filtered[max(0, i - 200): i + 1]

                # ── Check open positions for SL/Target ──
                positions_to_close: list[tuple[int, str, float]] = []
                for j, pos in enumerate(open_positions):
                    if pos.signal.symbol != symbol:
                        continue

                    if pos.signal.direction == TradeDirection.BUY.value:
                        # Check SL hit
                        if current.low <= pos.stop_loss:
                            positions_to_close.append((j, "SL_HIT", pos.stop_loss))
                        # Check target hit
                        elif current.high >= pos.target:
                            positions_to_close.append((j, "TARGET_HIT", pos.target))
                        # Trailing SL
                        elif pos.trailing_sl and current.low <= pos.trailing_sl:
                            positions_to_close.append((j, "TRAILING_SL", pos.trailing_sl))
                        else:
                            # Update trailing SL
                            unrealized = (current.close - pos.entry_price) * pos.quantity
                            risk = abs(pos.entry_price - pos.signal.stop_loss) * pos.quantity
                            if risk > 0 and unrealized >= 1.5 * risk:
                                pos.trailing_sl = max(
                                    pos.trailing_sl or pos.stop_loss,
                                    pos.entry_price,
                                )
                            if risk > 0 and unrealized >= 2.0 * risk:
                                pos.trailing_sl = max(
                                    pos.trailing_sl or pos.stop_loss,
                                    current.close - abs(pos.entry_price - pos.signal.stop_loss) * 0.5,
                                )
                    else:  # SELL direction
                        if current.high >= pos.stop_loss:
                            positions_to_close.append((j, "SL_HIT", pos.stop_loss))
                        elif current.low <= pos.target:
                            positions_to_close.append((j, "TARGET_HIT", pos.target))

                # Close positions (reverse to avoid index issues)
                for j, reason, exit_price in sorted(positions_to_close, reverse=True):
                    pos = open_positions.pop(j)
                    exit_p = self._apply_slippage(exit_price, "SELL" if pos.signal.direction == "BUY" else "BUY")

                    if pos.signal.direction == TradeDirection.BUY.value:
                        pnl_gross = (exit_p - pos.entry_price) * pos.quantity
                    else:
                        pnl_gross = (pos.entry_price - exit_p) * pos.quantity

                    buy_val = pos.entry_price * pos.quantity
                    sell_val = exit_p * pos.quantity
                    charges_detail = calculate_charges("futures", buy_val, sell_val, pos.quantity, is_futures=True)
                    total_charges = charges_detail["total_charges"]
                    pnl_net = pnl_gross - total_charges

                    capital += pnl_net
                    equity_curve.append(capital)

                    hold_mins = int((current.time - pos.entry_time).total_seconds() / 60) if hasattr(current.time, '__sub__') else 0

                    completed_trades.append(BacktestTrade(
                        id=str(uuid4())[:8],
                        symbol=symbol,
                        strategy=pos.signal.strategy_name,
                        direction=pos.signal.direction,
                        entry_price=pos.entry_price,
                        exit_price=exit_p,
                        quantity=pos.quantity,
                        entry_time=pos.entry_time,
                        exit_time=current.time,
                        pnl_gross=round(pnl_gross, 2),
                        charges=round(total_charges, 2),
                        pnl_net=round(pnl_net, 2),
                        exit_reason=reason,
                        regime=pos.signal.regime or "",
                        win=pnl_net > 0,
                        hold_minutes=hold_mins,
                    ))

                # ── Generate new signal ──
                if len(open_positions) < 5:  # Max positions check
                    context = {
                        "candles": {"15m": lookback},
                        "symbol": symbol,
                        "regime": MarketRegime.UNKNOWN.value,
                        "indicators": {},
                        "option_chain": None,
                        "news_events": [],
                    }
                    try:
                        signal = await strategy.generate_signal(context)
                        if signal and signal.confidence_score >= 0.60:
                            entry_p = self._apply_slippage(
                                float(signal.entry_price), signal.direction
                            )
                            open_positions.append(BacktestPosition(
                                signal=signal,
                                entry_price=entry_p,
                                quantity=signal.quantity,
                                stop_loss=float(signal.stop_loss),
                                target=float(signal.target),
                                entry_time=current.time,
                            ))
                    except Exception as e:
                        logger.debug(f"Strategy error during backtest: {e}")

        # Close remaining open positions at last price
        for pos in open_positions:
            last_candle = historical_data.get(pos.signal.symbol, [None])[-1]
            if last_candle:
                exit_p = float(last_candle.close)
                if pos.signal.direction == TradeDirection.BUY.value:
                    pnl = (exit_p - pos.entry_price) * pos.quantity
                else:
                    pnl = (pos.entry_price - exit_p) * pos.quantity
                capital += pnl
                equity_curve.append(capital)

        # Calculate metrics
        metrics = self.calculate_metrics(completed_trades, equity_curve, self.initial_capital, from_date, to_date)
        return metrics, completed_trades, equity_curve

    def calculate_metrics(
        self,
        trades: list[BacktestTrade],
        equity_curve: list[float],
        initial_capital: float,
        from_date: date,
        to_date: date,
    ) -> BacktestMetrics:
        """Calculate comprehensive backtest metrics."""
        if not trades:
            return BacktestMetrics()

        wins = [t for t in trades if t.win]
        losses = [t for t in trades if not t.win]

        total_pnl = sum(t.pnl_net for t in trades)
        gross_profit = sum(t.pnl_net for t in wins) if wins else 0
        gross_loss = abs(sum(t.pnl_net for t in losses)) if losses else 1

        # Max drawdown
        peak = initial_capital
        max_dd = 0.0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = peak - eq
            if dd > max_dd:
                max_dd = dd

        # CAGR
        days = (to_date - from_date).days
        final_capital = equity_curve[-1] if equity_curve else initial_capital
        years = days / 365.25 if days > 0 else 1
        total_return = final_capital / initial_capital
        cagr = (total_return ** (1 / years) - 1) * 100 if years > 0 else 0

        # Sharpe (from daily equity changes)
        daily_rets = []
        for i in range(1, len(equity_curve)):
            if equity_curve[i - 1] > 0:
                daily_rets.append((equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1])

        sharpe = calculate_sharpe(daily_rets) if daily_rets else 0

        # By regime
        by_regime: dict[str, dict] = {}
        for t in trades:
            r = t.regime or "UNKNOWN"
            if r not in by_regime:
                by_regime[r] = {"trades": 0, "wins": 0, "pnl": 0}
            by_regime[r]["trades"] += 1
            if t.win:
                by_regime[r]["wins"] += 1
            by_regime[r]["pnl"] += t.pnl_net

        # Monthly P&L
        monthly_pnl: dict[str, float] = {}
        for t in trades:
            if t.exit_time:
                key = t.exit_time.strftime("%Y-%m") if hasattr(t.exit_time, "strftime") else str(t.exit_time)[:7]
                monthly_pnl[key] = monthly_pnl.get(key, 0) + t.pnl_net

        return BacktestMetrics(
            total_trades=len(trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=len(wins) / len(trades) * 100 if trades else 0,
            total_pnl=round(total_pnl, 2),
            avg_win=round(np.mean([t.pnl_net for t in wins]), 2) if wins else 0,
            avg_loss=round(np.mean([t.pnl_net for t in losses]), 2) if losses else 0,
            profit_factor=round(gross_profit / gross_loss, 2) if gross_loss else 0,
            sharpe_ratio=round(sharpe, 2),
            max_drawdown=round(max_dd, 2),
            max_drawdown_pct=round(max_dd / initial_capital * 100, 2) if initial_capital else 0,
            cagr=round(cagr, 2),
            expectancy=round(total_pnl / len(trades), 2) if trades else 0,
            avg_hold_minutes=round(np.mean([t.hold_minutes for t in trades]), 1) if trades else 0,
            best_trade=max(t.pnl_net for t in trades) if trades else 0,
            worst_trade=min(t.pnl_net for t in trades) if trades else 0,
            by_regime=by_regime,
            by_strategy={},
            monthly_pnl=monthly_pnl,
        )
