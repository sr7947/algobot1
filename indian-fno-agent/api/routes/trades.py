"""
api/routes/trades.py
─────────────────────
FastAPI router for closed trade history and analytics.

Endpoints
---------
GET /trades                — list all closed trades (filterable)
GET /trades/analytics      — aggregated performance metrics
GET /trades/export         — CSV download of all trades
GET /trades/{trade_id}     — single trade detail
"""

from __future__ import annotations

import csv
import io
import logging
import math
import statistics
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_db_session
from db.repositories.trade_repository import TradeRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/trades", tags=["Trades"])


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────────────────────────────────────


class TradeBase(BaseModel):
    trade_id: UUID
    symbol: str
    exchange: str
    instrument_type: str       # FUT / CE / PE
    direction: str             # LONG / SHORT
    strategy: str
    regime: str
    entry_price: float
    exit_price: float
    quantity: int
    lot_size: int
    gross_pnl: float
    charges: float
    net_pnl: float
    net_pnl_pct: float
    entry_time: datetime
    exit_time: datetime
    hold_duration_mins: float
    exit_reason: str           # TARGET_HIT / SL_HIT / MANUAL / TIME_BASED / EXPIRY

    class Config:
        from_attributes = True


class TradeDetail(TradeBase):
    signal_id: Optional[UUID] = None
    entry_order_id: Optional[UUID] = None
    exit_order_id: Optional[UUID] = None
    strike_price: Optional[float] = None
    expiry_date: Optional[date] = None
    vix_at_entry: float
    premium_paid: Optional[float] = None
    broker_entry_id: Optional[str] = None
    broker_exit_id: Optional[str] = None
    notes: Optional[str] = None

    class Config:
        from_attributes = True


class TradeListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: List[TradeBase]


class StrategyBreakdown(BaseModel):
    trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    profit_factor: float
    avg_win: float
    avg_loss: float


class RegimeBreakdown(BaseModel):
    trades: int
    wins: int
    win_rate: float
    total_pnl: float


class TradeAnalytics(BaseModel):
    """Comprehensive performance metrics across all (or filtered) closed trades."""

    # Counts
    total_trades: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int

    # Core metrics
    win_rate: float = Field(..., description="Percentage of winning trades")
    profit_factor: float = Field(..., description="Gross profit / Gross loss")
    expectancy: float = Field(..., description="Average expected P&L per trade (₹)")

    # P&L
    total_gross_pnl: float
    total_charges: float
    total_net_pnl: float
    avg_win: float
    avg_loss: float
    largest_win: float
    largest_loss: float
    best_trade: Optional[Dict[str, Any]] = None
    worst_trade: Optional[Dict[str, Any]] = None

    # Risk metrics
    sharpe_ratio_approx: float = Field(..., description="Annualised Sharpe on daily P&L")
    max_drawdown: float = Field(..., description="Max drawdown in ₹ from equity peak")
    max_drawdown_pct: float
    avg_hold_time_mins: float

    # Breakdown
    by_strategy: Dict[str, StrategyBreakdown]
    by_regime: Dict[str, RegimeBreakdown]
    by_exit_reason: Dict[str, int]

    as_of: datetime


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────


def _safe_div(numerator: float, denominator: float, fallback: float = 0.0) -> float:
    """Division that returns fallback when denominator is zero."""
    return numerator / denominator if denominator != 0 else fallback


def _compute_analytics(trades: list) -> TradeAnalytics:
    """
    Compute all analytics from a list of trade ORM objects.
    All monetary values are in INR (₹).
    """
    if not trades:
        empty_breakdown: Dict[str, Any] = {}
        return TradeAnalytics(
            total_trades=0, winning_trades=0, losing_trades=0, breakeven_trades=0,
            win_rate=0.0, profit_factor=0.0, expectancy=0.0,
            total_gross_pnl=0.0, total_charges=0.0, total_net_pnl=0.0,
            avg_win=0.0, avg_loss=0.0, largest_win=0.0, largest_loss=0.0,
            sharpe_ratio_approx=0.0, max_drawdown=0.0, max_drawdown_pct=0.0,
            avg_hold_time_mins=0.0,
            by_strategy=empty_breakdown, by_regime=empty_breakdown, by_exit_reason=empty_breakdown,
            as_of=datetime.utcnow(),
        )

    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl < 0]
    breakevens = [t for t in trades if t.net_pnl == 0]

    gross_profit = sum(t.gross_pnl for t in wins)
    gross_loss = abs(sum(t.gross_pnl for t in losses))
    total_net = sum(t.net_pnl for t in trades)
    total_charges = sum(t.charges for t in trades)
    total_gross = sum(t.gross_pnl for t in trades)

    win_rate = _safe_div(len(wins) * 100, len(trades))
    profit_factor = _safe_div(gross_profit, gross_loss)
    avg_win = _safe_div(sum(t.net_pnl for t in wins), len(wins))
    avg_loss = _safe_div(sum(t.net_pnl for t in losses), len(losses))
    expectancy = (win_rate / 100) * avg_win + ((1 - win_rate / 100) * avg_loss)
    avg_hold = _safe_div(sum(t.hold_duration_mins for t in trades), len(trades))

    # ── Best / Worst trade ───────────────────────────────────────────────
    best = max(trades, key=lambda t: t.net_pnl)
    worst = min(trades, key=lambda t: t.net_pnl)

    def _trade_summary(t: Any) -> Dict[str, Any]:
        return {
            "trade_id": str(t.trade_id),
            "symbol": t.symbol,
            "strategy": t.strategy,
            "net_pnl": t.net_pnl,
            "entry_time": t.entry_time.isoformat(),
        }

    # ── Sharpe ratio (approx on daily net P&L) ───────────────────────────
    # Group net_pnl by date
    daily: Dict[date, float] = {}
    for t in trades:
        d = t.entry_time.date()
        daily[d] = daily.get(d, 0.0) + t.net_pnl
    daily_returns = list(daily.values())
    risk_free_daily = 0.065 / 252  # 6.5% annualised risk-free rate
    if len(daily_returns) >= 2:
        mean_ret = statistics.mean(daily_returns)
        std_ret = statistics.stdev(daily_returns)
        sharpe = _safe_div((mean_ret - risk_free_daily) * math.sqrt(252), std_ret)
    else:
        sharpe = 0.0

    # ── Max drawdown (on running equity curve) ───────────────────────────
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    peak_equity = 0.0
    for t in sorted(trades, key=lambda t: t.exit_time):
        equity += t.net_pnl
        if equity > peak:
            peak = equity
            peak_equity = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    max_dd_pct = _safe_div(max_dd * 100, peak_equity) if peak_equity > 0 else 0.0

    # ── By strategy ───────────────────────────────────────────────────────
    strategy_map: Dict[str, list] = {}
    for t in trades:
        strategy_map.setdefault(t.strategy, []).append(t)
    by_strategy: Dict[str, StrategyBreakdown] = {}
    for strat, strades in strategy_map.items():
        sw = [x for x in strades if x.net_pnl > 0]
        sl = [x for x in strades if x.net_pnl < 0]
        gp = sum(x.gross_pnl for x in sw)
        gl = abs(sum(x.gross_pnl for x in sl))
        by_strategy[strat] = StrategyBreakdown(
            trades=len(strades),
            wins=len(sw),
            losses=len(sl),
            win_rate=round(_safe_div(len(sw) * 100, len(strades)), 2),
            total_pnl=round(sum(x.net_pnl for x in strades), 2),
            profit_factor=round(_safe_div(gp, gl), 2),
            avg_win=round(_safe_div(sum(x.net_pnl for x in sw), len(sw)), 2),
            avg_loss=round(_safe_div(sum(x.net_pnl for x in sl), len(sl)), 2),
        )

    # ── By regime ─────────────────────────────────────────────────────────
    regime_map: Dict[str, list] = {}
    for t in trades:
        regime_map.setdefault(t.regime, []).append(t)
    by_regime: Dict[str, RegimeBreakdown] = {}
    for regime, rtrades in regime_map.items():
        rw = [x for x in rtrades if x.net_pnl > 0]
        by_regime[regime] = RegimeBreakdown(
            trades=len(rtrades),
            wins=len(rw),
            win_rate=round(_safe_div(len(rw) * 100, len(rtrades)), 2),
            total_pnl=round(sum(x.net_pnl for x in rtrades), 2),
        )

    # ── By exit reason ────────────────────────────────────────────────────
    exit_reason_map: Dict[str, int] = {}
    for t in trades:
        exit_reason_map[t.exit_reason] = exit_reason_map.get(t.exit_reason, 0) + 1

    return TradeAnalytics(
        total_trades=len(trades),
        winning_trades=len(wins),
        losing_trades=len(losses),
        breakeven_trades=len(breakevens),
        win_rate=round(win_rate, 2),
        profit_factor=round(profit_factor, 2),
        expectancy=round(expectancy, 2),
        total_gross_pnl=round(total_gross, 2),
        total_charges=round(total_charges, 2),
        total_net_pnl=round(total_net, 2),
        avg_win=round(avg_win, 2),
        avg_loss=round(avg_loss, 2),
        largest_win=round(max((t.net_pnl for t in trades), default=0.0), 2),
        largest_loss=round(min((t.net_pnl for t in trades), default=0.0), 2),
        best_trade=_trade_summary(best),
        worst_trade=_trade_summary(worst),
        sharpe_ratio_approx=round(sharpe, 3),
        max_drawdown=round(max_dd, 2),
        max_drawdown_pct=round(max_dd_pct, 2),
        avg_hold_time_mins=round(avg_hold, 1),
        by_strategy=by_strategy,
        by_regime=by_regime,
        by_exit_reason=exit_reason_map,
        as_of=datetime.utcnow(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Route: GET /trades/analytics  (before /{trade_id} to avoid routing clash)
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "/analytics",
    response_model=TradeAnalytics,
    summary="Aggregated trade performance analytics",
)
async def get_trade_analytics(
    strategy: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    db: AsyncSession = Depends(get_db_session),
) -> TradeAnalytics:
    """
    Returns comprehensive performance metrics including win rate, profit factor,
    Sharpe ratio (approximate), max drawdown, and per-strategy / per-regime breakdowns.
    """
    repo = TradeRepository(db)
    filters: Dict[str, Any] = {}
    if strategy:
        filters["strategy"] = strategy
    if symbol:
        filters["symbol"] = symbol.upper()
    if date_from:
        filters["entry_time__gte"] = datetime.combine(date_from, datetime.min.time())
    if date_to:
        filters["exit_time__lte"] = datetime.combine(date_to, datetime.max.time())

    try:
        trades, _ = await repo.list_trades(filters=filters, limit=100_000, offset=0)
    except Exception as exc:
        logger.exception("Failed to fetch trades for analytics: %s", exc)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="DB error.") from exc

    return _compute_analytics(trades)


# ─────────────────────────────────────────────────────────────────────────────
# Route: GET /trades/export  (before /{trade_id})
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "/export",
    summary="Export all trades as CSV",
    response_class=StreamingResponse,
)
async def export_trades(
    strategy: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    db: AsyncSession = Depends(get_db_session),
) -> StreamingResponse:
    """
    Streams a CSV file of all closed trades matching the given filters.
    Suitable for download by the dashboard or for import into Excel.
    """
    repo = TradeRepository(db)
    filters: Dict[str, Any] = {}
    if strategy:
        filters["strategy"] = strategy
    if symbol:
        filters["symbol"] = symbol.upper()
    if date_from:
        filters["entry_time__gte"] = datetime.combine(date_from, datetime.min.time())
    if date_to:
        filters["exit_time__lte"] = datetime.combine(date_to, datetime.max.time())

    try:
        trades, _ = await repo.list_trades(filters=filters, limit=100_000, offset=0)
    except Exception as exc:
        logger.exception("Failed to fetch trades for export: %s", exc)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="DB error.") from exc

    # Build CSV in memory
    output = io.StringIO()
    fieldnames = [
        "trade_id", "symbol", "exchange", "instrument_type", "direction",
        "strategy", "regime", "entry_price", "exit_price", "quantity",
        "lot_size", "gross_pnl", "charges", "net_pnl", "net_pnl_pct",
        "entry_time", "exit_time", "hold_duration_mins", "exit_reason",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    for t in trades:
        writer.writerow({
            "trade_id": str(t.trade_id),
            "symbol": t.symbol,
            "exchange": t.exchange,
            "instrument_type": t.instrument_type,
            "direction": t.direction,
            "strategy": t.strategy,
            "regime": t.regime,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "quantity": t.quantity,
            "lot_size": t.lot_size,
            "gross_pnl": t.gross_pnl,
            "charges": t.charges,
            "net_pnl": t.net_pnl,
            "net_pnl_pct": t.net_pnl_pct,
            "entry_time": t.entry_time.isoformat(),
            "exit_time": t.exit_time.isoformat(),
            "hold_duration_mins": t.hold_duration_mins,
            "exit_reason": t.exit_reason,
        })

    output.seek(0)
    filename = f"trades_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Route: GET /trades
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/list-raw")
async def list_trades_raw(asset_class: Optional[str] = Query(None)) -> Dict[str, Any]:
    """Return raw list of closed trades from memory, filtered by asset_class (FNO vs CRYPTO)."""
    from api.routes.positions import COMPLETED_TRADES

    target_class = asset_class.upper() if asset_class else None
    filtered = []

    for t in COMPLETED_TRADES:
        if target_class and t.get("asset_class", "FNO") != target_class:
            continue
        filtered.append(t)

    return {"status": "success", "total": len(filtered), "trades": filtered}


@router.get("/analytics-raw")
async def get_trades_analytics_raw(asset_class: Optional[str] = Query(None)) -> Dict[str, Any]:
    """Return aggregated analytics from memory for closed trades."""
    from api.routes.positions import COMPLETED_TRADES

    target_class = asset_class.upper() if asset_class else None
    trades = [t for t in COMPLETED_TRADES if (not target_class or t.get("asset_class", "FNO") == target_class)]

    total_trades = len(trades)
    if total_trades == 0:
        return {
            "status": "success",
            "total_trades": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "profit_factor": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "max_drawdown": 0.0,
            "sharpe_ratio": 0.0,
        }

    wins = [t for t in trades if t.get("net_pnl", 0.0) > 0]
    losses = [t for t in trades if t.get("net_pnl", 0.0) < 0]

    tot_pnl = round(sum(t.get("net_pnl", 0.0) for t in trades), 2)
    win_rate = round(len(wins) / total_trades * 100, 1)
    gp = sum(t.get("net_pnl", 0.0) for t in wins)
    gl = abs(sum(t.get("net_pnl", 0.0) for t in losses))
    pf = round(gp / gl, 2) if gl > 0 else (round(gp, 2) if gp > 0 else 0.0)
    avg_w = round(gp / len(wins), 2) if wins else 0.0
    avg_l = round(sum(t.get("net_pnl", 0.0) for t in losses) / len(losses), 2) if losses else 0.0

    return {
        "status": "success",
        "total_trades": total_trades,
        "win_rate": win_rate,
        "total_pnl": tot_pnl,
        "profit_factor": pf,
        "avg_win": avg_w,
        "avg_loss": avg_l,
        "max_drawdown": 0.0,
        "sharpe_ratio": 0.0,
    }


@router.get(
    "",
    response_model=TradeListResponse,
    summary="List all closed trades with filters",
)
async def list_trades(
    strategy: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    direction: Optional[str] = Query(None, description="LONG or SHORT"),
    regime: Optional[str] = Query(None),
    exit_reason: Optional[str] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db_session),
) -> TradeListResponse:
    """Return paginated list of closed trades with optional filters."""
    repo = TradeRepository(db)
    filters: Dict[str, Any] = {}
    if strategy:
        filters["strategy"] = strategy
    if symbol:
        filters["symbol"] = symbol.upper()
    if direction:
        filters["direction"] = direction.upper()
    if regime:
        filters["regime"] = regime
    if exit_reason:
        filters["exit_reason"] = exit_reason
    if date_from:
        filters["entry_time__gte"] = datetime.combine(date_from, datetime.min.time())
    if date_to:
        filters["exit_time__lte"] = datetime.combine(date_to, datetime.max.time())

    try:
        trades, total = await repo.list_trades(filters=filters, limit=limit, offset=offset)
    except Exception as exc:
        logger.exception("Failed to list trades: %s", exc)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="DB error.") from exc

    return TradeListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[TradeBase.model_validate(t) for t in trades],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Route: GET /trades/{trade_id}
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "/{trade_id}",
    response_model=TradeDetail,
    summary="Get single trade detail",
)
async def get_trade(
    trade_id: UUID,
    db: AsyncSession = Depends(get_db_session),
) -> TradeDetail:
    """Fetch a single closed trade by its UUID."""
    repo = TradeRepository(db)
    trade = await repo.get_by_id(trade_id)

    if trade is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Trade {trade_id} not found.",
        )

    return TradeDetail.model_validate(trade)
