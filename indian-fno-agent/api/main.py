"""
FastAPI application — main entry point for the trading agent REST API.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config.settings import get_settings
from api.routes import signals, positions, trades, risk, admin, market, backtest
from api.websocket import router as ws_router

logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan — startup and shutdown logic.
    Initialises database pools, broker adapter, orchestrator, Telegram bot.
    """
    logger.info("🚀 Starting Indian F&O Trading Agent API...")

    # ── Startup ──
    # In production, initialise asyncpg pool, Redis, broker, etc.
    # These are stored on app.state for route access.
    app.state.settings = settings
    app.state.db_pool = None      # asyncpg pool placeholder
    app.state.redis = None        # Redis client placeholder
    app.state.broker = None       # IBrokerAdapter instance
    app.state.orchestrator = None  # TradingOrchestrator instance

    # Initialize & start Telegram bot polling
    try:
        from telegram_bot.bot import TelegramBot
        bot = TelegramBot()
        app.state.telegram_bot = bot
        await bot.start()
        logger.info("Telegram bot initialized and polling.")
    except Exception as e:
        logger.error(f"Failed to start Telegram bot: {e}")
        app.state.telegram_bot = None

    logger.info(f"Trading mode: {settings.TRADING_MODE}")
    logger.info(f"Active broker: {settings.BROKER}")
    logger.info("API startup complete.")

    yield

    # ── Shutdown ──
    logger.info("Shutting down API...")
    if app.state.telegram_bot:
        try:
            await app.state.telegram_bot.stop()
        except Exception:
            pass
    if app.state.orchestrator:
        try:
            await app.state.orchestrator.stop()
        except Exception:
            pass
    logger.info("API shutdown complete.")


# ── Create App ───────────────────────────────────────────────────────

app = FastAPI(
    title="Indian F&O Trading Agent API",
    description="Production-grade AI-powered algo trading agent for Indian Futures & Options",
    version=settings.VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── CORS ─────────────────────────────────────────────────────────────

cors_origins = settings.CORS_ORIGINS.split(",") if hasattr(settings, "CORS_ORIGINS") and settings.CORS_ORIGINS else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routes ───────────────────────────────────────────────────────────

app.include_router(signals.router, prefix="/api/v1", tags=["Signals"])
app.include_router(positions.router, prefix="/api/v1", tags=["Positions"])
app.include_router(trades.router, prefix="/api/v1", tags=["Trades"])
app.include_router(risk.router, prefix="/api/v1", tags=["Risk"])
app.include_router(admin.router, prefix="/api/v1", tags=["Admin"])
app.include_router(market.router, prefix="/api/v1", tags=["Market Data"])
app.include_router(backtest.router, prefix="/api/v1", tags=["Backtest"])
app.include_router(ws_router, tags=["WebSocket"])


# ── Health Check ─────────────────────────────────────────────────────

@app.get("/health")
async def health_check(request: Request):
    """System health check endpoint."""
    broker_connected = False
    if request.app.state.broker:
        broker_connected = request.app.state.broker.is_connected()

    return {
        "status": "healthy",
        "version": settings.VERSION,
        "trading_mode": settings.TRADING_MODE,
        "broker": settings.BROKER,
        "broker_connected": broker_connected,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── Exception Handlers ──────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_server_error",
            "detail": str(exc),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    return JSONResponse(
        status_code=404,
        content={
            "error": "not_found",
            "detail": f"Path {request.url.path} not found",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
