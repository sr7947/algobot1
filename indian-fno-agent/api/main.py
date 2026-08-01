"""
FastAPI application — main entry point for the trading agent REST API.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

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
    app.state.settings = settings
    app.state.db_pool = None      # asyncpg pool placeholder
    app.state.redis = None        # Redis client placeholder
    app.state.orchestrator = None  # TradingOrchestrator instance

    # Instantiate Broker Adapter in background task
    try:
        from broker.base import BrokerFactory
        app.state.broker = BrokerFactory.create(settings.BROKER, settings)
        asyncio.create_task(app.state.broker.login())
        logger.info(f"Broker adapter '{settings.BROKER}' instantiated successfully.")
    except Exception as e:
        logger.error(f"Failed to instantiate broker '{settings.BROKER}': {e}")
        app.state.broker = None

    # Initialize & start Telegram bot polling in background task
    try:
        from telegram_bot.bot import TelegramBot
        bot = TelegramBot()
        app.state.telegram_bot = bot
        asyncio.create_task(bot.start())
        logger.info("Telegram bot initialized.")
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API Routes ───────────────────────────────────────────────────────

app.include_router(signals.router, prefix="/api/v1", tags=["Signals"])
app.include_router(positions.router, prefix="/api/v1", tags=["Positions"])
app.include_router(trades.router, prefix="/api/v1", tags=["Trades"])
app.include_router(risk.router, prefix="/api/v1", tags=["Risk"])
app.include_router(admin.router, prefix="/api/v1", tags=["Admin"])
app.include_router(market.router, prefix="/api/v1", tags=["Market Data"])
app.include_router(backtest.router, prefix="/api/v1", tags=["Backtest"])
app.include_router(ws_router, tags=["WebSocket"])


# ── Health Check ─────────────────────────────────────────────────────

@app.get("/api/v1/ip")
async def get_outbound_ip():
    """Get exact public outbound IP address of this Railway deployment."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get("https://api.ipify.org?format=json")
            return res.json()
    except Exception as exc:
        return {"error": str(exc)}


@app.get("/health", tags=["Health"])
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


# ── Dashboard & Static Assets Serving ─────────────────────────────────

dist_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "dashboard", "dist"))
assets_path = os.path.join(dist_path, "assets")

if os.path.exists(assets_path):
    app.mount("/assets", StaticFiles(directory=assets_path), name="assets")

@app.get("/")
async def root():
    index_file = os.path.join(dist_path, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return RedirectResponse(url="/docs")

@app.get("/{full_path:path}")
async def catch_all(full_path: str):
    # Do not intercept API or docs routes
    if full_path.startswith("api/") or full_path.startswith("docs") or full_path.startswith("redoc") or full_path.startswith("openapi.json"):
        return JSONResponse(status_code=404, content={"error": "not_found", "detail": f"Path /{full_path} not found"})
    
    index_file = os.path.join(dist_path, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return RedirectResponse(url="/docs")


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
