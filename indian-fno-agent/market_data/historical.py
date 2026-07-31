"""
market_data/historical.py
─────────────────────────
HistoricalDataService — fetches OHLCV candle data from a broker adapter,
caches results in Redis, falls back to TimescaleDB, and stores new candles
back to the DB.

Dependencies (install via pip):
    redis[asyncio]  asyncpg  pytz
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional, Protocol

import asyncpg
import pytz
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

IST = pytz.timezone("Asia/Kolkata")

# NSE normal market session (IST)
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 15
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 30

VALID_TIMEFRAMES = ("1m", "3m", "5m", "10m", "15m", "30m", "1h", "1d", "1w")

# Redis TTL constants (seconds)
REDIS_TTL_INTRADAY = 60 * 10       # 10 minutes for intraday candles
REDIS_TTL_EOD = 60 * 60 * 24 * 7  # 7 days for daily/weekly candles

# ─────────────────────────────────────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Candle:
    """Represents a single OHLCV candlestick."""

    symbol: str
    exchange: str
    timeframe: str
    timestamp: datetime          # UTC-aware datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    oi: int = 0                  # Open Interest (relevant for F&O)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Candle":
        data = dict(data)
        data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        return cls(**data)


# ─────────────────────────────────────────────────────────────────────────────
# Broker Adapter Protocol
# ─────────────────────────────────────────────────────────────────────────────

class BrokerAdapter(Protocol):
    """Structural protocol — any broker adapter must satisfy this interface."""

    async def get_historical_candles(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        from_dt: datetime,
        to_dt: datetime,
    ) -> List[Candle]:
        """Return OHLCV candles for the given symbol/timeframe range."""
        ...


# ─────────────────────────────────────────────────────────────────────────────
# NSE Market Hours Helpers
# ─────────────────────────────────────────────────────────────────────────────

def is_market_open(dt: Optional[datetime] = None) -> bool:
    """
    Returns True if the given datetime (or *now* if None) falls within
    normal NSE trading hours: 09:15 - 15:30 IST, Monday-Friday.
    Does NOT account for exchange holidays.
    """
    if dt is None:
        dt = datetime.now(tz=IST)
    elif dt.tzinfo is None:
        dt = IST.localize(dt)
    else:
        dt = dt.astimezone(IST)

    # Weekends
    if dt.weekday() >= 5:
        return False

    market_open = dt.replace(
        hour=MARKET_OPEN_HOUR,
        minute=MARKET_OPEN_MINUTE,
        second=0,
        microsecond=0,
    )
    market_close = dt.replace(
        hour=MARKET_CLOSE_HOUR,
        minute=MARKET_CLOSE_MINUTE,
        second=0,
        microsecond=0,
    )
    return market_open <= dt <= market_close


def next_market_open() -> datetime:
    """Returns the next NSE market open time (IST-aware)."""
    now = datetime.now(tz=IST)
    candidate = now.replace(
        hour=MARKET_OPEN_HOUR,
        minute=MARKET_OPEN_MINUTE,
        second=0,
        microsecond=0,
    )
    days_ahead = 0
    while True:
        check = candidate + timedelta(days=days_ahead)
        if check > now and check.weekday() < 5:
            return check
        days_ahead += 1


def _redis_ttl_for_timeframe(timeframe: str) -> int:
    """Choose appropriate Redis TTL based on candle timeframe."""
    if timeframe in ("1d", "1w"):
        return REDIS_TTL_EOD
    return REDIS_TTL_INTRADAY


# ─────────────────────────────────────────────────────────────────────────────
# Redis Cache Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _redis_key(symbol: str, timeframe: str, dt: date) -> str:
    """Canonical Redis key: candles:{symbol}:{timeframe}:{YYYY-MM-DD}"""
    return f"candles:{symbol}:{timeframe}:{dt.isoformat()}"


# ─────────────────────────────────────────────────────────────────────────────
# Main Service
# ─────────────────────────────────────────────────────────────────────────────

class HistoricalDataService:
    """
    Fetches and stores OHLCV historical data.

    Architecture:
        Broker (live)  ->  Redis cache  ->  TimescaleDB (persistent store)

    Fallback chain for fetch_ohlcv:
        1. Try broker adapter (real-time / most accurate)
        2. On failure, fall back to Redis cache
        3. On cache miss, fall back to TimescaleDB

    Usage::

        service = HistoricalDataService(
            broker=my_broker_adapter,
            redis_url="redis://localhost:6379/0",
            pg_dsn="postgresql://user:pass@localhost:5432/trading",
        )
        await service.connect()
        candles = await service.fetch_ohlcv("NIFTY", "NSE", "5m", lookback_days=5)
        await service.disconnect()
    """

    def __init__(
        self,
        broker: BrokerAdapter,
        redis_url: str = "redis://localhost:6379/0",
        pg_dsn: str = "postgresql://user:password@localhost:5432/trading",
        pg_min_connections: int = 2,
        pg_max_connections: int = 10,
    ) -> None:
        self._broker = broker
        self._redis_url = redis_url
        self._pg_dsn = pg_dsn
        self._pg_min = pg_min_connections
        self._pg_max = pg_max_connections

        # Initialised in connect()
        self._redis: Optional[aioredis.Redis] = None
        self._pg_pool: Optional[asyncpg.Pool] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Establish Redis and PostgreSQL connection pools."""
        logger.info("Connecting HistoricalDataService ...")
        self._redis = aioredis.from_url(
            self._redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        self._pg_pool = await asyncpg.create_pool(
            dsn=self._pg_dsn,
            min_size=self._pg_min,
            max_size=self._pg_max,
            command_timeout=30,
        )
        await self._ensure_schema()
        logger.info("HistoricalDataService connected.")

    async def disconnect(self) -> None:
        """Cleanly close all connections."""
        if self._redis:
            await self._redis.aclose()
            self._redis = None
        if self._pg_pool:
            await self._pg_pool.close()
            self._pg_pool = None
        logger.info("HistoricalDataService disconnected.")

    @asynccontextmanager
    async def _pg_conn(self) -> AsyncGenerator[asyncpg.Connection, None]:
        """Async context manager that acquires a connection from the pool."""
        if not self._pg_pool:
            raise RuntimeError("PostgreSQL pool not initialised. Call connect() first.")
        async with self._pg_pool.acquire() as conn:
            yield conn

    # ── Schema Bootstrap ──────────────────────────────────────────────────────

    async def _ensure_schema(self) -> None:
        """
        Creates the candles hypertable (TimescaleDB) if it does not exist.
        Falls back gracefully if TimescaleDB extension is unavailable.
        """
        create_table_sql = """
            CREATE TABLE IF NOT EXISTS candles (
                symbol      TEXT        NOT NULL,
                exchange    TEXT        NOT NULL,
                timeframe   TEXT        NOT NULL,
                ts          TIMESTAMPTZ NOT NULL,
                open        DOUBLE PRECISION NOT NULL,
                high        DOUBLE PRECISION NOT NULL,
                low         DOUBLE PRECISION NOT NULL,
                close       DOUBLE PRECISION NOT NULL,
                volume      BIGINT      NOT NULL,
                oi          BIGINT      NOT NULL DEFAULT 0,
                PRIMARY KEY (symbol, exchange, timeframe, ts)
            );
        """
        create_hypertable_sql = """
            SELECT create_hypertable(
                'candles', 'ts',
                if_not_exists => TRUE,
                migrate_data  => TRUE
            );
        """
        async with self._pg_conn() as conn:
            await conn.execute(create_table_sql)
            try:
                await conn.execute(create_hypertable_sql)
                logger.info("TimescaleDB hypertable ensured for 'candles'.")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Could not create TimescaleDB hypertable (extension may be missing): %s",
                    exc,
                )

    # ── Redis Helpers ─────────────────────────────────────────────────────────

    async def _cache_candles(
        self, candles: List[Candle], symbol: str, timeframe: str
    ) -> None:
        """Serialise and cache candles grouped by IST date into Redis."""
        if not self._redis or not candles:
            return

        # Group candles by the IST date of their timestamp
        by_date: Dict[date, List[Candle]] = {}
        for c in candles:
            ist_date = c.timestamp.astimezone(IST).date()
            by_date.setdefault(ist_date, []).append(c)

        ttl = _redis_ttl_for_timeframe(timeframe)
        pipe = self._redis.pipeline(transaction=False)
        for day, day_candles in by_date.items():
            key = _redis_key(symbol, timeframe, day)
            pipe.set(key, json.dumps([c.to_dict() for c in day_candles]), ex=ttl)

        await pipe.execute()
        logger.debug(
            "Cached %d candles for %s/%s across %d date keys.",
            len(candles), symbol, timeframe, len(by_date),
        )

    async def _load_candles_from_cache(
        self,
        symbol: str,
        timeframe: str,
        dates: List[date],
    ) -> List[Candle]:
        """Load candles from Redis for a list of calendar dates."""
        if not self._redis:
            return []

        keys = [_redis_key(symbol, timeframe, d) for d in dates]
        values = await self._redis.mget(*keys)

        candles: List[Candle] = []
        for raw in values:
            if raw:
                try:
                    candles.extend(
                        Candle.from_dict(item) for item in json.loads(raw)
                    )
                except (json.JSONDecodeError, KeyError, TypeError) as exc:
                    logger.warning("Corrupt Redis cache entry skipped: %s", exc)

        return sorted(candles, key=lambda c: c.timestamp)

    # ── DB Helpers ────────────────────────────────────────────────────────────

    async def _load_candles_from_db(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        from_dt: datetime,
        to_dt: datetime,
    ) -> List[Candle]:
        """Fetch candles from TimescaleDB for a given time window."""
        sql = """
            SELECT symbol, exchange, timeframe, ts, open, high, low, close, volume, oi
            FROM   candles
            WHERE  symbol    = $1
              AND  exchange  = $2
              AND  timeframe = $3
              AND  ts        BETWEEN $4 AND $5
            ORDER  BY ts ASC;
        """
        async with self._pg_conn() as conn:
            rows = await conn.fetch(sql, symbol, exchange, timeframe, from_dt, to_dt)

        return [
            Candle(
                symbol=r["symbol"],
                exchange=r["exchange"],
                timeframe=r["timeframe"],
                timestamp=r["ts"],
                open=r["open"],
                high=r["high"],
                low=r["low"],
                close=r["close"],
                volume=r["volume"],
                oi=r["oi"],
            )
            for r in rows
        ]

    # ── Public API ────────────────────────────────────────────────────────────

    async def fetch_ohlcv(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        lookback_days: int = 30,
    ) -> List[Candle]:
        """
        Fetch OHLCV candles with a 3-tier priority:
          1. Broker adapter (freshest data)
          2. Redis cache   (fast, recent)
          3. TimescaleDB   (persistent fallback)

        Args:
            symbol:        Trading symbol (e.g. "NIFTY23NOV18000CE")
            exchange:      Exchange segment (e.g. "NSE", "NFO", "MCX")
            timeframe:     Candle width ("1m", "5m", "15m", "1h", "1d")
            lookback_days: How many calendar days back to fetch

        Returns:
            List[Candle] sorted ascending by timestamp.
        """
        if timeframe not in VALID_TIMEFRAMES:
            raise ValueError(
                f"Invalid timeframe '{timeframe}'. Valid: {VALID_TIMEFRAMES}"
            )

        to_dt = datetime.now(tz=timezone.utc)
        from_dt = to_dt - timedelta(days=lookback_days)

        # ── Tier 1: Broker ────────────────────────────────────────────────────
        try:
            candles = await self._broker.get_historical_candles(
                symbol=symbol,
                exchange=exchange,
                timeframe=timeframe,
                from_dt=from_dt,
                to_dt=to_dt,
            )
            if candles:
                logger.debug(
                    "Fetched %d candles from broker for %s/%s.",
                    len(candles), symbol, timeframe,
                )
                # Background-persist to cache + DB without blocking caller
                asyncio.create_task(self._cache_candles(candles, symbol, timeframe))
                asyncio.create_task(self.store_candles(candles, symbol, timeframe))
                return candles
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Broker fetch failed for %s/%s: %s. Falling back to cache.",
                symbol, timeframe, exc,
            )

        # ── Tier 2: Redis Cache ───────────────────────────────────────────────
        date_range = [
            (from_dt.astimezone(IST).date() + timedelta(days=i))
            for i in range(lookback_days + 1)
        ]
        cached = await self._load_candles_from_cache(symbol, timeframe, date_range)
        if cached:
            logger.info(
                "Returning %d candles from Redis cache for %s/%s.",
                len(cached), symbol, timeframe,
            )
            return cached

        # ── Tier 3: TimescaleDB ───────────────────────────────────────────────
        logger.info(
            "Cache miss - loading %s/%s from TimescaleDB.", symbol, timeframe
        )
        db_candles = await self._load_candles_from_db(
            symbol, exchange, timeframe, from_dt, to_dt
        )
        if db_candles:
            asyncio.create_task(self._cache_candles(db_candles, symbol, timeframe))
        return db_candles

    async def get_multi_timeframe(
        self,
        symbol: str,
        exchange: str,
        timeframes: Optional[List[str]] = None,
        lookback_days: int = 30,
    ) -> Dict[str, List[Candle]]:
        """
        Fetch candles for multiple timeframes concurrently.

        Args:
            symbol:        Trading symbol.
            exchange:      Exchange segment.
            timeframes:    List of timeframe strings. Defaults to
                           ['1m','5m','15m','1h','1d'].
            lookback_days: Lookback period in calendar days.

        Returns:
            Dict mapping timeframe string to list of Candle objects.
        """
        if timeframes is None:
            timeframes = ["1m", "5m", "15m", "1h", "1d"]

        tasks = {
            tf: asyncio.create_task(
                self.fetch_ohlcv(symbol, exchange, tf, lookback_days)
            )
            for tf in timeframes
        }

        results: Dict[str, List[Candle]] = {}
        for tf, task in tasks.items():
            try:
                results[tf] = await task
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Failed to fetch %s candles for %s: %s", tf, symbol, exc
                )
                results[tf] = []

        return results

    async def store_candles(
        self,
        candles: List[Candle],
        symbol: str,       # kept for API clarity; value already in Candle
        timeframe: str,    # kept for API clarity; value already in Candle
    ) -> int:
        """
        Upsert candles into TimescaleDB (ON CONFLICT DO NOTHING).

        Args:
            candles:   Candle objects to persist.
            symbol:    Symbol name (used only for logging).
            timeframe: Timeframe (used only for logging).

        Returns:
            Number of rows inserted.
        """
        if not candles:
            return 0

        insert_sql = """
            INSERT INTO candles
                (symbol, exchange, timeframe, ts, open, high, low, close, volume, oi)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (symbol, exchange, timeframe, ts) DO NOTHING;
        """
        records = [
            (
                c.symbol, c.exchange, c.timeframe,
                c.timestamp, c.open, c.high, c.low, c.close, c.volume, c.oi,
            )
            for c in candles
        ]

        async with self._pg_conn() as conn:
            await conn.executemany(insert_sql, records)

        logger.debug(
            "Stored/upserted %d candles for %s/%s into TimescaleDB.",
            len(candles), symbol, timeframe,
        )
        return len(records)

    # ── Market Hours Utilities ────────────────────────────────────────────────

    @staticmethod
    def is_market_open(dt: Optional[datetime] = None) -> bool:
        """Proxy to module-level is_market_open() for convenience."""
        return is_market_open(dt)

    @staticmethod
    def next_market_open() -> datetime:
        """Proxy to module-level next_market_open()."""
        return next_market_open()
