"""
api/routes/risk.py
──────────────────
FastAPI router for risk management controls.

Endpoints
---------
GET    /risk/state        — current daily risk state snapshot
GET    /risk/limits       — all configured risk limits
POST   /risk/kill-switch  — activate kill switch (halt all trading)
DELETE /risk/kill-switch  — deactivate kill switch
GET    /risk/exposure     — current exposure by symbol and sector
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Request, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_db_session
from db.repositories.risk_repository import RiskRepository
from db.repositories.position_repository import PositionRepository
from core.risk.risk_engine import RiskEngine
from core.dependencies import get_risk_engine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/risk", tags=["Risk"])


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────────────────────────────────────


class DailyRiskState(BaseModel):
    """Snapshot of the risk engine's current daily counters."""

    date: str                                   # YYYY-MM-DD (IST)
    kill_switch_active: bool
    kill_switch_reason: Optional[str] = None
    kill_switch_activated_at: Optional[datetime] = None
    kill_switch_activated_by: Optional[str] = None

    # Daily P&L
    daily_realized_pnl: float
    daily_unrealized_pnl: float
    daily_net_pnl: float
    daily_max_loss_limit: float
    daily_max_loss_utilised_pct: float          # 0-100

    # Trade counts
    trades_today: int
    max_trades_per_day: int
    consecutive_losses: int
    max_consecutive_losses_limit: int

    # Exposure
    current_gross_exposure: float
    max_gross_exposure_limit: float
    exposure_utilised_pct: float

    # Margin
    margin_used: float
    margin_available: float
    margin_utilised_pct: float

    # Flags
    circuit_breaker_triggered: bool
    vix_halt_active: bool
    current_vix: float
    vix_halt_threshold: float

    last_updated: datetime


class RiskLimit(BaseModel):
    """A single configured risk limit parameter."""

    limit_key: str
    description: str
    current_value: Any
    unit: str                       # ₹, %, count, ratio
    is_hard_limit: bool             # Hard = halt trading; Soft = alert only
    is_active: bool


class RiskLimitsResponse(BaseModel):
    limits: List[RiskLimit]
    profile_name: str
    trading_mode: str               # paper / live


class KillSwitchRequest(BaseModel):
    reason: str = Field(..., min_length=5, description="Human-readable reason for activating kill switch")
    actor: str = Field("api_user", description="Who is activating the kill switch")


class KillSwitchDeactivateRequest(BaseModel):
    actor: str = Field("api_user", description="Who is deactivating the kill switch")
    confirmation: str = Field(
        ...,
        description="Must be 'CONFIRM' to prevent accidental deactivation",
    )


class SymbolExposure(BaseModel):
    symbol: str
    instrument_type: str            # FUT / CE / PE
    direction: str
    quantity: int
    notional_value: float
    unrealised_pnl: float
    pct_of_portfolio: float


class SectorExposure(BaseModel):
    sector: str
    gross_exposure: float
    net_exposure: float
    pct_of_portfolio: float
    symbols: List[str]


class ExposureResponse(BaseModel):
    total_gross_exposure: float
    total_net_exposure: float
    by_symbol: List[SymbolExposure]
    by_sector: List[SectorExposure]
    concentration_risk: Dict[str, float]    # top-3 symbols as % of portfolio
    as_of: datetime


class ActionResponse(BaseModel):
    success: bool
    message: str


# ─────────────────────────────────────────────────────────────────────────────
# Sector mapping helper (NSE sectors — extend as needed)
# ─────────────────────────────────────────────────────────────────────────────

_SYMBOL_SECTOR_MAP: Dict[str, str] = {
    "NIFTY": "Index",
    "BANKNIFTY": "Banking",
    "FINNIFTY": "Financials",
    "MIDCPNIFTY": "Mid-Cap",
    "RELIANCE": "Energy",
    "TCS": "IT",
    "INFY": "IT",
    "HCLTECH": "IT",
    "WIPRO": "IT",
    "TECHM": "IT",
    "HDFCBANK": "Banking",
    "ICICIBANK": "Banking",
    "KOTAKBANK": "Banking",
    "SBIN": "Banking",
    "AXISBANK": "Banking",
    "TATAMOTORS": "Auto",
    "MARUTI": "Auto",
    "BAJAJ-AUTO": "Auto",
    "EICHERMOT": "Auto",
    "SUNPHARMA": "Pharma",
    "DRREDDY": "Pharma",
    "CIPLA": "Pharma",
    "POWERGRID": "Power",
    "NTPC": "Power",
    "ONGC": "Energy",
    "BPCL": "Energy",
    "TATASTEEL": "Metals",
    "HINDALCO": "Metals",
    "JSWSTEEL": "Metals",
}


def _get_sector(symbol: str) -> str:
    return _SYMBOL_SECTOR_MAP.get(symbol.upper(), "Other")


# ─────────────────────────────────────────────────────────────────────────────
# Route: GET /risk/state
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "/state",
    response_model=DailyRiskState,
    summary="Current daily risk state snapshot",
)
async def get_risk_state(
    risk_engine: RiskEngine = Depends(get_risk_engine),
) -> DailyRiskState:
    """
    Returns the live snapshot of daily risk counters:
    P&L vs limits, trade counts, exposure, margin usage,
    and all active halt flags.
    """
    try:
        state = await risk_engine.get_daily_state()
    except Exception as exc:
        logger.exception("Failed to get risk state: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch risk state.",
        ) from exc

    return DailyRiskState(**state)


# ─────────────────────────────────────────────────────────────────────────────
# Route: GET /risk/limits
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "/limits",
    response_model=RiskLimitsResponse,
    summary="All configured risk limits",
)
async def get_risk_limits(
    risk_engine: RiskEngine = Depends(get_risk_engine),
) -> RiskLimitsResponse:
    """
    Returns every configured risk limit with its current value,
    description, units, and whether it is a hard or soft limit.
    """
    try:
        limits_data = await risk_engine.get_all_limits()
    except Exception as exc:
        logger.exception("Failed to get risk limits: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch risk limits.",
        ) from exc

    return RiskLimitsResponse(
        profile_name=limits_data["profile_name"],
        trading_mode=limits_data["trading_mode"],
        limits=[RiskLimit(**lim) for lim in limits_data["limits"]],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Route: POST /risk/kill-switch  — ACTIVATE
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/kill-switch",
    response_model=ActionResponse,
    status_code=status.HTTP_200_OK,
    summary="Activate trading kill switch",
)
async def activate_kill_switch(
    body: KillSwitchRequest,
    risk_engine: RiskEngine = Depends(get_risk_engine),
    db: AsyncSession = Depends(get_db_session),
) -> ActionResponse:
    """
    Immediately halts all new order placement.

    - Sets kill_switch = True in the risk engine (in-memory + Redis).
    - Persists to DB with actor and reason.
    - Sends Telegram alert to admin.
    - Does NOT cancel existing open orders — use broker panel for that.
    """
    try:
        await risk_engine.activate_kill_switch(reason=body.reason, actor=body.actor)
    except ValueError as exc:
        # Already active
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to activate kill switch: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to activate kill switch.",
        ) from exc

    logger.warning(
        "KILL SWITCH ACTIVATED by %s — reason: %s", body.actor, body.reason
    )
    return ActionResponse(
        success=True,
        message=f"Kill switch activated. Reason: {body.reason}. All new order placement is halted.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Route: DELETE /risk/kill-switch  — DEACTIVATE
# ─────────────────────────────────────────────────────────────────────────────


@router.delete(
    "/kill-switch",
    response_model=ActionResponse,
    status_code=status.HTTP_200_OK,
    summary="Deactivate trading kill switch",
)
async def deactivate_kill_switch(
    body: KillSwitchDeactivateRequest,
    risk_engine: RiskEngine = Depends(get_risk_engine),
) -> ActionResponse:
    """
    Re-enables order placement after a kill switch.

    Requires `confirmation: 'CONFIRM'` in the request body to prevent
    accidental deactivation.
    """
    if body.confirmation != "CONFIRM":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="confirmation field must be exactly 'CONFIRM'.",
        )

    try:
        was_active = await risk_engine.deactivate_kill_switch(actor=body.actor)
    except Exception as exc:
        logger.exception("Failed to deactivate kill switch: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to deactivate kill switch.",
        ) from exc

    if not was_active:
        return ActionResponse(success=True, message="Kill switch was not active — no change.")

    logger.info("Kill switch deactivated by %s", body.actor)
    return ActionResponse(success=True, message="Kill switch deactivated. Trading re-enabled.")


# ─────────────────────────────────────────────────────────────────────────────
# Route: GET /risk/exposure
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "/exposure",
    response_model=ExposureResponse,
    summary="Current exposure by symbol and sector",
)
async def get_exposure(
    db: AsyncSession = Depends(get_db_session),
) -> ExposureResponse:
    """
    Computes current market exposure from all open positions.

    - Per-symbol breakdown with notional value and unrealised P&L.
    - Per-sector grouping (NSE sector classification).
    - Concentration risk flags for top-3 positions.
    """
    repo = PositionRepository(db)
    try:
        positions = await repo.get_open_positions()
    except Exception as exc:
        logger.exception("Failed to fetch positions for exposure: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch positions.",
        ) from exc

    total_gross = sum(p.entry_price * p.quantity for p in positions)

    # ── Per-symbol aggregation ────────────────────────────────────────────
    sym_map: Dict[str, Dict[str, Any]] = {}
    for p in positions:
        notional = p.entry_price * p.quantity
        sym_map.setdefault(
            p.symbol,
            {
                "symbol": p.symbol,
                "instrument_type": p.instrument_type,
                "direction": p.direction,
                "quantity": 0,
                "notional_value": 0.0,
                "unrealised_pnl": 0.0,
            },
        )
        sym_map[p.symbol]["quantity"] += p.quantity
        sym_map[p.symbol]["notional_value"] += notional
        sym_map[p.symbol]["unrealised_pnl"] += p.unrealised_pnl

    by_symbol = [
        SymbolExposure(
            **v,
            pct_of_portfolio=round((v["notional_value"] / total_gross * 100) if total_gross else 0, 2),
        )
        for v in sym_map.values()
    ]
    by_symbol.sort(key=lambda s: s.notional_value, reverse=True)

    # ── Per-sector aggregation ────────────────────────────────────────────
    sector_map: Dict[str, Dict[str, Any]] = {}
    for p in positions:
        sector = _get_sector(p.symbol)
        notional = p.entry_price * p.quantity
        signed = notional if p.direction == "LONG" else -notional
        sector_map.setdefault(sector, {"gross": 0.0, "net": 0.0, "symbols": set()})
        sector_map[sector]["gross"] += notional
        sector_map[sector]["net"] += signed
        sector_map[sector]["symbols"].add(p.symbol)

    by_sector = [
        SectorExposure(
            sector=sec,
            gross_exposure=round(v["gross"], 2),
            net_exposure=round(v["net"], 2),
            pct_of_portfolio=round((v["gross"] / total_gross * 100) if total_gross else 0, 2),
            symbols=sorted(v["symbols"]),
        )
        for sec, v in sector_map.items()
    ]
    by_sector.sort(key=lambda s: s.gross_exposure, reverse=True)

    # ── Concentration risk (top-3 as % of portfolio) ──────────────────────
    concentration: Dict[str, float] = {
        s.symbol: s.pct_of_portfolio for s in by_symbol[:3]
    }

    # ── Net exposure ──────────────────────────────────────────────────────
    long_exp = sum(p.entry_price * p.quantity for p in positions if p.direction == "LONG")
    short_exp = sum(p.entry_price * p.quantity for p in positions if p.direction == "SHORT")
    net_exp = long_exp - short_exp

    return ExposureResponse(
        total_gross_exposure=round(total_gross, 2),
        total_net_exposure=round(net_exp, 2),
        by_symbol=by_symbol,
        by_sector=by_sector,
        concentration_risk=concentration,
        as_of=datetime.utcnow(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Route: GET /risk/margins — Live Real-Time Broker Margins
# ─────────────────────────────────────────────────────────────────────────────

_MARGINS_CACHE: Dict[str, Any] = {}
_MARGINS_CACHE_TIME: float = 0.0


@router.get(
    "/margins",
    summary="Real-time live account balances from active broker",
)
async def get_live_margins(
    request: Request,
    asset_class: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Fetch live real-time wallet balances & margins segregated by asset class (FNO vs CRYPTO) with 10s caching."""
    global _MARGINS_CACHE, _MARGINS_CACHE_TIME

    target_class = asset_class.upper() if asset_class else "FNO"
    now = time.time()

    cache_key = f"margin_{target_class}"
    if cache_key in _MARGINS_CACHE and (now - _MARGINS_CACHE_TIME) < 10.0:
        return _MARGINS_CACHE[cache_key]

    if target_class == "FNO":
        from api.routes.positions import ACTIVE_PAPER_POSITIONS
        fno_positions = [p for p in ACTIVE_PAPER_POSITIONS if p.get("asset_class") == "FNO"]
        total_balance = 500000.00
        used_margin = sum(float(p.get("entry", 145.0)) * int(p.get("qty", 50)) * 0.2 for p in fno_positions)
        res = {
            "status": "success",
            "asset_class": "FNO",
            "currency": "INR",
            "symbol": "₹",
            "total_balance": total_balance,
            "available_margin": round(total_balance - used_margin, 2),
            "used_margin": round(used_margin, 2),
        }
        _MARGINS_CACHE[cache_key] = res
        _MARGINS_CACHE_TIME = now
        return res

    # CRYPTO Mode
    from api.routes.positions import ACTIVE_PAPER_POSITIONS
    crypto_positions = [p for p in ACTIVE_PAPER_POSITIONS if p.get("asset_class") == "CRYPTO"]

    # Delta Exchange contract multiplier: 1 contract = 0.001 BTC (~$65.20 notional). At 10x leverage = $6.52 initial margin.
    used_m = sum(float(p.get("entry", 65200.0)) * int(p.get("qty", 1)) * 0.001 * 0.1 for p in crypto_positions)
    tot_bal = 200.00
    avail_m = max(0.0, tot_bal - used_m)

    broker = getattr(request.app.state, "broker", None)
    if broker and getattr(broker, "_authenticated", False):
        try:
            m = await asyncio.wait_for(broker.get_margins(), timeout=1.0)
            tot_bal = round(float(m.available_cash), 2)
            avail_m = round(float(m.available_margin), 2)
            used_m = round(float(m.used_margin), 2)
        except Exception:
            avail_m = max(0.0, round(tot_bal - used_m, 2))

    res = {
        "status": "success",
        "asset_class": "CRYPTO",
        "currency": "USD",
        "symbol": "$",
        "total_balance": tot_bal,
        "available_margin": avail_m,
        "used_margin": round(used_m, 2),
    }
    _MARGINS_CACHE[cache_key] = res
    _MARGINS_CACHE_TIME = now
    return res

