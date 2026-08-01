"""
api/routes/signals.py
─────────────────────
FastAPI router for trade signal management.

Endpoints
---------
GET  /signals                     — list all signals (filterable)
GET  /signals/pending             — pending-approval signals
GET  /signals/{signal_id}         — signal detail + audit trail
POST /signals/{signal_id}/approve — manually approve a signal
POST /signals/{signal_id}/reject  — manually reject a signal
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_db_session
from db.repositories.signal_repository import SignalRepository
from models.signal import SignalStatus, SignalDirection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/signals", tags=["Signals"])


@router.get("/ip")
async def get_outbound_ip() -> Dict[str, Any]:
    """Get exact public outbound IP address of this Railway deployment."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get("https://api.ipify.org?format=json")
            return res.json()
    except Exception as exc:
        return {"error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Response / Request Models
# ─────────────────────────────────────────────────────────────────────────────


class AuditEntry(BaseModel):
    """Single audit-trail record attached to a signal."""

    event: str
    actor: str
    timestamp: datetime
    details: Optional[Dict[str, Any]] = None

    class Config:
        from_attributes = True


class SignalBase(BaseModel):
    """Minimal signal representation used in list responses."""

    signal_id: UUID
    symbol: str
    exchange: str
    strategy: str
    direction: SignalDirection
    entry_price: float
    stop_loss: float
    target_price: float
    quantity: int
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    regime: str
    status: SignalStatus
    generated_at: datetime

    class Config:
        from_attributes = True


class SignalDetail(SignalBase):
    """Full signal record including metadata and audit trail."""

    option_type: Optional[str] = None      # CE / PE / None for futures
    strike_price: Optional[float] = None
    expiry_date: Optional[date] = None
    lot_size: int
    risk_reward_ratio: float
    max_loss_amount: float
    indicators: Dict[str, Any] = Field(default_factory=dict)
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    audit_trail: List[AuditEntry] = Field(default_factory=list)

    class Config:
        from_attributes = True


class SignalListResponse(BaseModel):
    """Paginated list wrapper."""

    total: int
    limit: int
    offset: int
    items: List[SignalBase]


class ApproveRequest(BaseModel):
    """Optional actor/note when approving via API."""

    actor: str = "api_user"
    note: Optional[str] = None


class RejectRequest(BaseModel):
    """Required reason when rejecting via API."""

    actor: str = "api_user"
    reason: str = Field(..., min_length=5)


class ActionResponse(BaseModel):
    """Generic success/message response."""

    success: bool
    message: str
    signal_id: UUID


# ─────────────────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────────────────


def _build_filter_dict(
    status_filter: Optional[str],
    strategy: Optional[str],
    symbol: Optional[str],
    date_from: Optional[date],
    date_to: Optional[date],
) -> Dict[str, Any]:
    """Convert query params to a repository-compatible filter dict."""
    filters: Dict[str, Any] = {}
    if status_filter:
        filters["status"] = status_filter
    if strategy:
        filters["strategy"] = strategy
    if symbol:
        filters["symbol"] = symbol.upper()
    if date_from:
        filters["generated_at__gte"] = datetime.combine(date_from, datetime.min.time())
    if date_to:
        filters["generated_at__lte"] = datetime.combine(date_to, datetime.max.time())
    return filters


# ─────────────────────────────────────────────────────────────────────────────
# Route: GET /signals
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "",
    response_model=SignalListResponse,
    summary="List all signals",
    description=(
        "Returns a paginated list of signals. Supports filtering by status, "
        "strategy, symbol and date range."
    ),
)
async def list_signals(
    status_filter: Optional[str] = Query(
        None,
        alias="status",
        description="Filter by signal status: PENDING, APPROVED, REJECTED, EXECUTED, CANCELLED",
    ),
    strategy: Optional[str] = Query(None, description="Filter by strategy name"),
    symbol: Optional[str] = Query(None, description="Filter by underlying symbol e.g. NIFTY"),
    date_from: Optional[date] = Query(None, description="Start date (inclusive) YYYY-MM-DD"),
    date_to: Optional[date] = Query(None, description="End date (inclusive) YYYY-MM-DD"),
    limit: int = Query(50, ge=1, le=500, description="Page size"),
    offset: int = Query(0, ge=0, description="Page offset"),
    db: AsyncSession = Depends(get_db_session),
) -> SignalListResponse:
    """Return filtered + paginated signals."""
    repo = SignalRepository(db)
    filters = _build_filter_dict(status_filter, strategy, symbol, date_from, date_to)

    try:
        signals, total = await repo.list_signals(filters=filters, limit=limit, offset=offset)
    except Exception as exc:
        logger.exception("Failed to list signals: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve signals from database.",
        ) from exc

    return SignalListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[SignalBase.model_validate(s) for s in signals],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Route: GET /signals/pending  (must be BEFORE /{signal_id} to avoid conflict)
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "/pending",
    response_model=SignalListResponse,
    summary="List pending-approval signals",
)
async def list_pending_signals(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db_session),
) -> SignalListResponse:
    """Returns all signals awaiting human or automated approval."""
    repo = SignalRepository(db)
    try:
        signals, total = await repo.list_signals(
            filters={"status": SignalStatus.PENDING.value},
            limit=limit,
            offset=offset,
        )
    except Exception as exc:
        logger.exception("Failed to fetch pending signals: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve pending signals.",
        ) from exc

    return SignalListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[SignalBase.model_validate(s) for s in signals],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Route: GET /signals/{signal_id}
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "/{signal_id}",
    response_model=SignalDetail,
    summary="Get signal detail with audit trail",
)
async def get_signal(
    signal_id: UUID,
    db: AsyncSession = Depends(get_db_session),
) -> SignalDetail:
    """Retrieve a single signal by UUID including its full audit trail."""
    repo = SignalRepository(db)
    try:
        signal = await repo.get_by_id(signal_id)
    except Exception as exc:
        logger.exception("DB error fetching signal %s: %s", signal_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error while fetching signal.",
        ) from exc

    if signal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Signal {signal_id} not found.",
        )

    return SignalDetail.model_validate(signal)


# ─────────────────────────────────────────────────────────────────────────────
# Route: POST /signals/{signal_id}/approve
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/{signal_id}/approve",
    response_model=ActionResponse,
    summary="Manually approve a signal",
    status_code=status.HTTP_200_OK,
)
async def approve_signal(
    signal_id: UUID,
    body: ApproveRequest,
    db: AsyncSession = Depends(get_db_session),
) -> ActionResponse:
    """
    Approve a PENDING signal.

    - Validates signal exists and is in PENDING state.
    - Transitions status to APPROVED.
    - Appends audit-trail entry with actor and note.
    - The execution engine will pick it up in the next cycle.
    """
    repo = SignalRepository(db)
    signal = await repo.get_by_id(signal_id)

    if signal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Signal {signal_id} not found.")

    if signal.status != SignalStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Signal is in '{signal.status}' state; only PENDING signals can be approved.",
        )

    audit_entry: Dict[str, Any] = {
        "event": "APPROVED_VIA_API",
        "actor": body.actor,
        "timestamp": datetime.utcnow().isoformat(),
        "details": {"note": body.note},
    }

    try:
        await repo.update_status(
            signal_id=signal_id,
            new_status=SignalStatus.APPROVED,
            audit_entry=audit_entry,
            extra_fields={"approved_by": body.actor, "approved_at": datetime.utcnow()},
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.exception("Failed to approve signal %s: %s", signal_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to approve signal.",
        ) from exc

    logger.info("Signal %s approved by %s", signal_id, body.actor)
    return ActionResponse(success=True, message="Signal approved successfully.", signal_id=signal_id)


# ─────────────────────────────────────────────────────────────────────────────
# Route: POST /signals/{signal_id}/reject
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/{signal_id}/reject",
    response_model=ActionResponse,
    summary="Manually reject a signal",
    status_code=status.HTTP_200_OK,
)
async def reject_signal(
    signal_id: UUID,
    body: RejectRequest,
    db: AsyncSession = Depends(get_db_session),
) -> ActionResponse:
    """
    Reject a PENDING signal.

    - Validates signal exists and is in PENDING state.
    - Transitions status to REJECTED.
    - Stores rejection reason in the record and audit trail.
    """
    repo = SignalRepository(db)
    signal = await repo.get_by_id(signal_id)

    if signal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Signal {signal_id} not found.")

    if signal.status != SignalStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Signal is in '{signal.status}' state; only PENDING signals can be rejected.",
        )

    audit_entry: Dict[str, Any] = {
        "event": "REJECTED_VIA_API",
        "actor": body.actor,
        "timestamp": datetime.utcnow().isoformat(),
        "details": {"reason": body.reason},
    }

    try:
        await repo.update_status(
            signal_id=signal_id,
            new_status=SignalStatus.REJECTED,
            audit_entry=audit_entry,
            extra_fields={"rejection_reason": body.reason},
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.exception("Failed to reject signal %s: %s", signal_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reject signal.",
        ) from exc

    logger.info("Signal %s rejected by %s. Reason: %s", signal_id, body.actor, body.reason)
    return ActionResponse(success=True, message="Signal rejected successfully.", signal_id=signal_id)


@router.post("/trigger-sample", status_code=status.HTTP_201_CREATED)
async def trigger_sample_signal(
    request: Request,
    body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Generate a sample trade signal (NIFTY 50 or Crypto BTCUSD), register in bot memory, and push to Telegram."""
    from uuid import uuid4
    from datetime import datetime, timezone, timedelta
    from core.models import TradeSignal, MarketRegime, SignalStatus
    from telegram_bot.notifier import TelegramNotifier

    payload = body or {}
    sym = str(payload.get("symbol", "NIFTY 24400 CE")).upper()
    is_crypto = "BTC" in sym or "ETH" in sym or "CRYPTO" in str(payload.get("asset_class", "")).upper()

    if "ETH" in sym:
        sig_symbol = "ETHUSD"
        exchange = "DELTA"
        itype = "FUT"
        entry = 3450.00
        sl = 3320.00
        target = 3710.00
        qty = 5
        strategy = "Crypto Trend Breakout"
        rationale = [
            "ETHUSD broke above $3,400 key resistance with strong buying volume",
            "Ethereum Layer 2 TVL reached all-time high of $42 Billion",
            "Bullish EMA 9/21 cross on 1H timeframe confirmed",
        ]
        news = "Ethereum Staking ratio hits 28% as institutional demand grows"
    elif is_crypto:
        sig_symbol = "BTCUSD"
        exchange = "DELTA"
        itype = "FUT"
        entry = 65200.00
        sl = 63500.00
        target = 68600.00
        qty = 1
        strategy = "Crypto Trend Breakout"
        rationale = [
            "BTCUSD broke above key 4H resistance at $65,000",
            "Open Interest on Delta Exchange +14% with strong buying volume",
            "RSI momentum bullish at 62 with MACD histogram expansion",
        ]
        news = "Bitcoin ETF net inflows reach $450M; Fed signals upcoming rate cuts"
    else:
        sig_symbol = "NIFTY 24400 CE"
        exchange = "NFO"
        itype = "CE"
        entry = 145.00
        sl = 101.50
        target = 217.50
        qty = 50
        strategy = "Options Momentum"
        rationale = [
            "Price broke above 20-day high with 2.1x volume",
            "ADX at 31 confirms trending market",
            "PCR at 0.75 shows bullish bias",
        ]
        news = "Positive FII inflow data; RBI holds rates steady"

    from agents.gemini_reasoner import GeminiReasoningEngine

    reasoner = GeminiReasoningEngine()
    ai_res = await reasoner.evaluate_trade_signal(
        symbol=sig_symbol,
        direction="BUY",
        entry_price=entry,
        stop_loss=sl,
        target=target,
        technical_indicators={"EMA_Bull_Stack": True, "VWAP_Support": True, "RSI": 62, "Volume": "2.1x"},
        rationale=rationale,
        news_summary=news,
    )

    verdict_badge = f"🤖 Gemini AI Verdict: [{ai_res.get('verdict', 'APPROVE')}]"
    final_rationale = [verdict_badge] + ai_res.get("ai_rationale", rationale)

    signal = TradeSignal(
        id=uuid4(),
        created_at=datetime.now(timezone.utc),
        strategy_name=strategy,
        symbol=sig_symbol,
        exchange=exchange,
        instrument_type=itype,
        direction="BUY",
        entry_price=entry,
        stop_loss=sl,
        target=target,
        quantity=qty,
        lot_size=qty,
        confidence_score=0.82 if is_crypto else 0.78,
        regime=MarketRegime.TRENDING_BULL.value,
        rationale=final_rationale,
        news_summary=news,
        indicators_snapshot={},
        status=SignalStatus.PENDING_APPROVAL.value,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )

    bot = getattr(request.app.state, "telegram_bot", None)
    if bot:
        bot.register_signal(signal)

    try:
        import asyncio
        notifier = TelegramNotifier()
        asyncio.create_task(notifier.send_trade_card(signal, {"capital": 500000}))
    except Exception as exc:
        logger.warning("Could not dispatch Telegram card: %s", exc)

    return {
        "status": "success",
        "signal_id": str(signal.id),
        "symbol": sig_symbol,
        "message": f"Sample trade signal for {sig_symbol} triggered and registered in bot memory!",
    }
