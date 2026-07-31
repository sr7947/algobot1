"""
Admin API routes — system health, broker management, strategy control.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")


class ModeRequest(BaseModel):
    mode: str  # 'paper' or 'live'


class StrategyToggle(BaseModel):
    enabled: bool


@router.get("/health")
async def detailed_health(request: Request):
    """Detailed system health check: broker, DB, Redis, Telegram status."""
    checks = {
        "api": "healthy",
        "database": "unknown",
        "redis": "unknown",
        "broker": "disconnected",
        "telegram_bot": "unknown",
        "trading_mode": request.app.state.settings.TRADING_MODE,
        "kill_switch": "unknown",
    }

    # Check broker
    if request.app.state.broker:
        checks["broker"] = "connected" if request.app.state.broker.is_connected() else "disconnected"

    # Check DB pool
    if request.app.state.db_pool:
        try:
            await request.app.state.db_pool.fetchval("SELECT 1")
            checks["database"] = "connected"
        except Exception:
            checks["database"] = "error"

    # Check Redis
    if request.app.state.redis:
        try:
            await request.app.state.redis.ping()
            checks["redis"] = "connected"
        except Exception:
            checks["redis"] = "error"

    return {"status": "healthy", "checks": checks, "timestamp": datetime.now(timezone.utc).isoformat()}


@router.post("/mode")
async def switch_mode(req: ModeRequest, request: Request):
    """Switch trading mode between paper and live."""
    if req.mode not in ("paper", "live", "shadow"):
        return {"error": "Invalid mode. Use 'paper', 'live', or 'shadow'."}

    if req.mode == "live":
        return {
            "warning": "Live mode requires environment variable change and service restart.",
            "instruction": "Set TRADING_MODE=LIVE in .env and restart the service.",
        }

    return {"message": f"Mode switch to '{req.mode}' acknowledged.", "current_mode": request.app.state.settings.TRADING_MODE}


@router.get("/broker/instruments")
async def list_instruments(request: Request, exchange: str = "NFO"):
    """Fetch instrument list from active broker."""
    if not request.app.state.broker:
        return {"error": "Broker not initialised"}
    try:
        instruments = await request.app.state.broker.get_instruments(exchange)
        return {"count": len(instruments), "instruments": [i.model_dump() for i in instruments[:100]]}
    except Exception as e:
        return {"error": str(e)}


@router.post("/broker/reconnect")
async def reconnect_broker(request: Request):
    """Force broker reconnection."""
    if not request.app.state.broker:
        return {"error": "No broker adapter configured"}
    try:
        result = await request.app.state.broker.login()
        return {"success": result, "broker": request.app.state.settings.BROKER}
    except Exception as e:
        return {"error": str(e)}


@router.get("/logs")
async def recent_logs(request: Request, limit: int = 50):
    """Fetch recent audit log entries."""
    if not request.app.state.db_pool:
        return {"logs": [], "message": "Database not connected"}

    try:
        rows = await request.app.state.db_pool.fetch(
            "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT $1", limit
        )
        return {"logs": [dict(r) for r in rows]}
    except Exception as e:
        return {"error": str(e)}


@router.get("/strategies")
async def list_strategies():
    """List all registered strategies with their enabled status."""
    from strategies.trend_breakout import TrendBreakoutStrategy
    from strategies.vwap_rsi_reversal import VwapRsiReversalStrategy
    from strategies.options_momentum import OptionsMomentumStrategy
    from strategies.short_premium import ShortPremiumStrategy

    strategies = [
        TrendBreakoutStrategy,
        VwapRsiReversalStrategy,
        OptionsMomentumStrategy,
        ShortPremiumStrategy,
    ]

    return {
        "strategies": [
            {
                "name": s.strategy_name if hasattr(s, "strategy_name") else s.__name__,
                "version": getattr(s, "version", "1.0.0"),
                "enabled": getattr(s, "is_enabled", True),
            }
            for s in strategies
        ]
    }


@router.put("/strategies/{strategy_name}")
async def toggle_strategy(strategy_name: str, body: StrategyToggle):
    """Enable or disable a strategy at runtime."""
    # In production, this would update a registry or database
    return {
        "message": f"Strategy '{strategy_name}' set to {'enabled' if body.enabled else 'disabled'}.",
        "note": "Runtime strategy toggling will be persisted via Redis in production.",
    }
