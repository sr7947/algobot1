"""
core/events.py
==============
Async in-process event bus for the Indian F&O Trading Agent.

Architecture
------------
The ``EventBus`` implements a lightweight publish/subscribe pattern built on
``asyncio``.  All handlers are coroutines – synchronous callables are NOT
supported by design, because the agent is async-first throughout.

Usage example::

    bus = EventBus.get_singleton()

    async def on_order_filled(event: Event) -> None:
        print(f"Order filled: {event.payload}")

    bus.subscribe(EventType.ORDER_FILLED, on_order_filled)

    await bus.publish(Event(
        event_type=EventType.ORDER_FILLED,
        payload={"broker_order_id": "ORD123", "fill_price": 19500.0},
        source="broker_adapter",
    ))

Thread-safety
-------------
This implementation is single-threaded (runs in one asyncio event loop).
If the agent ever adopts a multi-loop model, the singleton pattern and the
internal handler dict would need to be made thread-safe (e.g. with asyncio
locks).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Coroutine, Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event type enumeration
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    """
    All event types that can be published on the ``EventBus``.

    Consumers subscribe to one or more of these types; the bus fans out each
    published ``Event`` only to handlers registered for that specific type.
    """

    # Signal lifecycle
    SIGNAL_READY        = "SIGNAL_READY"         # A new trade signal is ready for risk checks
    # Order lifecycle
    ORDER_PLACED        = "ORDER_PLACED"          # Order has been submitted to the broker
    ORDER_FILLED        = "ORDER_FILLED"          # Broker confirmed the order is fully filled
    ORDER_REJECTED      = "ORDER_REJECTED"        # Broker or exchange rejected the order
    # Position lifecycle
    POSITION_UPDATED    = "POSITION_UPDATED"      # Position mark-to-market has been refreshed
    SL_HIT              = "SL_HIT"               # Stop-loss level has been hit
    TARGET_HIT          = "TARGET_HIT"           # Profit target has been reached
    # System control
    KILL_SWITCH         = "KILL_SWITCH"          # Kill-switch activated or deactivated
    # Data feeds
    MARKET_DATA_UPDATE  = "MARKET_DATA_UPDATE"   # New tick / candle data is available
    NEWS_UPDATE         = "NEWS_UPDATE"          # A new news event has been ingested and processed


# ---------------------------------------------------------------------------
# Event dataclass
# ---------------------------------------------------------------------------

# Type alias for an async event handler coroutine
AsyncHandler = Callable[["Event"], Coroutine[Any, Any, None]]


@dataclass
class Event:
    """
    An immutable event payload published to the ``EventBus``.

    Attributes
    ----------
    event_type:
        The type of event; determines which subscribers receive it.
    payload:
        Arbitrary dictionary carrying event data.  Subscribers should
        document which keys they expect for each ``EventType``.
    source:
        Human-readable identifier of the component that published this event
        (e.g. ``"strategy.supertrend"``, ``"broker_adapter.angel_one"``).
    timestamp:
        UTC timestamp set automatically at creation time.
    """

    event_type: EventType
    payload: dict[str, Any]
    source: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------

class EventBus:
    """
    Async publish/subscribe event bus.

    All handler invocations are fire-and-forget coroutines wrapped in
    ``asyncio.create_task``.  Exceptions raised inside handlers are caught and
    logged so that one misbehaving handler cannot prevent others from running.

    Singleton
    ---------
    Use ``EventBus.get_singleton()`` to obtain the shared application-wide
    instance.  Direct instantiation is still supported for testing.
    """

    _instance: EventBus | None = None

    def __init__(self) -> None:
        # Maps EventType -> list of registered async handlers
        self._handlers: dict[EventType, list[AsyncHandler]] = {
            event_type: [] for event_type in EventType
        }
        # Counts total events published (for diagnostics)
        self._publish_count: int = 0

    # ------------------------------------------------------------------
    # Singleton access
    # ------------------------------------------------------------------

    @classmethod
    def get_singleton(cls) -> "EventBus":
        """
        Return the application-wide singleton ``EventBus`` instance.

        Creates the instance on first call (lazy initialisation).
        """
        if cls._instance is None:
            cls._instance = cls()
            logger.debug("EventBus singleton created.")
        return cls._instance

    @classmethod
    def reset_singleton(cls) -> None:
        """
        Destroy the singleton instance.

        Intended for use in unit tests only – do not call in production code.
        """
        cls._instance = None
        logger.debug("EventBus singleton reset.")

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------

    def subscribe(self, event_type: EventType, handler: AsyncHandler) -> None:
        """
        Register an async handler for a specific event type.

        Parameters
        ----------
        event_type:
            The ``EventType`` to subscribe to.
        handler:
            An async callable with the signature ``async def handler(event: Event) -> None``.
            The same handler can be registered for multiple event types by
            calling ``subscribe`` once for each type.

        Raises
        ------
        TypeError
            If ``handler`` is not a coroutine function.
        ValueError
            If ``handler`` is already registered for ``event_type`` (guards
            against accidental double-registration).
        """
        if not asyncio.iscoroutinefunction(handler):
            raise TypeError(
                f"Handler {handler!r} must be an async coroutine function. "
                "Synchronous handlers are not supported."
            )
        handlers = self._handlers[event_type]
        if handler in handlers:
            raise ValueError(
                f"Handler {handler!r} is already subscribed to {event_type.value}."
            )
        handlers.append(handler)
        logger.debug(
            "Subscribed %s to %s (total handlers: %d)",
            getattr(handler, "__qualname__", repr(handler)),
            event_type.value,
            len(handlers),
        )

    def unsubscribe(self, event_type: EventType, handler: AsyncHandler) -> None:
        """
        Remove a previously registered handler for a specific event type.

        Silently ignores the call if the handler was not registered, so that
        teardown code does not need to track whether a subscription was made.

        Parameters
        ----------
        event_type:
            The ``EventType`` to unsubscribe from.
        handler:
            The handler coroutine to remove.
        """
        handlers = self._handlers[event_type]
        try:
            handlers.remove(handler)
            logger.debug(
                "Unsubscribed %s from %s (remaining handlers: %d)",
                getattr(handler, "__qualname__", repr(handler)),
                event_type.value,
                len(handlers),
            )
        except ValueError:
            logger.debug(
                "unsubscribe called for %s on %s but handler was not registered – ignoring.",
                getattr(handler, "__qualname__", repr(handler)),
                event_type.value,
            )

    def subscriber_count(self, event_type: EventType) -> int:
        """Return the number of handlers registered for ``event_type``."""
        return len(self._handlers[event_type])

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    async def publish(self, event: Event) -> None:
        """
        Fan out ``event`` to all handlers registered for its ``event_type``.

        Each handler is scheduled as an independent ``asyncio.Task`` so that:

        1. A slow handler does not block others.
        2. An exception in one handler is caught and logged without affecting
           the remaining handlers.

        Parameters
        ----------
        event:
            The ``Event`` to publish.  The bus does not mutate the event.

        Notes
        -----
        Because tasks are created and immediately awaited via
        ``asyncio.gather``, this coroutine returns only after *all* handlers
        have completed (or raised).  This gives the publisher a clear
        "all handlers done" guarantee, which is important for test assertions.
        If fire-and-forget semantics are ever needed, swap ``gather`` for
        ``create_task``.
        """
        handlers = self._handlers.get(event.event_type, [])
        self._publish_count += 1

        if not handlers:
            logger.debug(
                "EventBus: no subscribers for %s – event discarded.", event.event_type.value
            )
            return

        logger.debug(
            "EventBus: publishing %s from '%s' to %d handler(s).",
            event.event_type.value,
            event.source,
            len(handlers),
        )

        # Create one task per handler so they run concurrently
        tasks = [
            asyncio.ensure_future(self._invoke_handler(handler, event))
            for handler in handlers
        ]
        # Wait for all tasks; return_exceptions=True prevents gather from
        # raising on the first failure – each failure is handled inside
        # _invoke_handler already.
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _invoke_handler(self, handler: AsyncHandler, event: Event) -> None:
        """
        Safely invoke a single handler, catching and logging any exception.

        Parameters
        ----------
        handler:
            The async handler coroutine to invoke.
        event:
            The event to pass to the handler.
        """
        try:
            await handler(event)
        except Exception as exc:  # noqa: BLE001  (intentional broad catch)
            logger.exception(
                "EventBus: unhandled exception in handler %s for event %s: %s",
                getattr(handler, "__qualname__", repr(handler)),
                event.event_type.value,
                exc,
            )

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def publish_count(self) -> int:
        """Total number of events published since this bus was created."""
        return self._publish_count

    def get_subscription_summary(self) -> dict[str, int]:
        """
        Return a mapping of event type name → subscriber count.

        Useful for health-check endpoints and startup logging.
        """
        return {
            event_type.value: len(handlers)
            for event_type, handlers in self._handlers.items()
            if handlers  # Only include types that have at least one subscriber
        }
