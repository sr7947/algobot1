"""
core/exceptions.py
==================
Custom exception hierarchy for the Indian F&O Trading Agent.

All exceptions inherit from ``FnoAgentError`` so that callers can catch the
entire family with a single ``except FnoAgentError`` clause, while still being
able to narrow to a specific subtype when handling is different.

Design notes
------------
- Every exception stores a human-readable ``message`` as the first positional
  argument so that ``str(exc)`` always returns something useful in logs.
- Structured fields (e.g. ``reason``, ``rule``) are stored as proper
  attributes – not buried inside the message string – so that upstream
  handlers (audit logger, Telegram notifier) can inspect them programmatically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    # Avoid a circular import at runtime; ``OrderRequest`` is only needed for
    # type annotations in this module.
    from core.models import OrderRequest


# ---------------------------------------------------------------------------
# Base exception
# ---------------------------------------------------------------------------

class FnoAgentError(Exception):
    """
    Base class for all application-specific exceptions.

    Usage::

        raise FnoAgentError("Something went wrong")

    All subclasses should call ``super().__init__(message)`` so that the
    ``args[0]`` contract is honoured.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message: str = message

    def __repr__(self) -> str:  # pragma: no cover
        return f"{self.__class__.__name__}({self.message!r})"


# ---------------------------------------------------------------------------
# Broker / connectivity exceptions
# ---------------------------------------------------------------------------

class BrokerConnectionError(FnoAgentError):
    """
    Raised when the agent cannot establish or maintain a connection to the
    broker API (network error, timeout, server unreachable, etc.).
    """

    def __init__(
        self,
        message: str = "Failed to connect to the broker API.",
        *,
        broker: str = "",
    ) -> None:
        super().__init__(message)
        self.broker: str = broker  # Broker identifier, e.g. 'angel_one'


class BrokerAuthError(FnoAgentError):
    """
    Raised when broker API authentication fails (invalid credentials, expired
    session token, TOTP mismatch, etc.).
    """

    def __init__(
        self,
        message: str = "Broker authentication failed.",
        *,
        broker: str = "",
    ) -> None:
        super().__init__(message)
        self.broker: str = broker


# ---------------------------------------------------------------------------
# Order exceptions
# ---------------------------------------------------------------------------

class OrderPlacementError(FnoAgentError):
    """
    Raised when an order cannot be placed at the broker (validation failure,
    exchange rejection, connectivity issue at order time, etc.).

    Attributes
    ----------
    order_request:
        The ``OrderRequest`` that failed, preserved for the audit logger and
        retry logic.
    """

    def __init__(
        self,
        message: str,
        *,
        order_request: Optional["OrderRequest"] = None,
    ) -> None:
        super().__init__(message)
        self.order_request: Optional["OrderRequest"] = order_request


class DuplicateOrderError(FnoAgentError):
    """
    Raised when the idempotency check detects that an order with the same key
    has already been submitted (guards against double-clicks / retries).

    Attributes
    ----------
    idempotency_key:
        The key that was found to be a duplicate (typically a hash of
        ``signal_id + direction + symbol + quantity``).
    """

    def __init__(
        self,
        message: str,
        *,
        idempotency_key: str,
    ) -> None:
        super().__init__(message)
        self.idempotency_key: str = idempotency_key


# ---------------------------------------------------------------------------
# Risk management exceptions
# ---------------------------------------------------------------------------

class RiskCheckError(FnoAgentError):
    """
    Raised when a trade signal fails one of the risk management rules.

    Attributes
    ----------
    reason:
        A human-readable explanation of *why* the check failed.
    rule:
        The name of the specific risk rule that was violated
        (e.g. ``'MAX_DAILY_LOSS'``, ``'MAX_OPEN_POSITIONS'``).
    """

    def __init__(self, message: str, *, reason: str, rule: str) -> None:
        super().__init__(message)
        self.reason: str = reason
        self.rule: str = rule


class KillSwitchActiveError(FnoAgentError):
    """
    Raised when a new order is attempted while the kill-switch is active.

    The kill-switch is triggered when the agent's daily loss or consecutive
    loss limit is breached.  It must be manually reset by the operator via
    the Telegram admin interface or the management API.
    """

    def __init__(
        self,
        message: str = "Kill-switch is active. No new orders are permitted until it is reset.",
    ) -> None:
        super().__init__(message)


class InsufficientMarginError(FnoAgentError):
    """
    Raised when the available margin / cash in the trading account is
    insufficient to cover the required margin for a new order.

    Attributes
    ----------
    required:
        Margin required for the intended order (INR).
    available:
        Margin currently available in the account (INR).
    """

    def __init__(
        self,
        message: str,
        *,
        required: float = 0.0,
        available: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.required: float = required
        self.available: float = available


# ---------------------------------------------------------------------------
# Signal exceptions
# ---------------------------------------------------------------------------

class SignalExpiredError(FnoAgentError):
    """
    Raised when an attempt is made to execute a ``TradeSignal`` after its
    ``expires_at`` timestamp has passed.

    The signal approval / execution pipeline should discard such signals and
    emit an ``EXPIRED`` audit event rather than letting them reach the broker.
    """

    def __init__(
        self,
        message: str = "Trade signal has expired and cannot be executed.",
    ) -> None:
        super().__init__(message)


# ---------------------------------------------------------------------------
# Strategy / configuration exceptions
# ---------------------------------------------------------------------------

class InvalidStrategyConfigError(FnoAgentError):
    """
    Raised during strategy initialisation when the provided configuration
    dictionary is missing required fields or contains invalid values.

    Attributes
    ----------
    strategy_name:
        Name of the strategy that failed to initialise.
    """

    def __init__(
        self,
        message: str,
        *,
        strategy_name: str = "",
    ) -> None:
        super().__init__(message)
        self.strategy_name: str = strategy_name


# ---------------------------------------------------------------------------
# Market data exceptions
# ---------------------------------------------------------------------------

class MarketDataError(FnoAgentError):
    """
    Raised when market data cannot be fetched, parsed, or is stale beyond the
    acceptable threshold.

    This covers failures from the broker WebSocket feed, REST APIs, and any
    third-party data providers.
    """

    def __init__(
        self,
        message: str = "Market data is unavailable or invalid.",
        *,
        symbol: str = "",
    ) -> None:
        super().__init__(message)
        self.symbol: str = symbol  # Affected symbol, if known
