"""
execution/order_manager.py
--------------------------
Manages the lifecycle of orders in PostgreSQL for the Indian F&O Agent.

Responsibilities:
  - Persisting new order records with signal linkage
  - Updating order status on fill / rejection / partial fills
  - Syncing order state from the broker on a polling cycle
  - Idempotency: guarantees one order record per signal_id

All DB operations use an asyncpg connection pool passed in at construction
time. The schema expected by this module is defined in
migrations/V2__orders.sql.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import asyncpg  # type: ignore

from brokers.base import BrokerAdapter
from core.models import OrderRequest, OrderResponse, TradeSignal
from core.enums import OrderStatus

logger = logging.getLogger(__name__)


class OrderManager:
    """
    Provides CRUD and sync operations for the ``orders`` table.

    Parameters
    ----------
    db_pool:
        An ``asyncpg.Pool`` connected to the trading PostgreSQL database.
    """

    # ------------------------------------------------------------------
    # SQL templates
    # ------------------------------------------------------------------

    _INSERT_ORDER = """
        INSERT INTO orders (
            id,
            signal_id,
            broker_order_id,
            symbol,
            exchange,
            product,
            order_type,
            transaction_type,
            quantity,
            filled_quantity,
            price,
            trigger_price,
            average_price,
            status,
            tag,
            raw_response,
            created_at,
            updated_at
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8,
            $9, $10, $11, $12, $13, $14, $15, $16, $17, $17
        )
        ON CONFLICT (signal_id) DO NOTHING
        RETURNING id;
    """

    _UPDATE_STATUS = """
        UPDATE orders
        SET
            status          = $1,
            filled_quantity = COALESCE($2, filled_quantity),
            average_price   = COALESCE($3, average_price),
            updated_at      = $4
        WHERE broker_order_id = $5;
    """

    _SELECT_PENDING = """
        SELECT *
        FROM orders
        WHERE status IN ('OPEN', 'PARTIAL')
        ORDER BY created_at ASC;
    """

    _SELECT_BY_SIGNAL_ID = """
        SELECT *
        FROM orders
        WHERE signal_id = $1
        LIMIT 1;
    """

    _CHECK_SIGNAL_EXISTS = """
        SELECT id FROM orders WHERE signal_id = $1 LIMIT 1;
    """

    # ------------------------------------------------------------------

    def __init__(self, db_pool: asyncpg.Pool) -> None:
        self._pool = db_pool

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def create_order_record(
        self,
        signal: TradeSignal,
        order_response: OrderResponse,
    ) -> Optional[str]:
        """
        Persist a new order record linked to ``signal``.

        Before inserting, checks whether an order for ``signal.id``
        already exists to enforce idempotency at the DB level (Redis is
        the primary guard; this is the secondary defence).

        Parameters
        ----------
        signal:
            The originating TradeSignal.
        order_response:
            The broker's response after placing the order.

        Returns
        -------
        str or None
            Internal UUID of the newly created order row, or ``None``
            if the record was skipped due to a duplicate signal_id.
        """
        # ── Idempotency check ──────────────────────────────────────────
        existing = await self._get_existing_for_signal(str(signal.id))
        if existing is not None:
            logger.warning(
                "Order already exists for signal — skipping insert",
                extra={"signal_id": signal.id, "existing_order_id": existing},
            )
            return None

        order_id = str(uuid.uuid4())
        now = datetime.now(tz=timezone.utc)

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                self._INSERT_ORDER,
                order_id,
                str(signal.id),
                order_response.broker_order_id,
                signal.symbol,
                signal.exchange,
                signal.product,
                signal.order_type,
                signal.direction,
                signal.quantity,
                order_response.filled_quantity or 0,
                signal.entry_price,
                signal.trigger_price,
                order_response.average_price,
                order_response.status,
                f"SIGNAL:{signal.id}",
                order_response.raw_response,  # JSONB column
                now,
            )

        if row is None:
            # ON CONFLICT DO NOTHING path — treat as idempotent success
            logger.info(
                "Order insert skipped (signal_id conflict at DB level)",
                extra={"signal_id": signal.id},
            )
            return None

        logger.info(
            "Order record created",
            extra={
                "order_id": order_id,
                "signal_id": signal.id,
                "broker_order_id": order_response.broker_order_id,
            },
        )
        return order_id

    # ------------------------------------------------------------------

    async def update_order_status(
        self,
        broker_order_id: str,
        status: OrderStatus,
        fill_price: Optional[float] = None,
        filled_quantity: Optional[int] = None,
    ) -> None:
        """
        Update the status (and optional fill details) of an order by its
        broker-assigned order ID.

        Parameters
        ----------
        broker_order_id:
            The ID returned by the broker when the order was placed.
        status:
            New ``OrderStatus`` value (e.g. ``FILLED``, ``REJECTED``).
        fill_price:
            Average fill price; pass ``None`` to leave unchanged.
        filled_quantity:
            Cumulative filled quantity; pass ``None`` to leave unchanged.
        """
        now = datetime.now(tz=timezone.utc)
        async with self._pool.acquire() as conn:
            await conn.execute(
                self._UPDATE_STATUS,
                status.value,
                filled_quantity,
                fill_price,
                now,
                broker_order_id,
            )

        logger.info(
            "Order status updated",
            extra={
                "broker_order_id": broker_order_id,
                "status": status.value,
                "fill_price": fill_price,
            },
        )

    # ------------------------------------------------------------------

    async def get_pending_orders(self) -> List[Dict[str, Any]]:
        """
        Return all orders currently in OPEN or PARTIAL state.

        Returns
        -------
        list of dict
            Each dict mirrors a row from the ``orders`` table.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(self._SELECT_PENDING)

        orders = [dict(row) for row in rows]
        logger.debug("Fetched pending orders", extra={"count": len(orders)})
        return orders

    # ------------------------------------------------------------------

    async def sync_with_broker(self, broker_adapter: BrokerAdapter) -> None:
        """
        Poll the broker for the current status of all pending orders and
        update the database accordingly.

        Designed to be called on a periodic schedule (e.g. every 30 s)
        by the orchestrator. Individual failures are logged and skipped
        so a single bad order cannot block the sync cycle.

        Parameters
        ----------
        broker_adapter:
            Live broker adapter with an ``get_order_status`` coroutine.
        """
        pending = await self.get_pending_orders()
        if not pending:
            logger.debug("sync_with_broker: no pending orders to sync")
            return

        logger.info(
            "Syncing orders with broker",
            extra={"pending_count": len(pending)},
        )

        for order in pending:
            broker_order_id: str = order["broker_order_id"]
            try:
                broker_status = await broker_adapter.get_order_status(broker_order_id)

                new_status = OrderStatus(broker_status["status"])
                await self.update_order_status(
                    broker_order_id=broker_order_id,
                    status=new_status,
                    fill_price=broker_status.get("average_price"),
                    filled_quantity=broker_status.get("filled_quantity"),
                )

                logger.debug(
                    "Order synced",
                    extra={
                        "broker_order_id": broker_order_id,
                        "new_status": new_status.value,
                    },
                )
            except Exception as exc:
                # Non-fatal: log and continue with remaining orders
                logger.error(
                    "Failed to sync order with broker",
                    extra={"broker_order_id": broker_order_id, "error": str(exc)},
                    exc_info=True,
                )

    # ------------------------------------------------------------------

    async def get_order_by_signal_id(
        self,
        signal_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Look up an order by its originating signal ID.

        Parameters
        ----------
        signal_id:
            UUID string of the TradeSignal.

        Returns
        -------
        dict or None
            Order row as a dict, or ``None`` if not found.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(self._SELECT_BY_SIGNAL_ID, signal_id)

        if row is None:
            logger.debug(
                "No order found for signal_id",
                extra={"signal_id": signal_id},
            )
            return None

        return dict(row)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _get_existing_for_signal(self, signal_id: str) -> Optional[str]:
        """
        Return the order UUID if an order already exists for the given
        signal_id, otherwise return None.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(self._CHECK_SIGNAL_EXISTS, signal_id)
        return str(row["id"]) if row else None
