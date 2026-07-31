"""
config/settings.py
==================
Centralised application settings for the Indian F&O Trading Agent.

All configuration is read from environment variables or a ``.env`` file via
Pydantic's ``BaseSettings``.  The module exposes a cached ``get_settings()``
function so that every part of the application shares a single ``Settings``
instance – keeping environment parsing to a one-time cost.

Usage
-----
    from config.settings import get_settings

    settings = get_settings()
    print(settings.BROKER)

``.env`` file example
---------------------
    TRADING_MODE=PAPER
    BROKER=angel_one
    ANGEL_ONE_API_KEY=abc123
    ANGEL_ONE_CLIENT_ID=A12345
    ANGEL_ONE_PASSWORD=secret
    ANGEL_ONE_TOTP_SECRET=BASE32SECRET
    TELEGRAM_BOT_TOKEN=123456:AABBccdd
    TELEGRAM_CHAT_ID=987654321
    GEMINI_API_KEY=AIzaSy...

"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.enums import TradingMode


# ---------------------------------------------------------------------------
# Settings model
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    """
    Application-wide configuration for the F&O trading agent.

    Fields are grouped by concern.  Every field has a default where a sensible
    one exists; secrets (API keys, passwords) have no default so that
    deployment environments must explicitly provide them.
    """

    model_config = SettingsConfigDict(
        env_file=(".env", str(Path(__file__).resolve().parent.parent / ".env")),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------

    APP_NAME: str = Field(
        default="Indian F&O Trading Agent",
        description="Human-readable application name shown in logs and Telegram notifications.",
    )
    VERSION: str = Field(
        default="0.1.0",
        description="Semantic version string of the running agent.",
    )
    DEBUG: bool = Field(
        default=False,
        description="Enable debug mode – verbose logging, no real orders in debug+paper.",
    )
    TRADING_MODE: TradingMode = Field(
        default=TradingMode.PAPER,
        description=(
            "Operational mode: PAPER (simulate orders), LIVE (real orders), "
            "SHADOW (observe only, no paper trades)."
        ),
    )
    LOG_LEVEL: str = Field(
        default="INFO",
        description="Python logging level: DEBUG, INFO, WARNING, ERROR, CRITICAL.",
    )

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------

    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://fnoagent:changeme@localhost:5432/fnoagent",
        description=(
            "SQLAlchemy async connection URL for the primary relational database. "
            "Use PostgreSQL in production: postgresql+asyncpg://user:pass@host/db"
        ),
    )
    REDIS_URL: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL used for caching, pub/sub, and idempotency keys.",
    )
    TIMESCALE_URL: Optional[str] = Field(
        default=None,
        description=(
            "TimescaleDB connection URL for storing time-series OHLCV / tick data. "
            "If None, the agent falls back to DATABASE_URL."
        ),
    )

    # ------------------------------------------------------------------
    # Broker
    # ------------------------------------------------------------------

    BROKER: str = Field(
        default="angel_one",
        description=(
            "Active broker adapter to use. Supported values: "
            "'angel_one', 'dhan', 'groww'."
        ),
    )

    # Angel One
    ANGEL_ONE_API_KEY: Optional[str] = Field(
        default=None,
        description="Angel One SmartAPI API key.",
    )
    ANGEL_ONE_CLIENT_ID: Optional[str] = Field(
        default=None,
        description="Angel One client / login ID.",
    )
    ANGEL_ONE_PASSWORD: Optional[str] = Field(
        default=None,
        description="Angel One trading password (kept in env, never hardcoded).",
    )
    ANGEL_ONE_TOTP_SECRET: Optional[str] = Field(
        default=None,
        description="Base-32 TOTP secret for Angel One 2FA login.",
    )

    # Dhan
    DHAN_ACCESS_TOKEN: Optional[str] = Field(
        default=None,
        description="Dhan API access token.",
    )
    DHAN_CLIENT_ID: Optional[str] = Field(
        default=None,
        description="Dhan client ID.",
    )

    # Delta Exchange (Crypto Futures & Options)
    DELTA_API_KEY: Optional[str] = Field(
        default=None,
        description="Delta Exchange API Key.",
    )
    DELTA_API_SECRET: Optional[str] = Field(
        default=None,
        description="Delta Exchange API Secret.",
    )
    DELTA_ENV: str = Field(
        default="paper",
        description="Delta Exchange environment: 'paper' (Testnet) or 'live' (Production).",
    )

    # Groww
    GROWW_ACCESS_TOKEN: Optional[str] = Field(
        default=None,
        description="Groww API access token.",
    )

    # ------------------------------------------------------------------
    # Telegram
    # ------------------------------------------------------------------

    TELEGRAM_BOT_TOKEN: Optional[str] = Field(
        default=None,
        description="Telegram Bot API token from @BotFather.",
    )
    TELEGRAM_CHAT_ID: Optional[str] = Field(
        default=None,
        description=(
            "Telegram chat/channel ID where signals and alerts are sent. "
            "Use a negative value for groups/channels."
        ),
    )
    TELEGRAM_APPROVAL_TIMEOUT_MINUTES: int = Field(
        default=15,
        ge=1,
        le=60,
        description=(
            "Minutes to wait for a user to approve/reject a signal via Telegram "
            "before the signal is automatically marked EXPIRED."
        ),
    )

    # ------------------------------------------------------------------
    # Google Gemini (AI layer)
    # ------------------------------------------------------------------

    GEMINI_API_KEY: Optional[str] = Field(
        default=None,
        description="Google Gemini API key for AI-powered signal analysis and NLP.",
    )

    # ------------------------------------------------------------------
    # News / event ingestion
    # ------------------------------------------------------------------

    NEWS_API_KEY: Optional[str] = Field(
        default=None,
        description="API key for the news data provider (e.g. newsapi.org).",
    )
    NEWS_FETCH_INTERVAL_MINUTES: int = Field(
        default=15,
        ge=1,
        description="How often (in minutes) the news ingestion service polls for new articles.",
    )

    # ------------------------------------------------------------------
    # Risk management
    # ------------------------------------------------------------------

    MAX_RISK_PER_TRADE_PCT: float = Field(
        default=1.0,
        ge=0.1,
        le=10.0,
        description=(
            "Maximum risk per trade as a percentage of total account capital. "
            "E.g. 1.0 → risk no more than 1% of capital on a single trade."
        ),
    )
    MAX_DAILY_LOSS_PCT: float = Field(
        default=3.0,
        ge=0.5,
        le=20.0,
        description=(
            "Kill-switch threshold: if the daily loss exceeds this percentage of "
            "account capital, the kill-switch is activated."
        ),
    )
    MAX_OPEN_POSITIONS: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum number of open positions the agent may hold simultaneously.",
    )
    MAX_EXPOSURE_PER_SYMBOL_PCT: float = Field(
        default=20.0,
        ge=1.0,
        le=100.0,
        description=(
            "Maximum capital exposure to a single symbol as a percentage of total capital. "
            "Prevents over-concentration in one name."
        ),
    )
    SLIPPAGE_PCT: float = Field(
        default=0.1,
        ge=0.0,
        le=2.0,
        description=(
            "Assumed slippage percentage applied to entry prices during risk calculations "
            "and position sizing (conservative assumption)."
        ),
    )
    MIN_LIQUIDITY_VOLUME: int = Field(
        default=1000,
        ge=1,
        description=(
            "Minimum traded volume (in lots or shares) required for a signal to pass the "
            "liquidity filter.  Signals on illiquid instruments are rejected."
        ),
    )
    MAX_CONSECUTIVE_LOSSES: int = Field(
        default=3,
        ge=1,
        le=10,
        description=(
            "Number of consecutive losing trades that triggers the kill-switch. "
            "Set to a high value to effectively disable this rule."
        ),
    )

    # ------------------------------------------------------------------
    # Scheduler / timing
    # ------------------------------------------------------------------

    MARKET_OPEN_TIME: str = Field(
        default="09:15",
        description="Market open time in 'HH:MM' (IST).  NSE/BSE equity opens at 09:15.",
    )
    MARKET_CLOSE_TIME: str = Field(
        default="15:30",
        description="Market close time in 'HH:MM' (IST).  NSE/BSE equity closes at 15:30.",
    )
    SIGNAL_SCAN_INTERVAL_SECONDS: int = Field(
        default=60,
        ge=5,
        description=(
            "Interval in seconds between strategy scan cycles.  Lower values give faster "
            "signal detection but increase CPU and API load."
        ),
    )

    # ------------------------------------------------------------------
    # File-system paths
    # ------------------------------------------------------------------

    DATA_DIR: Path = Field(
        default=Path("data"),
        description="Root directory for local market data files (OHLCV CSVs, instrument master, etc.).",
    )
    LOG_DIR: Path = Field(
        default=Path("logs"),
        description="Directory where log files are written.",
    )
    BACKTEST_DIR: Path = Field(
        default=Path("backtests"),
        description="Directory where backtest results and reports are stored.",
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("LOG_LEVEL")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        """Ensure LOG_LEVEL is a valid Python logging level name."""
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid_levels:
            raise ValueError(
                f"LOG_LEVEL must be one of {valid_levels}, got {v!r}."
            )
        return upper

    @field_validator("MARKET_OPEN_TIME", "MARKET_CLOSE_TIME")
    @classmethod
    def _validate_time_format(cls, v: str) -> str:
        """Ensure market time strings are in 'HH:MM' format."""
        parts = v.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError(
                f"Time value {v!r} must be in 'HH:MM' format (e.g. '09:15')."
            )
        hh, mm = int(parts[0]), int(parts[1])
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError(
                f"Time value {v!r} is out of range (hours 0–23, minutes 0–59)."
            )
        return v

    # ------------------------------------------------------------------
    # Convenience methods
    # ------------------------------------------------------------------

    def is_live_mode(self) -> bool:
        """Return True if the agent is configured to place real broker orders."""
        return TradingMode(self.TRADING_MODE) == TradingMode.LIVE

    def is_paper_mode(self) -> bool:
        """Return True if the agent is running in paper-trading (simulation) mode."""
        return TradingMode(self.TRADING_MODE) == TradingMode.PAPER

    def is_shadow_mode(self) -> bool:
        """Return True if the agent is in shadow (observe-only) mode."""
        return TradingMode(self.TRADING_MODE) == TradingMode.SHADOW

    def get_broker_config(self) -> dict[str, Any]:
        """
        Return a dictionary of credentials / config for the active broker.

        The returned dict is passed to the broker adapter factory so that it
        can initialise the correct API client.

        Returns
        -------
        dict
            Keys depend on ``BROKER``:
            - ``angel_one``: api_key, client_id, password, totp_secret
            - ``dhan``: access_token, client_id
            - ``groww``: access_token
        """
        broker = self.BROKER.lower()

        if broker == "angel_one":
            return {
                "broker": broker,
                "api_key": self.ANGEL_ONE_API_KEY,
                "client_id": self.ANGEL_ONE_CLIENT_ID,
                "password": self.ANGEL_ONE_PASSWORD,
                "totp_secret": self.ANGEL_ONE_TOTP_SECRET,
            }
        elif broker == "dhan":
            return {
                "broker": broker,
                "access_token": self.DHAN_ACCESS_TOKEN,
                "client_id": self.DHAN_CLIENT_ID,
            }
        elif broker == "groww":
            return {
                "broker": broker,
                "access_token": self.GROWW_ACCESS_TOKEN,
            }
        else:
            # Return a minimal config for unknown/custom brokers so that the
            # factory can raise a more specific error rather than crashing here.
            return {"broker": broker}

    def ensure_dirs(self) -> None:
        """
        Create DATA_DIR, LOG_DIR, and BACKTEST_DIR if they do not already exist.

        Call this once during application startup before any file I/O.
        """
        for directory in (self.DATA_DIR, self.LOG_DIR, self.BACKTEST_DIR):
            directory.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Cached accessor
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the application-wide ``Settings`` singleton.

    The result is cached via ``@lru_cache`` so that environment variables and
    the ``.env`` file are parsed only once per process.  In tests, call
    ``get_settings.cache_clear()`` before constructing a fresh ``Settings``
    with overridden env vars.

    Example::

        from config.settings import get_settings
        settings = get_settings()
    """
    return Settings()
