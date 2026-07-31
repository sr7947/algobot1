"""
FastAPI dependency injection utilities.
"""
from __future__ import annotations

from typing import Optional
from fastapi import Request
from risk.engine import RiskEngine
from broker.base import IBrokerAdapter


async def get_risk_engine(request: Request) -> Optional[RiskEngine]:
    """Dependency provider for RiskEngine."""
    if hasattr(request.app.state, "risk_engine"):
        return request.app.state.risk_engine
    return RiskEngine()


async def get_broker(request: Request) -> Optional[IBrokerAdapter]:
    """Dependency provider for active broker adapter."""
    if hasattr(request.app.state, "broker"):
        return request.app.state.broker
    return None
