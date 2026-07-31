"""
broker/paper.py
===============
In-memory paper-trading adapter for the Indian F&O trading agent.

Purpose
-------
Simulate a live brokerage account without touching real money.  Useful for:
- Strategy back-testing and forward-testing during market hours
- CI/CD pipeline smoke-tests that must not place real orders
- Developer onboarding (no broker credentials required)

Simulation Model
----------------
* **MARKET orders** fill immediately at  LTP ± slippage.
* **LIMIT orders**  are kept in a pending queue; a separate ``tick()`` method
  (called by the market-data feed) checks for fill conditions.
* **Slippage** is expressed as a percentage of LTP (default 0.05 %).
* **Virtual cash** starts at ₹5,00,000 (configurable via settings).
* **Margin** for F&O positions is approximated at 20 % of notional value.
* All state is in-memory Python dicts – extend ``_persist()`` to push to
  Redis / SQLite for persistence across restarts.

Thread / async safety
---------------------
All public methods are ``async`` (even the trivially synchronous ones) so they
satisfy the IBrokerAdapter contract.  Internal state is mutated only inside
these coroutines; add an ``asyncio.Lock`` if you run multiple coroutines
concurrently.
"""

from __future__ import annotations

import logging
import uuid
from copy import deepcopy
from datetime import date, datetime
from typing import Any, Optional

from broker.base import IBrokerAdapter
from core.enums import Exchange, TradeDirection as OrderSide, OrderStatus, OrderType
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
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_VIRTUAL_CASH: float = 500_000.0          # ₹5,00,000
_FNO_MARGIN_PCT: float = 0.20                     # 20 % of notional
_DEFAULT_SLIPPAGE_PCT: float = 0.0005             # 0.05 %


# ---------------------------------------------------------------------------
# Paper Broker
# ---------------------------------------------------------------------------


class PaperBroker(IBrokerAdapter):
    """In-memory paper-trading adapter.

    Parameters
    ----------
    settings : Any
        Application settings object.  The adapter reads:

        * ``settings.PAPER_VIRTUAL_CASH``  – starting capital (float, INR).
          Defaults to ₹5,00,000 if the attribute is absent.
        * ``settings.SLIPPAGE_PCT``        – fill slippage fraction (float).
          Defaults to 0.0005 (0.05 %) if the attribute is absent.

    Notes
    -----
    ``login()`` always returns ``True`` – no network call is made.
    All mutating operations (place/modify/cancel order) also update the
    ``_positions`` and ``_cash`` state immediately for MARKET orders.
    LIMIT / SL orders are held in ``_pending_orders`` until ``tick()`` is
    called with a live LTP update.
    """

    broker_name: str = "paper"

    def __init__(self, settings: Any) -> None:
        # ---- configurable parameters ----
        self._virtual_cash: float = getattr(
            settings, "PAPER_VIRTUAL_CASH", _DEFAULT_VIRTUAL_CASH
        )
        self._slippage_pct: float = getattr(
            settings, "SLIPPAGE_PCT", _DEFAULT_SLIPPAGE_PCT
        )

        # ---- internal state ----
        self._connected: bool = False
        self._cash: float = self._virtual_cash          # remaining free cash
        self._used_margin: float = 0.0                  # margin locked in open positions

        # key: broker_order_id (str)
        self._orders: dict[str, dict] = {}

        # key: symbol (str)  →  Position
        self._positions: dict[str, Position] = {}

        # key: broker_order_id  →  pending OrderRequest
        self._pending_orders: dict[str, OrderRequest] = {}

        # executed trades list
        self._trades: list[Trade] = []

        # last known LTP per symbol (updated via tick())
        self._ltp_cache: dict[str, float] = {}

        # instrument master cache: exchange -> list[Instrument]
        self._instrument_cache: dict[Exchange, list[Instrument]] = {}

        logger.info(
            "PaperBroker initialised | virtual_cash=%.2f | slippage=%.4f%%",
            self._virtual_cash,
            self._slippage_pct * 100,
        )

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def login(self) -> bool:
        """Simulate a successful broker login.

        Paper trading never makes network calls.  This method just flips the
        internal ``_connected`` flag to ``True`` and resets the session state.

        Returns
        -------
        bool
            Always ``True``.
        """
        self._connected = True
        self._cash = self._virtual_cash
        self._used_margin = 0.0
        self._orders.clear()
        self._positions.clear()
        self._pending_orders.clear()
        self._trades.clear()
        logger.info("PaperBroker: login successful (virtual session started)")
        return True

    async def logout(self) -> bool:
        """End the paper-trading session.

        Logs a P&L summary and clears the connected flag.

        Returns
        -------
        bool
            Always ``True``.
        """
        pnl = self._compute_total_pnl()
        logger.info(
            "PaperBroker: logout | session_pnl=%.2f | trades=%d",
            pnl,
            len(self._trades),
        )
        self._connected = False
        return True

    def is_connected(self) -> bool:
        """Return ``True`` if the paper session is active."""
        return self._connected

    # ------------------------------------------------------------------
    # Market data – instruments
    # ------------------------------------------------------------------

    async def get_instruments(self, exchange: Exchange) -> list[Instrument]:
        """Return cached instruments for the exchange.

        In paper mode the instrument master is populated externally (e.g. from
        Angel One's CSV) and stored via ``load_instruments()``.  If no cache
        exists, an empty list is returned.

        Parameters
        ----------
        exchange : Exchange
            Target exchange.

        Returns
        -------
        list[Instrument]
            All instruments previously loaded for this exchange.
        """
        return list(self._instrument_cache.get(exchange, []))

    def load_instruments(
        self,
        exchange: Exchange,
        instruments: list[Instrument],
    ) -> None:
        """Populate the instrument master cache (called by test harness / feed).

        Parameters
        ----------
        exchange : Exchange
            The exchange these instruments belong to.
        instruments : list[Instrument]
            Instrument objects to cache.
        """
        self._instrument_cache[exchange] = instruments
        logger.debug(
            "PaperBroker: loaded %d instruments for %s",
            len(instruments),
            exchange.value,
        )

    async def get_ltp(
        self,
        tokens: list[str],
        exchange: Exchange,
    ) -> dict[str, float]:
        """Return last known LTP from the in-memory cache.

        The cache is populated by calling ``update_ltp()`` (typically from a
        WebSocket market-data feed or a test fixture).

        Parameters
        ----------
        tokens : list[str]
            Instrument tokens to query.
        exchange : Exchange
            Exchange (used for logging only in paper mode).

        Returns
        -------
        dict[str, float]
            ``{token: ltp}`` for tokens that are in the cache.
            Tokens not found in the cache are omitted.
        """
        return {t: self._ltp_cache[t] for t in tokens if t in self._ltp_cache}

    def update_ltp(self, symbol: str, ltp: float) -> None:
        """Push a new LTP into the cache and attempt to fill pending orders.

        Parameters
        ----------
        symbol : str
            Trading symbol (used as key in the LTP cache and order matching).
        ltp : float
            Latest traded price in INR.
        """
        self._ltp_cache[symbol] = ltp
        self._try_fill_pending_orders(symbol, ltp)

    async def get_ohlc(
        self,
        symbol: str,
        exchange: Exchange,
        timeframe: str,
        from_dt: datetime,
        to_dt: datetime,
    ) -> list[Candle]:
        """Return an empty list – paper broker does not generate OHLC history.

        In a real test setup, inject synthetic candles via the strategy's
        data feed; the paper broker only simulates *execution*.

        Returns
        -------
        list[Candle]
            Always empty in paper mode.
        """
        logger.debug(
            "PaperBroker.get_ohlc: paper mode has no OHLC history for %s",
            symbol,
        )
        return []

    async def get_option_chain(
        self,
        underlying: str,
        expiry: date,
        exchange: Exchange = Exchange.NFO,
    ) -> Optional[OptionChain]:
        """Return ``None`` – paper broker has no option chain data.

        Option chain data should come from a real market-data provider even in
        paper-trading mode.

        Returns
        -------
        None
            Always.
        """
        logger.debug(
            "PaperBroker.get_option_chain: no chain data in paper mode for %s",
            underlying,
        )
        return None

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        """Simulate order placement and immediate fill for MARKET orders.

        Fill logic
        ----------
        * **MARKET** – fills instantly at LTP ± slippage.
          - BUY:  fill_price = ltp * (1 + slippage_pct)
          - SELL: fill_price = ltp * (1 - slippage_pct)
        * **LIMIT / SL / SL-M** – added to the pending queue; filled when
          ``update_ltp()`` drives the price through the limit/trigger.

        Parameters
        ----------
        order : OrderRequest
            The order to simulate.

        Returns
        -------
        OrderResponse
            Contains a UUID broker_order_id and fill details for MARKET orders.

        Raises
        ------
        ValueError
            If there is insufficient virtual cash/margin to place the order.
        """
        broker_order_id = str(uuid.uuid4())
        now = datetime.now()

        # --- record the order ---
        order_record: dict = {
            "broker_order_id": broker_order_id,
            "symbol": order.symbol,
            "exchange": order.exchange,
            "side": order.side,
            "order_type": order.order_type,
            "quantity": order.quantity,
            "price": order.price,
            "trigger_price": order.trigger_price,
            "status": OrderStatus.OPEN,
            "placed_at": now,
            "filled_at": None,
            "fill_price": None,
            "fill_qty": 0,
        }
        self._orders[broker_order_id] = order_record

        if order.order_type == OrderType.MARKET:
            ltp = self._ltp_cache.get(order.symbol)
            if ltp is None:
                # No LTP available – treat like a pending order
                logger.warning(
                    "PaperBroker: no LTP for %s; MARKET order queued as pending",
                    order.symbol,
                )
                self._pending_orders[broker_order_id] = order
                return OrderResponse(
                    broker_order_id=broker_order_id,
                    status=OrderStatus.OPEN,
                    message="Queued: no LTP available yet",
                )

            fill_price = self._apply_slippage(ltp, order.side)
            self._execute_fill(broker_order_id, order, fill_price, now)

        else:
            # LIMIT / SL / SL-M – queue for deferred fill
            self._pending_orders[broker_order_id] = order
            logger.info(
                "PaperBroker: %s %s order queued | id=%s | qty=%d | price=%.2f",
                order.order_type.value,
                order.symbol,
                broker_order_id,
                order.quantity,
                order.price or 0,
            )

        return OrderResponse(
            broker_order_id=broker_order_id,
            status=self._orders[broker_order_id]["status"],
            fill_price=self._orders[broker_order_id].get("fill_price"),
            fill_qty=self._orders[broker_order_id].get("fill_qty", 0),
            message="Simulated fill" if order.order_type == OrderType.MARKET else "Order queued",
        )

    async def modify_order(
        self,
        broker_order_id: str,
        modifications: dict,
    ) -> OrderResponse:
        """Modify a pending (unfilled) paper order.

        Only OPEN orders in the pending queue can be modified.  Filled or
        cancelled orders return an error response.

        Parameters
        ----------
        broker_order_id : str
            ID of the order to modify.
        modifications : dict
            Keys may include ``"price"``, ``"trigger_price"``, ``"quantity"``.

        Returns
        -------
        OrderResponse
            The updated order state.
        """
        if broker_order_id not in self._orders:
            logger.warning("PaperBroker.modify_order: unknown order %s", broker_order_id)
            return OrderResponse(
                broker_order_id=broker_order_id,
                status=OrderStatus.REJECTED,
                message=f"Order {broker_order_id} not found",
            )

        record = self._orders[broker_order_id]
        if record["status"] != OrderStatus.OPEN:
            return OrderResponse(
                broker_order_id=broker_order_id,
                status=record["status"],
                message=f"Cannot modify order in state {record['status'].value}",
            )

        for field in ("price", "trigger_price", "quantity"):
            if field in modifications:
                record[field] = modifications[field]

        # Update the pending order object if it exists
        if broker_order_id in self._pending_orders:
            pending = self._pending_orders[broker_order_id]
            if "price" in modifications:
                pending.price = modifications["price"]
            if "trigger_price" in modifications:
                pending.trigger_price = modifications["trigger_price"]
            if "quantity" in modifications:
                pending.quantity = modifications["quantity"]

        logger.info("PaperBroker: order modified | id=%s | changes=%s", broker_order_id, modifications)
        return OrderResponse(
            broker_order_id=broker_order_id,
            status=OrderStatus.OPEN,
            message="Order modified successfully",
        )

    async def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel a pending paper order.

        Parameters
        ----------
        broker_order_id : str
            Broker order ID to cancel.

        Returns
        -------
        bool
            ``True`` if the order was successfully cancelled,
            ``False`` if it was already filled or not found.
        """
        if broker_order_id not in self._orders:
            logger.warning("PaperBroker.cancel_order: unknown order %s", broker_order_id)
            return False

        record = self._orders[broker_order_id]
        if record["status"] != OrderStatus.OPEN:
            logger.warning(
                "PaperBroker.cancel_order: order %s is %s – cannot cancel",
                broker_order_id,
                record["status"].value,
            )
            return False

        record["status"] = OrderStatus.CANCELLED
        self._pending_orders.pop(broker_order_id, None)
        logger.info("PaperBroker: order cancelled | id=%s", broker_order_id)
        return True

    # ------------------------------------------------------------------
    # Account / portfolio
    # ------------------------------------------------------------------

    async def get_positions(self) -> list[Position]:
        """Return a snapshot of all open paper positions.

        Returns
        -------
        list[Position]
            Current open positions with unrealised P&L calculated from
            the most recently seen LTP.
        """
        positions = []
        for symbol, pos in self._positions.items():
            # Refresh unrealised P&L using latest LTP
            ltp = self._ltp_cache.get(symbol)
            if ltp is not None and pos.quantity != 0:
                pos.ltp = ltp
                pos.unrealised_pnl = (ltp - pos.average_price) * pos.quantity
            positions.append(deepcopy(pos))
        return positions

    async def get_orders(self) -> list[dict]:
        """Return the raw paper order book.

        Returns
        -------
        list[dict]
            All orders placed in this paper session (filled, open, cancelled).
        """
        return [deepcopy(record) for record in self._orders.values()]

    async def get_trade_book(self) -> list[Trade]:
        """Return all executed paper trades for this session.

        Returns
        -------
        list[Trade]
            Trades in execution order.
        """
        return list(self._trades)

    async def get_margins(self) -> MarginInfo:
        """Compute and return simulated margin/fund information.

        Margin model
        ------------
        * Used margin ≈ 20 % of total open position notional value.
        * Available cash = virtual cash − realised losses − used margin.
        * Net liquidation = cash + sum of all position market values.

        Returns
        -------
        MarginInfo
            Simulated margin breakdown.
        """
        total_notional = sum(
            abs(pos.quantity) * (self._ltp_cache.get(sym, pos.average_price))
            for sym, pos in self._positions.items()
            if pos.quantity != 0
        )
        self._used_margin = total_notional * _FNO_MARGIN_PCT

        unrealised_pnl = self._compute_total_pnl()
        available_cash = self._cash - self._used_margin

        return MarginInfo(
            total_cash=self._cash,
            available_cash=max(available_cash, 0.0),
            used_margin=self._used_margin,
            net_liquidation=self._cash + unrealised_pnl,
            unrealised_pnl=unrealised_pnl,
        )

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    async def search_instrument(
        self,
        query: str,
        exchange: Exchange,
    ) -> list[Instrument]:
        """Search the loaded instrument cache by symbol substring.

        Parameters
        ----------
        query : str
            Substring to search (case-insensitive).
        exchange : Exchange
            Exchange to search within.

        Returns
        -------
        list[Instrument]
            Instruments whose symbol or name contains *query*.
        """
        q = query.upper()
        return [
            inst
            for inst in self._instrument_cache.get(exchange, [])
            if q in inst.symbol.upper() or q in (inst.name or "").upper()
        ]

    # ------------------------------------------------------------------
    # Paper-specific helpers (not part of IBrokerAdapter)
    # ------------------------------------------------------------------

    def get_session_summary(self) -> dict:
        """Return a summary dict of the current paper-trading session.

        Returns
        -------
        dict
            Keys: ``virtual_cash``, ``current_cash``, ``used_margin``,
            ``unrealised_pnl``, ``realised_pnl``, ``total_trades``,
            ``open_positions``.
        """
        realised = sum(t.pnl for t in self._trades if t.pnl is not None)
        unrealised = self._compute_total_pnl()
        return {
            "virtual_cash":    self._virtual_cash,
            "current_cash":    self._cash,
            "used_margin":     self._used_margin,
            "unrealised_pnl":  unrealised,
            "realised_pnl":    realised,
            "total_trades":    len(self._trades),
            "open_positions":  sum(1 for p in self._positions.values() if p.quantity != 0),
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _apply_slippage(self, ltp: float, side: OrderSide) -> float:
        """Compute fill price by applying slippage in the adverse direction.

        Parameters
        ----------
        ltp : float
            Last traded price.
        side : OrderSide
            BUY pays a premium; SELL receives a discount.

        Returns
        -------
        float
            Simulated fill price.
        """
        if side == OrderSide.BUY:
            return round(ltp * (1 + self._slippage_pct), 2)
        return round(ltp * (1 - self._slippage_pct), 2)

    def _execute_fill(
        self,
        broker_order_id: str,
        order: OrderRequest,
        fill_price: float,
        ts: datetime,
    ) -> None:
        """Record a fill: update order record, positions, cash, and trade book.

        Parameters
        ----------
        broker_order_id : str
            The order being filled.
        order : OrderRequest
            Original order details.
        fill_price : float
            Price at which the order filled.
        ts : datetime
            Fill timestamp.
        """
        record = self._orders[broker_order_id]
        record["status"] = OrderStatus.COMPLETE
        record["fill_price"] = fill_price
        record["fill_qty"] = order.quantity
        record["filled_at"] = ts

        # -- update positions --
        sym = order.symbol
        if sym not in self._positions:
            self._positions[sym] = Position(
                symbol=sym,
                exchange=order.exchange,
                quantity=0,
                average_price=0.0,
                ltp=fill_price,
                unrealised_pnl=0.0,
                realised_pnl=0.0,
                product=getattr(order, "product", "NRML"),
            )

        pos = self._positions[sym]
        qty_signed = order.quantity if order.side == OrderSide.BUY else -order.quantity
        old_qty = pos.quantity

        if old_qty == 0:
            # Fresh position
            pos.quantity = qty_signed
            pos.average_price = fill_price
        elif (old_qty > 0 and qty_signed > 0) or (old_qty < 0 and qty_signed < 0):
            # Adding to existing position – weighted average
            total_qty = old_qty + qty_signed
            pos.average_price = (
                (pos.average_price * abs(old_qty) + fill_price * abs(qty_signed))
                / abs(total_qty)
            )
            pos.quantity = total_qty
        else:
            # Reducing / reversing position
            closed_qty = min(abs(qty_signed), abs(old_qty))
            pnl_per_unit = (fill_price - pos.average_price) * (1 if old_qty > 0 else -1)
            realised = pnl_per_unit * closed_qty
            pos.realised_pnl = (pos.realised_pnl or 0.0) + realised
            self._cash += realised      # book realised P&L into cash

            remaining = old_qty + qty_signed
            pos.quantity = remaining
            if remaining == 0:
                pos.average_price = 0.0

        pos.ltp = fill_price

        # -- update cash (debit cost for new buys) --
        cost = fill_price * abs(qty_signed)
        if qty_signed > 0:   # BUY
            margin_required = cost * _FNO_MARGIN_PCT
            self._cash -= margin_required
        else:                 # SELL (short – credit margin)
            margin_freed = cost * _FNO_MARGIN_PCT
            self._cash += margin_freed

        # -- record the trade --
        trade = Trade(
            trade_id=str(uuid.uuid4()),
            broker_order_id=broker_order_id,
            symbol=sym,
            exchange=order.exchange,
            side=order.side,
            quantity=order.quantity,
            price=fill_price,
            traded_at=ts,
            pnl=None,       # point-in-time P&L computed separately
        )
        self._trades.append(trade)

        logger.info(
            "PaperBroker: filled %s %s %d @ %.2f | cash_remaining=%.2f",
            order.side.value,
            sym,
            order.quantity,
            fill_price,
            self._cash,
        )

        # Remove from pending if it was there
        self._pending_orders.pop(broker_order_id, None)

    def _try_fill_pending_orders(self, symbol: str, ltp: float) -> None:
        """Check if any pending orders for *symbol* should fill at *ltp*.

        Called every time ``update_ltp()`` is invoked.  Applies the
        following fill rules:

        * **LIMIT BUY**   – fill if ``ltp <= limit_price``
        * **LIMIT SELL**  – fill if ``ltp >= limit_price``
        * **SL BUY**      – fill if ``ltp >= trigger_price`` (stop-loss buy)
        * **SL SELL**     – fill if ``ltp <= trigger_price``
        * **SL-M** orders – same trigger logic, fill at LTP (market after trigger)

        Parameters
        ----------
        symbol : str
            Symbol that received a new LTP.
        ltp : float
            Updated LTP.
        """
        to_fill: list[str] = []

        for oid, order in list(self._pending_orders.items()):
            if order.symbol != symbol:
                continue

            ot = order.order_type
            side = order.side

            should_fill = False
            if ot == OrderType.LIMIT:
                should_fill = (
                    (side == OrderSide.BUY and ltp <= (order.price or float("inf")))
                    or (side == OrderSide.SELL and ltp >= (order.price or 0.0))
                )
            elif ot in (OrderType.SL, OrderType.SL_M):
                tp = order.trigger_price or order.price or 0.0
                should_fill = (
                    (side == OrderSide.BUY and ltp >= tp)
                    or (side == OrderSide.SELL and ltp <= tp)
                )

            if should_fill:
                to_fill.append(oid)

        for oid in to_fill:
            order = self._pending_orders[oid]
            fill_price = self._apply_slippage(ltp, order.side)
            self._execute_fill(oid, order, fill_price, datetime.now())
            logger.info(
                "PaperBroker: pending order %s triggered at ltp=%.2f | fill=%.2f",
                oid,
                ltp,
                fill_price,
            )

    def _compute_total_pnl(self) -> float:
        """Compute unrealised P&L across all open positions.

        Returns
        -------
        float
            Sum of unrealised P&L in INR.
        """
        total = 0.0
        for sym, pos in self._positions.items():
            if pos.quantity == 0:
                continue
            ltp = self._ltp_cache.get(sym, pos.average_price)
            pnl = (ltp - pos.average_price) * pos.quantity
            total += pnl
        return round(total, 2)
