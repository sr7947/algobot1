"""
broker/base.py
==============
Abstract broker adapter interface (IBrokerAdapter) and BrokerFactory.

Design Principle
----------------
All execution flows through IBrokerAdapter.  Strategy logic NEVER imports
broker SDKs directly; it only talks to this interface.  This makes it trivial
to swap brokers (Angel One ↔ Dhan ↔ Paper) without touching any strategy code.

Usage
-----
    adapter = BrokerFactory.create("angel_one", settings)
    await adapter.login()
    ltp = await adapter.get_ltp(["NIFTY25JULFIX"], Exchange.NFO)
"""

from __future__ import annotations

import importlib
import logging
from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Optional

from core.enums import Exchange, InstrumentType  # noqa: F401 – re-exported for adapters
from core.models import (
    Candle,
    Instrument,
    MarginInfo,
    OptionChain,
    OrderRequest,
    OrderResponse,
    Position,
    Tick,
    Trade,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract Interface
# ---------------------------------------------------------------------------


class IBrokerAdapter(ABC):
    """Abstract interface for all broker adapters.

    Every concrete broker implementation (AngelOne, Dhan, Paper, etc.) must
    subclass this and implement every abstract method.  The interface is
    intentionally kept at the *trading-domain* level – no SDK types leak out.

    Attributes
    ----------
    broker_name : str
        Short identifier for the broker (e.g. ``"angel_one"``, ``"paper"``).
        Set as a class-level attribute in each concrete subclass.
    """

    broker_name: str  # must be overridden by subclass

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    @abstractmethod
    async def login(self) -> bool:
        """Authenticate with the broker and establish a session.

        Implementations should store session tokens internally so that
        subsequent API calls work without re-authenticating.

        Returns
        -------
        bool
            ``True`` if login succeeded, ``False`` otherwise.
        """

    @abstractmethod
    async def logout(self) -> bool:
        """Terminate the active broker session and clear cached tokens.

        Returns
        -------
        bool
            ``True`` if logout succeeded cleanly.
        """

    @abstractmethod
    def is_connected(self) -> bool:
        """Return whether the adapter currently holds a valid session.

        This is a *synchronous* convenience check (no network call) – it
        inspects the cached token / expiry timestamp.

        Returns
        -------
        bool
            ``True`` if a valid session is active.
        """

    # ------------------------------------------------------------------
    # Market data - instruments
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_instruments(self, exchange: Exchange) -> list[Instrument]:
        """Fetch the full instrument master for a given exchange.

        The master list is typically a large CSV/JSON file published daily by
        the broker.  Implementations should cache it for the trading session.

        Parameters
        ----------
        exchange : Exchange
            The exchange whose instruments to fetch (``NFO``, ``NSE``, ``MCX``).

        Returns
        -------
        list[Instrument]
            All tradeable instruments on the requested exchange.
        """

    @abstractmethod
    async def get_ltp(
        self,
        tokens: list[str],
        exchange: Exchange,
    ) -> dict[str, float]:
        """Return the Last Traded Price for a list of instrument tokens.

        Parameters
        ----------
        tokens : list[str]
            Broker-specific instrument tokens (not trading symbols).
        exchange : Exchange
            The exchange on which the tokens are listed.

        Returns
        -------
        dict[str, float]
            Mapping of ``token -> LTP`` in Indian Rupees.
        """

    @abstractmethod
    async def get_ohlc(
        self,
        symbol: str,
        exchange: Exchange,
        timeframe: str,
        from_dt: datetime,
        to_dt: datetime,
    ) -> list[Candle]:
        """Fetch historical OHLCV candle data.

        Parameters
        ----------
        symbol : str
            Trading symbol (e.g. ``"NIFTY"``).
        exchange : Exchange
            Exchange on which the symbol is listed.
        timeframe : str
            Candle timeframe in compact notation: ``"1m"``, ``"5m"``,
            ``"15m"``, ``"1h"``, ``"1d"``.
        from_dt : datetime
            Start of the requested range (IST, timezone-naive is accepted).
        to_dt : datetime
            End of the requested range (IST, timezone-naive is accepted).

        Returns
        -------
        list[Candle]
            List of OHLCV candles ordered oldest-first.
        """

    @abstractmethod
    async def get_option_chain(
        self,
        underlying: str,
        expiry: date,
        exchange: Exchange = Exchange.NFO,
    ) -> Optional[OptionChain]:
        """Fetch a full option chain snapshot for *underlying* at *expiry*.

        Parameters
        ----------
        underlying : str
            Underlying index / stock symbol (e.g. ``"NIFTY"``, ``"BANKNIFTY"``).
        expiry : date
            Option expiry date.
        exchange : Exchange
            Derivatives exchange, defaults to ``NFO``.

        Returns
        -------
        OptionChain | None
            Populated option chain, or ``None`` if no data is available.
        """

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    @abstractmethod
    async def place_order(self, order: OrderRequest) -> OrderResponse:
        """Submit a new order to the broker.

        Parameters
        ----------
        order : OrderRequest
            Fully populated order request (symbol, qty, side, type, etc.).

        Returns
        -------
        OrderResponse
            Broker acknowledgment including the broker-assigned order ID.

        Raises
        ------
        BrokerOrderError
            If the broker rejects the order (margin breach, invalid symbol).
        """

    @abstractmethod
    async def modify_order(
        self,
        broker_order_id: str,
        modifications: dict,
    ) -> OrderResponse:
        """Modify an existing open order.

        Parameters
        ----------
        broker_order_id : str
            The broker-assigned order ID returned from ``place_order``.
        modifications : dict
            Fields to update, e.g. ``{"price": 105.50, "quantity": 50}``.

        Returns
        -------
        OrderResponse
            Updated order response.
        """

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel an open order by its broker order ID.

        Parameters
        ----------
        broker_order_id : str
            Broker-assigned order ID.

        Returns
        -------
        bool
            ``True`` if cancellation was accepted.
        """

    # ------------------------------------------------------------------
    # Account / portfolio
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Return all open positions for the active trading session.

        Returns
        -------
        list[Position]
            Net and day positions across all segments.
        """

    @abstractmethod
    async def get_orders(self) -> list[dict]:
        """Return the raw order book for today's session.

        Returns a list of raw dicts (broker-native keys) rather than a typed
        model because order fields vary significantly across brokers.  Callers
        should use ``get_positions()`` and ``get_trade_book()`` for typed data.

        Returns
        -------
        list[dict]
            Raw order entries from the broker's order book API.
        """

    @abstractmethod
    async def get_trade_book(self) -> list[Trade]:
        """Return executed trades for the current session.

        Returns
        -------
        list[Trade]
            Executed trade entries ordered by execution time.
        """

    @abstractmethod
    async def get_margins(self) -> MarginInfo:
        """Fetch current account margin / fund information.

        Returns
        -------
        MarginInfo
            Available cash, used margin, net liquidation value, etc.
        """

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @abstractmethod
    async def search_instrument(
        self,
        query: str,
        exchange: Exchange,
    ) -> list[Instrument]:
        """Search the instrument master by a partial name / symbol.

        Parameters
        ----------
        query : str
            Search string (case-insensitive substring match expected).
        exchange : Exchange
            Limit search to this exchange.

        Returns
        -------
        list[Instrument]
            Matching instruments (empty list if none found).
        """

    # ------------------------------------------------------------------
    # Helpers available to all subclasses
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        connected = self.is_connected()
        return f"<{self.__class__.__name__} broker={self.broker_name!r} connected={connected}>"


# ---------------------------------------------------------------------------
# Broker Factory
# ---------------------------------------------------------------------------


class BrokerFactory:
    """Creates the correct IBrokerAdapter subclass based on a broker name.

    Supported broker names
    ----------------------
    * ``"angel_one"``  - Production Angel One SmartAPI adapter.
    * ``"paper"``      - In-memory paper-trading simulator.
    * ``"dhan"``       - Dhan placeholder (raises NotImplementedError).
    * ``"groww"``      - Groww placeholder (raises NotImplementedError).

    Usage
    -----
    ::

        from broker.base import BrokerFactory
        from config.settings import settings

        adapter = BrokerFactory.create("angel_one", settings)
        await adapter.login()
    """

    # Maps broker_name -> (module_path, class_name)
    _REGISTRY: dict[str, tuple[str, str]] = {
        "angel_one": ("broker.angel_one", "AngelOneBroker"),
        "paper":     ("broker.paper",     "PaperBroker"),
        "dhan":      ("broker.dhan",      "DhanBroker"),
        "groww":     ("broker.groww",     "GrowwBroker"),
    }

    @staticmethod
    def create(broker_name: str, settings) -> IBrokerAdapter:  # type: ignore[return]
        """Instantiate and return the adapter for *broker_name*.

        Parameters
        ----------
        broker_name : str
            One of ``"angel_one"``, ``"paper"``, ``"dhan"``, ``"groww"``.
        settings : Any
            The application settings object passed directly to the adapter
            constructor.  Each adapter reads the keys it needs.

        Returns
        -------
        IBrokerAdapter
            A concrete adapter instance (not yet logged in).

        Raises
        ------
        ValueError
            If *broker_name* is not registered.
        ImportError
            If the adapter module cannot be imported (missing dependency).
        """
        key = broker_name.lower().strip()
        if key not in BrokerFactory._REGISTRY:
            available = ", ".join(sorted(BrokerFactory._REGISTRY))
            raise ValueError(
                f"Unknown broker {broker_name!r}. "
                f"Available adapters: {available}"
            )

        module_path, class_name = BrokerFactory._REGISTRY[key]

        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            raise ImportError(
                f"Could not import broker adapter module {module_path!r}. "
                f"Make sure all required SDK packages are installed.\n"
                f"Original error: {exc}"
            ) from exc

        cls = getattr(module, class_name)
        logger.info("BrokerFactory: creating %s adapter", broker_name)
        return cls(settings)  # type: ignore[return-value]

    @staticmethod
    def register(broker_name: str, module_path: str, class_name: str) -> None:
        """Register a custom broker adapter at runtime.

        This is useful for plugins or third-party adapters that are not
        shipped with the core package.

        Parameters
        ----------
        broker_name : str
            Unique identifier (e.g. ``"zerodha"``).
        module_path : str
            Dotted import path (e.g. ``"my_pkg.zerodha_adapter"``).
        class_name : str
            Name of the class inside the module.
        """
        BrokerFactory._REGISTRY[broker_name.lower()] = (module_path, class_name)
        logger.info(
            "BrokerFactory: registered custom adapter %r -> %s.%s",
            broker_name,
            module_path,
            class_name,
        )

    @staticmethod
    def list_adapters() -> list[str]:
        """Return a sorted list of all registered broker names."""
        return sorted(BrokerFactory._REGISTRY.keys())


# Alias for backward compatibility / dependency injection
BrokerBase = IBrokerAdapter

