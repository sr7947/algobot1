"""
execution/engine.py
-------------------
Core execution engine for the Indian F&O Trading Agent.

Responsibilities:
  - Accepting trade signals and placing orders via a broker adapter
  - Idempotency via Redis (prevents duplicate orders)
  - Paper-mode routing to a simulated adapter
  - Position lifecycle management (open / modify SL / close / emergency close)
  - Telegram notifications and structured audit logging for every action

Dependencies (injected via constructor):
  broker_adapter   – BrokerAdapter (live or paper)
  position_tracker – PositionTracker
  order_manager    – OrderManager
  notifier         – TelegramNotifier
  audit_logger     – AuditLogger
  redis_client     – aioredis.Redis
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import aioredis  # type: ignore

from audit.logger import AuditLogger, AuditEventType
from brokers.base import BrokerAdapter
from core.exceptions import (
    DuplicateOrderError,
    OrderPlacementError,
    PositionNotFoundError,
)
from core.models import OrderRequest, OrderResponse, TradeSignal, Position
from core.settings import get_settings
from execution.order_manager import OrderManager
from execution.position_tracker import PositionTracker
from notifications.telegram import TelegramNotifier

logger = logging.getLogger(__name__)
settings = get_settings()


class ExecutionEngine:
    """
    Orchestrates the full order-placement lifecycle for a trade signal.

    All public methods are coroutines (async) so they integrate cleanly
    with the asyncio event loop used throughout the agent.
    """

    # Redis key expiry for idempotency records (24 hours)
    _IDEMPOTENCY_TTL_SECONDS: int = 86_400

    def __init__(
        self,
        broker_adapter: BrokerAdapter,
        position_tracker: PositionTracker,
        order_manager: OrderManager,
        notifier: TelegramNotifier,
        audit_logger: AuditLogger,
        redis_client: aioredis.Redis,
    ) -> None:
        # In paper-mode, swap in the simulated adapter transparently
        if settings.is_paper_mode():
            from brokers.paper import PaperBrokerAdapter  # local import avoids circular

            logger.info("Paper mode active — routing orders to PaperBrokerAdapter")
            self._broker = PaperBrokerAdapter()
        else:
            self._broker = broker_adapter

        self._positions = position_tracker
        self._orders = order_manager
        self._notifier = notifier
        self._audit = audit_logger
        self._redis = redis_client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute_signal(self, signal: TradeSignal) -> OrderResponse:
        """
        End-to-end execution of a trade signal.

        Steps
        -----
        1. Build a deterministic idempotency key.
        2. Check Redis — raise DuplicateOrderError if already processed.
        3. Build OrderRequest from the signal.
        4. Place order via broker adapter.
        5. Persist idempotency key in Redis (24 hr TTL).
        6. Open position in PositionTracker.
        7. Record in OrderManager.
        8. Send Telegram notification (success or failure).
        9. Write audit log entry.
        10. Return the OrderResponse.

        Parameters
        ----------
        signal:
            Fully validated TradeSignal coming from the strategy layer.

        Returns
        -------
        OrderResponse
            Contains broker order ID, fill price, status, timestamps, etc.

        Raises
        ------
        DuplicateOrderError
            If an order for this signal on today's date already exists.
        OrderPlacementError
            If the broker rejects the order for any reason.
        """
        idempotency_key = (
            f"order:{signal.id}:{signal.created_at.date()}"
        )

        # ── Step 1: Duplicate guard ──────────────────────────────────────
        if await self._redis.exists(idempotency_key):
            logger.warning(
                "Duplicate order attempt blocked",
                extra={"signal_id": signal.id, "key": idempotency_key},
            )
            raise DuplicateOrderError(
                f"Signal {signal.id} already has an order placed today."
            )

        # ── Step 2: Build order request ──────────────────────────────────
        order_request = self._build_order_request(signal)

        # ── Step 3: Place with broker ────────────────────────────────────
        order_response: Optional[OrderResponse] = None
        try:
            order_response = await self._broker.place_order(order_request)
            logger.info(
                "Order placed successfully",
                extra={
                    "signal_id": signal.id,
                    "broker_order_id": order_response.broker_order_id,
                    "status": order_response.status,
                },
            )
        except Exception as exc:
            # Notify on failure but do NOT store idempotency key —
            # the operator may want to retry after fixing the issue.
            error_msg = f"Order placement failed for signal {signal.id}: {exc}"
            logger.error(error_msg, exc_info=True)
            await self._notifier.send_order_failed(signal, str(exc))
            await self._audit.log(
                event_type=AuditEventType.ORDER_FAILED,
                entity_type="signal",
                entity_id=str(signal.id),
                payload={"error": str(exc), "order_request": order_request.dict()},
            )
            raise OrderPlacementError(error_msg) from exc

        # ── Step 4: Idempotency record in Redis ──────────────────────────
        await self._redis.set(
            idempotency_key,
            order_response.broker_order_id,
            ex=self._IDEMPOTENCY_TTL_SECONDS,
        )

        # ── Step 5: Open position in tracker ────────────────────────────
        await self._positions.open_position(signal, order_response)

        # ── Step 6: Persist order record ─────────────────────────────────
        await self._orders.create_order_record(signal, order_response)

        # ── Step 7: Telegram notification ───────────────────────────────
        await self._notifier.send_order_placed(signal, order_response)

        # ── Step 8: Audit log ────────────────────────────────────────────
        await self._audit.log_order(order_request, order_response)

        return order_response

    # ------------------------------------------------------------------

    async def modify_stop_loss(
        self,
        position_id: str,
        new_sl: float,
    ) -> None:
        """
        Modify the stop-loss of an existing open position.

        Sends an SL order modification request to the broker and updates
        the position in the tracker so that subsequent P&L checks use
        the new stop-loss level.

        Parameters
        ----------
        position_id:
            Internal position UUID.
        new_sl:
            New stop-loss price in INR.

        Raises
        ------
        PositionNotFoundError
            If no open position with the given ID exists.
        """
        position: Position = self._positions.get_position(position_id)
        if position is None:
            raise PositionNotFoundError(
                f"No open position found for id={position_id}"
            )

        logger.info(
            "Modifying stop-loss",
            extra={
                "position_id": position_id,
                "old_sl": position.stop_loss,
                "new_sl": new_sl,
            },
        )

        modify_request = OrderRequest(
            symbol=position.symbol,
            exchange=position.exchange,
            product=position.product,
            order_type="SL",
            transaction_type="SELL" if position.side == "LONG" else "BUY",
            quantity=position.open_quantity,
            price=new_sl,
            trigger_price=new_sl,
            tag=f"SL_MODIFY:{position_id}",
        )

        await self._broker.modify_order(
            broker_order_id=position.sl_order_id,
            modify_request=modify_request,
        )

        # Reflect change in memory + DB
        await self._positions.update_stop_loss(position_id, new_sl)

        await self._audit.log(
            event_type=AuditEventType.SL_MODIFIED,
            entity_type="position",
            entity_id=position_id,
            payload={"old_sl": position.stop_loss, "new_sl": new_sl},
        )
        await self._notifier.send_sl_modified(position, new_sl)

    # ------------------------------------------------------------------

    async def close_position(
        self,
        position_id: str,
        reason: str = "MANUAL",
    ) -> None:
        """
        Close an open position using a market order.

        After the order is filled the position tracker is updated and
        a Telegram message + audit record are generated.

        Parameters
        ----------
        position_id:
            Internal position UUID.
        reason:
            Human-readable reason string (e.g. 'TARGET_HIT', 'MANUAL',
            'TRAILING_SL').
        """
        position: Position = self._positions.get_position(position_id)
        if position is None:
            raise PositionNotFoundError(
                f"Cannot close — position {position_id} not found"
            )

        logger.info(
            "Closing position",
            extra={"position_id": position_id, "reason": reason},
        )

        close_request = OrderRequest(
            symbol=position.symbol,
            exchange=position.exchange,
            product=position.product,
            order_type="MARKET",
            transaction_type="SELL" if position.side == "LONG" else "BUY",
            quantity=position.open_quantity,
            tag=f"CLOSE:{reason}:{position_id}",
        )

        try:
            close_response: OrderResponse = await self._broker.place_order(
                close_request
            )
        except Exception as exc:
            logger.error(
                "Failed to close position",
                extra={"position_id": position_id, "error": str(exc)},
                exc_info=True,
            )
            await self._notifier.send_close_failed(position, reason, str(exc))
            raise

        # Update position state with exit fill price
        await self._positions.close_position(
            position_id=position_id,
            exit_price=close_response.average_price,
            reason=reason,
        )

        await self._audit.log(
            event_type=AuditEventType.POSITION_CLOSED,
            entity_type="position",
            entity_id=position_id,
            payload={
                "reason": reason,
                "exit_price": close_response.average_price,
                "broker_order_id": close_response.broker_order_id,
            },
        )
        await self._notifier.send_position_closed(position, close_response, reason)

    # ------------------------------------------------------------------

    async def emergency_close_all(self) -> None:
        """
        Emergency routine — closes every open position immediately via
        concurrent market orders.

        Errors from individual closures are logged but do not abort the
        remaining closures (best-effort approach during emergencies).
        """
        open_positions = self._positions.get_open_positions()
        if not open_positions:
            logger.info("emergency_close_all called but no open positions found")
            return

        logger.warning(
            "EMERGENCY CLOSE ALL triggered",
            extra={"position_count": len(open_positions)},
        )

        await self._notifier.send_emergency_close_started(len(open_positions))

        # Fire all close requests concurrently; gather errors without
        # stopping the remaining tasks.
        tasks = [
            asyncio.create_task(
                self._safe_close(pos.position_id, reason="EMERGENCY"),
                name=f"emergency_close_{pos.position_id}",
            )
            for pos in open_positions
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        failed = [r for r in results if isinstance(r, Exception)]
        if failed:
            logger.error(
                "Some emergency closes failed",
                extra={"failed_count": len(failed), "errors": [str(e) for e in failed]},
            )

        await self._audit.log(
            event_type=AuditEventType.EMERGENCY_CLOSE,
            entity_type="system",
            entity_id="ALL",
            payload={
                "positions_attempted": len(open_positions),
                "failed_count": len(failed),
            },
        )
        await self._notifier.send_emergency_close_completed(
            total=len(open_positions), failed=len(failed)
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_order_request(self, signal: TradeSignal) -> OrderRequest:
        """
        Translate a TradeSignal into a broker-agnostic OrderRequest.

        The signal already carries validated lot size, product type and
        exchange from the risk-management layer.
        """
        return OrderRequest(
            symbol=signal.symbol,
            exchange=signal.exchange,
            product=signal.product,             # e.g. 'NRML', 'MIS'
            order_type=signal.order_type,       # e.g. 'LIMIT', 'MARKET', 'SL'
            transaction_type=signal.direction,  # 'BUY' or 'SELL'
            quantity=signal.quantity,
            price=signal.entry_price,
            trigger_price=signal.trigger_price,
            validity="DAY",
            tag=f"SIGNAL:{signal.id}",
            disclosed_quantity=0,
        )

    async def _safe_close(self, position_id: str, reason: str) -> None:
        """
        Wrapper around close_position that swallows exceptions so that
        asyncio.gather can collect them without short-circuiting siblings.
        """
        try:
            await self.close_position(position_id, reason)
        except Exception as exc:
            logger.error(
                "safe_close failed",
                extra={"position_id": position_id, "error": str(exc)},
                exc_info=True,
            )
            raise  # re-raise so gather captures it as a failed result
