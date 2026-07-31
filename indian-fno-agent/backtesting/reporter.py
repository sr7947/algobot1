"""
Backtest results reporter — generates JSON reports and console summaries.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backtesting.engine import BacktestMetrics, BacktestTrade
from config.settings import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()


class BacktestReporter:
    """
    Generates reports from backtest results.
    Saves JSON, prints console summary, and analyses by regime.
    """

    def __init__(self):
        self._output_dir = Path(settings.BACKTEST_DIR)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def generate_report(
        self,
        metrics: BacktestMetrics,
        trades: list[BacktestTrade],
        equity_curve: list[float],
        strategy_name: str,
        from_date: str = "",
        to_date: str = "",
    ) -> Path:
        """
        Generate a comprehensive backtest report and save as JSON.

        Returns:
            Path to the saved report file.
        """
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "strategy": strategy_name,
            "period": {"from": from_date, "to": to_date},
            "metrics": {
                "total_trades": metrics.total_trades,
                "winning_trades": metrics.winning_trades,
                "losing_trades": metrics.losing_trades,
                "win_rate": metrics.win_rate,
                "total_pnl": metrics.total_pnl,
                "avg_win": metrics.avg_win,
                "avg_loss": metrics.avg_loss,
                "profit_factor": metrics.profit_factor,
                "sharpe_ratio": metrics.sharpe_ratio,
                "max_drawdown": metrics.max_drawdown,
                "max_drawdown_pct": metrics.max_drawdown_pct,
                "cagr": metrics.cagr,
                "expectancy": metrics.expectancy,
                "avg_hold_minutes": metrics.avg_hold_minutes,
                "best_trade": metrics.best_trade,
                "worst_trade": metrics.worst_trade,
            },
            "by_regime": metrics.by_regime,
            "monthly_pnl": metrics.monthly_pnl,
            "equity_curve": equity_curve,
            "trades": [
                {
                    "id": t.id,
                    "symbol": t.symbol,
                    "direction": t.direction,
                    "entry": t.entry_price,
                    "exit": t.exit_price,
                    "qty": t.quantity,
                    "pnl_net": t.pnl_net,
                    "charges": t.charges,
                    "exit_reason": t.exit_reason,
                    "regime": t.regime,
                    "win": t.win,
                    "hold_mins": t.hold_minutes,
                    "entry_time": str(t.entry_time),
                    "exit_time": str(t.exit_time),
                }
                for t in trades
            ],
        }

        # Save to file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"backtest_{strategy_name}_{timestamp}.json"
        filepath = self._output_dir / filename

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)

        logger.info(f"Backtest report saved to {filepath}")
        return filepath

    @staticmethod
    def print_summary(metrics: BacktestMetrics, strategy_name: str) -> str:
        """Print a formatted console summary. Returns the summary string."""
        lines = [
            "",
            "═" * 60,
            f"  BACKTEST RESULTS — {strategy_name}",
            "═" * 60,
            f"  Total Trades:    {metrics.total_trades}",
            f"  Winning:         {metrics.winning_trades} ({metrics.win_rate:.1f}%)",
            f"  Losing:          {metrics.losing_trades}",
            "─" * 60,
            f"  Total P&L:       ₹{metrics.total_pnl:,.2f}",
            f"  Avg Win:         ₹{metrics.avg_win:,.2f}",
            f"  Avg Loss:        ₹{metrics.avg_loss:,.2f}",
            f"  Profit Factor:   {metrics.profit_factor:.2f}",
            f"  Expectancy:      ₹{metrics.expectancy:,.2f} per trade",
            "─" * 60,
            f"  CAGR:            {metrics.cagr:.1f}%",
            f"  Sharpe Ratio:    {metrics.sharpe_ratio:.2f}",
            f"  Max Drawdown:    ₹{metrics.max_drawdown:,.2f} ({metrics.max_drawdown_pct:.1f}%)",
            f"  Avg Hold Time:   {metrics.avg_hold_minutes:.0f} minutes",
            "─" * 60,
            f"  Best Trade:      ₹{metrics.best_trade:,.2f}",
            f"  Worst Trade:     ₹{metrics.worst_trade:,.2f}",
            "═" * 60,
        ]

        # Regime breakdown
        if metrics.by_regime:
            lines.append("  PERFORMANCE BY REGIME:")
            lines.append("─" * 60)
            for regime, data in metrics.by_regime.items():
                wr = (data["wins"] / data["trades"] * 100) if data["trades"] else 0
                lines.append(f"  {regime:20s} | {data['trades']:3d} trades | {wr:5.1f}% WR | ₹{data['pnl']:,.0f}")

        # Monthly P&L
        if metrics.monthly_pnl:
            lines.append("")
            lines.append("  MONTHLY P&L:")
            lines.append("─" * 60)
            for month, pnl in sorted(metrics.monthly_pnl.items()):
                emoji = "🟢" if pnl >= 0 else "🔴"
                lines.append(f"  {month}  {emoji}  ₹{pnl:>10,.2f}")

        lines.append("═" * 60)
        lines.append("")

        summary = "\n".join(lines)
        print(summary)
        return summary

    @staticmethod
    def get_worst_drawdown_periods(
        equity_curve: list[float], top_n: int = 10
    ) -> list[dict]:
        """Find the worst drawdown periods in the equity curve."""
        if len(equity_curve) < 2:
            return []

        drawdowns: list[dict] = []
        peak = equity_curve[0]
        peak_idx = 0
        current_dd = 0.0
        dd_start = 0

        for i, eq in enumerate(equity_curve):
            if eq > peak:
                if current_dd > 0:
                    drawdowns.append({
                        "start_idx": dd_start,
                        "end_idx": i,
                        "peak": peak,
                        "trough": peak - current_dd,
                        "drawdown": round(current_dd, 2),
                        "drawdown_pct": round(current_dd / peak * 100, 2) if peak else 0,
                        "duration_bars": i - dd_start,
                    })
                peak = eq
                peak_idx = i
                current_dd = 0
                dd_start = i
            else:
                dd = peak - eq
                if dd > current_dd:
                    current_dd = dd

        # Final drawdown if still in one
        if current_dd > 0:
            drawdowns.append({
                "start_idx": dd_start,
                "end_idx": len(equity_curve) - 1,
                "peak": peak,
                "trough": peak - current_dd,
                "drawdown": round(current_dd, 2),
                "drawdown_pct": round(current_dd / peak * 100, 2) if peak else 0,
                "duration_bars": len(equity_curve) - 1 - dd_start,
            })

        drawdowns.sort(key=lambda x: x["drawdown"], reverse=True)
        return drawdowns[:top_n]
