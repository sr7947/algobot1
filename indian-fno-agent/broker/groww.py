"""
broker/groww.py
===============
Groww broker adapter – placeholder implementation.

Groww Developer API Reference: https://developers.groww.in/

When implemented this adapter will support
------------------------------------------
- OAuth 2.0 based authentication (access token via ``/v1/auth/token``)
- Order placement, modification, and cancellation (equity & F&O)
- Real-time WebSocket market data feed
- Historical OHLCV candle data
- Option chain data retrieval
- Portfolio: positions, holdings, order book, trade book
- Fund summary / margin information
- GTT (Good Till Triggered) order support
- SIP and recurring order management (equity)
- Mutual fund order placement (equity segment only)
- Intraday square-off
- Brokerage / charges calculator

SDK / dependency
----------------
No official Python SDK is available yet. The adapter will use ``httpx``
(async) for REST calls and ``websockets`` for the real-time feed:
    pip install httpx websockets

Configuration keys expected in ``settings``
-------------------------------------------
* ``GROWW_CLIENT_ID``     – OAuth application client ID
* ``GROWW_CLIENT_SECRET`` – OAuth application client secret
* ``GROWW_ACCESS_TOKEN``  – Bearer token obtained via Groww developer portal
* ``GROWW_USER_ID``       – Registered Groww user ID
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Optional

from broker.base import IBrokerAdapter
from core.enums import Exchange, InstrumentType
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

_NOT_IMPLEMENTED_MSG = (
    "Groww adapter is not yet implemented. "
    "Configure Angel One or use Paper trading mode."
)


class GrowwBroker(IBrokerAdapter):
    """Placeholder adapter for the Groww brokerage platform.

    .. note::
        Every method in this class raises ``NotImplementedError``.
        This stub is provided so that the ``BrokerFactory`` can resolve
        ``"groww"`` without an ``ImportError``, and to document the intended
        contract when the adapter is built out.

    Parameters
    ----------
    settings : Any
        Application settings (not consumed yet).
    """

    broker_name: str = "groww"

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        logger.warning(
            "GrowwBroker is a placeholder. All method calls will raise NotImplementedError."
        )

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def login(self) -> bool:
        """Authenticate with Groww using an OAuth 2.0 access token.

        When implemented, this will:
        1. Exchange the configured ``GROWW_ACCESS_TOKEN`` for a session token
           via ``POST /v1/auth/token``.
        2. Validate the token by calling the user-profile endpoint.
        3. Cache the token and its expiry for subsequent API calls.
        4. Start a background coroutine to refresh the token before expiry.

        Returns
        -------
        bool
            ``True`` on success.

        Raises
        ------
        NotImplementedError
            Always, until implemented.
        """
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    async def logout(self) -> bool:
        """Revoke the Groww OAuth token and close connections.

        When implemented, this will:
        1. Call ``POST /v1/auth/revoke`` to invalidate the access token.
        2. Close the WebSocket market-data feed.
        3. Clear cached tokens and session state.

        Returns
        -------
        bool
            ``True`` on success.

        Raises
        ------
        NotImplementedError
            Always, until implemented.
        """
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def is_connected(self) -> bool:
        """Return whether a valid Groww session is active.

        When implemented, this will inspect the cached access token and its
        expiry timestamp without making a network call.

        Returns
        -------
        bool
            ``True`` if connected.

        Raises
        ------
        NotImplementedError
            Always, until implemented.
        """
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    # ------------------------------------------------------------------
    # Market data - instruments
    # ------------------------------------------------------------------

    async def get_instruments(self, exchange: Exchange) -> list[Instrument]:
        """Download the Groww instrument master and parse into ``Instrument`` objects.

        When implemented, this will:
        1. Fetch the Groww scrip master JSON/CSV from the developer API.
        2. Filter by ``exchange``.
        3. Convert each record to an ``Instrument`` model.
        4. Cache the result in memory for the session.

        Parameters
        ----------
        exchange : Exchange
            Target exchange (``NSE``, ``NFO``, ``BSE``).

        Returns
        -------
        list[Instrument]
            All instruments on the exchange.

        Raises
        ------
        NotImplementedError
            Always, until implemented.
        """
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    async def get_ltp(
        self,
        tokens: list[str],
        exchange: Exchange,
    ) -> dict[str, float]:
        """Fetch Last Traded Prices via Groww's market quote endpoint.

        When implemented, this will call the Groww market-data API with a
        list of symbol IDs and return a ``{token: ltp}`` mapping.

        Parameters
        ----------
        tokens : list[str]
            Groww-specific instrument / segment IDs.
        exchange : Exchange
            Exchange for the tokens.

        Returns
        -------
        dict[str, float]
            LTP per token.

        Raises
        ------
        NotImplementedError
            Always, until implemented.
        """
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    async def get_ohlc(
        self,
        symbol: str,
        exchange: Exchange,
        timeframe: str,
        from_dt: datetime,
        to_dt: datetime,
    ) -> list[Candle]:
        """Fetch historical OHLCV candles from Groww's historical data API.

        When implemented, this will:
        - Use the Groww historical data endpoint (exact path TBD from docs).
        - Map timeframe strings to Groww interval codes.
        - Parse the response into ``Candle`` objects.

        Parameters
        ----------
        symbol : str
            Groww symbol / segment ID.
        exchange : Exchange
            Exchange for the symbol.
        timeframe : str
            ``"1m"``, ``"5m"``, ``"15m"``, ``"1h"``, ``"1d"``.
        from_dt : datetime
            Range start.
        to_dt : datetime
            Range end.

        Returns
        -------
        list[Candle]
            OHLCV candles, oldest first.

        Raises
        ------
        NotImplementedError
            Always, until implemented.
        """
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    async def get_option_chain(
        self,
        underlying: str,
        expiry: date,
        exchange: Exchange = Exchange.NFO,
    ) -> Optional[OptionChain]:
        """Fetch option chain data from Groww's API.

        When implemented, this will call the Groww option chain endpoint and
        map the response to the ``OptionChain`` model including OI, volume,
        bid/ask, and Greeks if available from the API.

        Parameters
        ----------
        underlying : str
            Underlying symbol (e.g. ``"NIFTY"``, ``"BANKNIFTY"``).
        expiry : date
            Option expiry date.
        exchange : Exchange
            Derivatives exchange.

        Returns
        -------
        OptionChain | None
            Populated option chain.

        Raises
        ------
        NotImplementedError
            Always, until implemented.
        """
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        """Place an order via Groww's order management API.

        When implemented, this will:
        1. Map ``OrderRequest`` fields to Groww's order-placement request body.
        2. Handle order types: LIMIT, MARKET, SL, SL-M, GTT.
        3. Call ``POST /v1/orders`` (exact path TBD from docs).
        4. Return the Groww-assigned order ID in an ``OrderResponse``.

        Parameters
        ----------
        order : OrderRequest
            Internal order request model.

        Returns
        -------
        OrderResponse
            Contains the Groww-assigned order ID.

        Raises
        ------
        NotImplementedError
            Always, until implemented.
        """
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    async def modify_order(
        self,
        broker_order_id: str,
        modifications: dict,
    ) -> OrderResponse:
        """Modify an open Groww order.

        When implemented, modifications will include price, trigger price, and
        quantity fields, mapped to Groww's modification API schema.

        Parameters
        ----------
        broker_order_id : str
            Groww order ID.
        modifications : dict
            Fields to update (``price``, ``trigger_price``, ``quantity``).

        Returns
        -------
        OrderResponse
            Updated order state.

        Raises
        ------
        NotImplementedError
            Always, until implemented.
        """
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    async def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel an open Groww order.

        When implemented, this will call the Groww order cancellation endpoint
        and return ``True`` if the cancellation was accepted.

        Parameters
        ----------
        broker_order_id : str
            Groww order ID.

        Returns
        -------
        bool
            ``True`` if cancelled.

        Raises
        ------
        NotImplementedError
            Always, until implemented.
        """
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    # ------------------------------------------------------------------
    # Account / portfolio
    # ------------------------------------------------------------------

    async def get_positions(self) -> list[Position]:
        """Fetch open positions from Groww's positions API.

        When implemented, this will call the Groww positions endpoint and
        map each entry to a ``Position`` model.

        Returns
        -------
        list[Position]
            Open positions including day and net quantities.

        Raises
        ------
        NotImplementedError
            Always, until implemented.
        """
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    async def get_orders(self) -> list[dict]:
        """Fetch the raw order book from Groww's orders API.

        Returns
        -------
        list[dict]
            Raw Groww order entries.

        Raises
        ------
        NotImplementedError
            Always, until implemented.
        """
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    async def get_trade_book(self) -> list[Trade]:
        """Fetch the executed trade book from Groww's trades API.

        Returns
        -------
        list[Trade]
            Today's executed trades mapped to ``Trade`` models.

        Raises
        ------
        NotImplementedError
            Always, until implemented.
        """
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    async def get_margins(self) -> MarginInfo:
        """Fetch fund and margin summary from Groww's funds API.

        When implemented, this will call the Groww funds/balance endpoint and
        optionally the margin-calculator endpoint for precise F&O margins.

        Returns
        -------
        MarginInfo
            Margin and fund breakdown.

        Raises
        ------
        NotImplementedError
            Always, until implemented.
        """
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    async def search_instrument(
        self,
        query: str,
        exchange: Exchange,
    ) -> list[Instrument]:
        """Search the Groww instrument master by symbol name substring.

        When implemented, this will load the cached instrument master and
        return instruments whose symbol or name contains *query* (case-insensitive).

        Parameters
        ----------
        query : str
            Substring to search.
        exchange : Exchange
            Exchange to limit results to.

        Returns
        -------
        list[Instrument]
            Matching instruments.

        Raises
        ------
        NotImplementedError
            Always, until implemented.
        """
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)
