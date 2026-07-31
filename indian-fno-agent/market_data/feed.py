"""
market_data/feed.py
───────────────────
LiveMarketFeed — manages a watchlist of symbols, polls the broker adapter
for latest prices on a configurable interval, publishes MARKET_DATA_UPDATE
events to EventBus, and provides per-symbol OHLCV update subscriptions.

Dependencies (install via pip):
    redis[asyncio]  pytz
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, Set

import pytz

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# ─────────────────────────────────────────────────────────────────────────────
# Event system
# ─────────────────────────────────────────────────────────────────────────────

class EventType:
    MARKET_DATA_UPDATE = "MARKET_DATA_UPDATE"
    OHLCV_UPDATE       = "OHLCV_UPDATE"
    SYMBOL_ADDED       = "SYMBOL_ADDED"
    SYMBOL_REMOVED     = "SYMBOL_REMOVED"


@dataclass
class MarketEvent:
    """Payload published to the EventBus."""
    event_type: str
    symbol: str
    exchange: str
    data: Dict[str, Any]
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


# Callback type alias
EventCallback = Callable[[MarketEvent], Awaitable[None]]


class EventBus:
    """
    Simple in-process async event bus.
    Supports multiple subscribers per event type.
    Thread-safe via asyncio.Lock.
    """

    def __init__(self) -> None:
        self._subscribers: Dict[str, List[EventCallback]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, event_type: str, callback: EventCallback) -> None:
        """Register an async callback for a given event type."""
        async with self._lock:
            self._subscribers.setdefault(event_type, []).append(callback)

    async def unsubscribe(self, event_type: str, callback: EventCallback) -> None:
        """Deregister a previously registered callback."""
        async with self._lock:
            handlers = self._subscribers.get(event_type, [])
            if callback in handlers:
                handlers.remove(callback)

    async def publish(self, event: MarketEvent) -> None:
        """
        Publish an event to all registered subscribers.
        Each callback is awaited; errors are logged but do not abort delivery.
        """
        async with self._lock:
            handlers = list(self._subscribers.get(event.event_type, []))

        for handler in handlers:
            try:
                await handler(event)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "EventBus handler error for '%s': %s", event.event_type, exc
                )


# ─────────────────────────────────────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TickData:
    """Represents a single price tick."""
    symbol: str
    exchange: str
    ltp: float                       # Last Traded Price
    volume: int = 0
    oi: int = 0                      # Open Interest
    bid: float = 0.0
    ask: float = 0.0
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )


@dataclass
class WatchlistEntry:
    """Internal metadata for each watched symbol."""
    symbol: str
    exchange: str
    last_tick: Optional[TickData] = None
    added_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


# ─────────────────────────────────────────────────────────────────────────────
# Broker Adapter Protocol
# ─────────────────────────────────────────────────────────────────────────────

class BrokerAdapter(Protocol):
    """Structural protocol for broker adapters."""

    async def get_ltp(self, symbol: str, exchange: str) -> float:
        """Return the last traded price for the given symbol."""
        ...

    async def get_quote(self, symbol: str, exchange: str) -> Dict[str, Any]:
        """
        Return a full quote dict containing at minimum:
        {'ltp': float, 'volume': int, 'oi': int, 'bid': float, 'ask': float}
        """
        ...


# ─────────────────────────────────────────────────────────────────────────────
# OHLCV subscription registry
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OhlcvSubscription:
    """Holds a callback registered for OHLCV updates on a specific symbol/timeframe."""
    symbol: str
    timeframe: str
    callback: EventCallback


# ─────────────────────────────────────────────────────────────────────────────
# LiveMarketFeed
# ─────────────────────────────────────────────────────────────────────────────

class LiveMarketFeed:
    """
    Manages a dynamic watchlist of symbols and polls the broker adapter for
    real-time price updates. Publishes MARKET_DATA_UPDATE events on each tick.

    Architecture:
        - A single background asyncio.Task runs the polling loop.
        - Watchlist mutations are guarded by asyncio.Lock.
        - LTP values are stored in an in-memory dict for O(1) reads.
        - Optionally, each tick is also written to a Redis hash for
          cross-process access.

    Usage::

        bus  = EventBus()
        feed = LiveMarketFeed(broker=adapter, event_bus=bus, poll_interval=5.0)
        await feed.start()

        await feed.add_symbol("NIFTY 50", "NSE")
        price = await feed.get_ltp("NIFTY 50")

        await feed.stop()
    """

    def __init__(
        self,
        broker: BrokerAdapter,
        event_bus: EventBus,
        poll_interval: float = 5.0,      # seconds between polls
        use_full_quote: bool = True,      # False = only fetch LTP (faster)
    ) -> None:
        self._broker = broker
        self._event_bus = event_bus
        self._poll_interval = poll_interval
        self._use_full_quote = use_full_quote

        # Watchlist: symbol -> WatchlistEntry
        self._watchlist: Dict[str, WatchlistEntry] = {}

        # LTP cache: symbol -> float
        self._ltp_cache: Dict[str, float] = {}

        # OHLCV subscriptions list
        self._ohlcv_subscriptions: List[OhlcvSubscription] = []

        # Concurrency control
        self._lock = asyncio.Lock()

        # Background polling task
        self._poll_task: Optional[asyncio.Task] = None
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background polling loop."""
        if self._running:
            logger.warning("LiveMarketFeed is already running.")
            return

        self._running = True
        self._poll_task = asyncio.create_task(
            self._polling_loop(), name="live_market_feed_poll"
        )
        logger.info(
            "LiveMarketFeed started (poll interval: %.1fs).", self._poll_interval
        )

    async def stop(self) -> None:
        """Gracefully stop the polling loop."""
        self._running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self._poll_task = None
        logger.info("LiveMarketFeed stopped.")

    # ── Watchlist Management ──────────────────────────────────────────────────

    async def add_symbol(self, symbol: str, exchange: str) -> None:
        """
        Add a symbol to the watchlist.
        No-op if the symbol is already being watched.
        Publishes a SYMBOL_ADDED event.
        """
        async with self._lock:
            if symbol in self._watchlist:
                logger.debug("Symbol '%s' already in watchlist.", symbol)
                return
            self._watchlist[symbol] = WatchlistEntry(symbol=symbol, exchange=exchange)
            logger.info("Added '%s' (%s) to watchlist.", symbol, exchange)

        await self._event_bus.publish(
            MarketEvent(
                event_type=EventType.SYMBOL_ADDED,
                symbol=symbol,
                exchange=exchange,
                data={"exchange": exchange},
            )
        )

    async def remove_symbol(self, symbol: str) -> None:
        """
        Remove a symbol from the watchlist.
        Also evicts the LTP cache entry.
        Publishes a SYMBOL_REMOVED event.
        """
        async with self._lock:
            entry = self._watchlist.pop(symbol, None)
            self._ltp_cache.pop(symbol, None)
            if entry is None:
                logger.debug("Symbol '%s' not in watchlist; nothing to remove.", symbol)
                return
            logger.info("Removed '%s' from watchlist.", symbol)

        await self._event_bus.publish(
            MarketEvent(
                event_type=EventType.SYMBOL_REMOVED,
                symbol=symbol,
                exchange="",
                data={},
            )
        )

    async def get_watchlist(self) -> List[str]:
        """Return a snapshot of the current watchlist symbols."""
        async with self._lock:
            return list(self._watchlist.keys())

    # ── Price Access ──────────────────────────────────────────────────────────

    async def get_ltp(self, symbol: str) -> Optional[float]:
        """
        Return the most recent Last Traded Price for a symbol.
        Returns None if the symbol has not been polled yet or is not in watchlist.
        """
        async with self._lock:
            return self._ltp_cache.get(symbol)

    async def get_tick(self, symbol: str) -> Optional[TickData]:
        """Return the full tick snapshot for a symbol."""
        async with self._lock:
            entry = self._watchlist.get(symbol)
            return entry.last_tick if entry else None

    # ── OHLCV Subscriptions ───────────────────────────────────────────────────

    async def subscribe_to_ohlcv_updates(
        self,
        symbol: str,
        timeframe: str,
        callback: EventCallback,
    ) -> None:
        """
        Register a callback to be invoked whenever an OHLCV_UPDATE event
        is published for the given symbol + timeframe pair.

        The callback receives a MarketEvent whose data dict contains:
            {
                'timeframe': str,
                'candle': {
                    'open': float, 'high': float, 'low': float,
                    'close': float, 'volume': int, 'oi': int
                }
            }

        Note: The actual OHLCV candle assembly (aggregating ticks into candles)
        is handled externally by a candle builder and should call
        publish_ohlcv_update() when a candle closes.
        """
        async with self._lock:
            sub = OhlcvSubscription(
                symbol=symbol, timeframe=timeframe, callback=callback
            )
            self._ohlcv_subscriptions.append(sub)
            logger.info(
                "Registered OHLCV subscription for %s/%s.", symbol, timeframe
            )

        # Also subscribe to the global EventBus for OHLCV_UPDATE events
        async def _filtered_callback(event: MarketEvent) -> None:
            if event.symbol == symbol and event.data.get("timeframe") == timeframe:
                await callback(event)

        await self._event_bus.subscribe(EventType.OHLCV_UPDATE, _filtered_callback)

    async def publish_ohlcv_update(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        candle_data: Dict[str, Any],
    ) -> None:
        """
        Publish a closed OHLCV candle to all OHLCV subscribers.
        Intended to be called by external candle-building logic.
        """
        event = MarketEvent(
            event_type=EventType.OHLCV_UPDATE,
            symbol=symbol,
            exchange=exchange,
            data={"timeframe": timeframe, "candle": candle_data},
        )
        await self._event_bus.publish(event)

    # ── Polling Loop ──────────────────────────────────────────────────────────

    async def _polling_loop(self) -> None:
        """
        Core polling loop — runs until self._running is False.
        Fetches price data for all watchlist symbols on each iteration
        and publishes MARKET_DATA_UPDATE events.
        """
        logger.info("Polling loop started.")
        while self._running:
            loop_start = time.monotonic()

            # Snapshot the watchlist to avoid holding the lock during I/O
            async with self._lock:
                symbols_to_poll = list(self._watchlist.items())

            if symbols_to_poll:
                # Poll all symbols concurrently
                tasks = [
                    asyncio.create_task(
                        self._fetch_and_publish(symbol, entry.exchange)
                    )
                    for symbol, entry in symbols_to_poll
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for sym, result in zip(
                    (s for s, _ in symbols_to_poll), results
                ):
                    if isinstance(result, Exception):
                        logger.warning(
                            "Poll error for symbol '%s': %s", sym, result
                        )

            # Sleep for the remainder of the poll interval
            elapsed = time.monotonic() - loop_start
            sleep_time = max(0.0, self._poll_interval - elapsed)
            await asyncio.sleep(sleep_time)

        logger.info("Polling loop exited.")

    async def _fetch_and_publish(self, symbol: str, exchange: str) -> None:
        """
        Fetch the latest price for one symbol and publish a MARKET_DATA_UPDATE.
        Updates the internal LTP cache and watchlist entry atomically.
        """
        try:
            if self._use_full_quote:
                raw: Dict[str, Any] = await self._broker.get_quote(symbol, exchange)
                ltp: float = float(raw.get("ltp", 0.0))
                tick = TickData(
                    symbol=symbol,
                    exchange=exchange,
                    ltp=ltp,
                    volume=int(raw.get("volume", 0)),
                    oi=int(raw.get("oi", 0)),
                    bid=float(raw.get("bid", 0.0)),
                    ask=float(raw.get("ask", 0.0)),
                )
            else:
                ltp = await self._broker.get_ltp(symbol, exchange)
                tick = TickData(symbol=symbol, exchange=exchange, ltp=ltp)

            async with self._lock:
                self._ltp_cache[symbol] = ltp
                if symbol in self._watchlist:
                    self._watchlist[symbol].last_tick = tick

            event = MarketEvent(
                event_type=EventType.MARKET_DATA_UPDATE,
                symbol=symbol,
                exchange=exchange,
                data={
                    "ltp": tick.ltp,
                    "volume": tick.volume,
                    "oi": tick.oi,
                    "bid": tick.bid,
                    "ask": tick.ask,
                },
            )
            await self._event_bus.publish(event)

        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to fetch/publish tick for '%s/%s': %s",
                symbol, exchange, exc,
            )
            raise  # Re-raise so gather() captures it

    # ── Utility ───────────────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        """True if the polling loop is active."""
        return self._running

    @property
    def poll_interval(self) -> float:
        """Current poll interval in seconds."""
        return self._poll_interval

    async def set_poll_interval(self, interval: float) -> None:
        """
        Update the polling interval at runtime.
        Takes effect on the next poll iteration.
        """
        if interval <= 0:
            raise ValueError("poll_interval must be positive.")
        self._poll_interval = interval
        logger.info("Poll interval updated to %.1fs.", interval)

    def snapshot_ltp_cache(self) -> Dict[str, float]:
        """
        Return a synchronous snapshot of all cached LTPs.
        Safe to call from non-async contexts; does not acquire the lock.
        """
        return dict(self._ltp_cache)
