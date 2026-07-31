"""
execution/position_tracker.py
------------------------------
In-memory + PostgreSQL position lifecycle manager for the F&O agent.

Responsibilities:
  - Maintain a hot dict of open Position objects for sub-millisecond reads
  - Mark-to-market on every market-data tick
  - Automatic SL / Target detection with trailing-SL logic
  - Compute daily P&L across closed positions
  - Publish domain events to the EventBus so downstream consumers
    (execution engine, notifier, risk manager) can react without polling

Trailing SL rules
-----------------
  profit >= 1.5 × risk  →  move SL to breakeven (entry price)
  profit >= 2.0 × risk  →  trail SL by 0.5 × ATR below/above current price
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

import asyncpg  # type: ignore

from core.enums import EventType, PositionStatus, PositionSide
from core.events import EventBus
from core.models import OrderResponse, Position, TradeSignal

logger = logging.getLogger(__name__)


class PositionTracker:
    """
    Thread-safe (asyncio-safe) position lifecycle manager.

    Parameters
    ----------
    db_pool:
        ``asyncpg.Pool`` for persisting positions to PostgreSQL.
    event_bus:
        ``EventBus`` instance used to publish SL/target events.
    """

    # ------------------------------------------------------------------
    # SQL
    # ------------------------------------------------------------------

    _INSERT_POSITION = """
        INSERT INTO positions (
            id, signal_id, broker_order_id, symbol, exchange, product,
            side, entry_price, current_price, stop_loss, target,
            sl_order_id, open_quantity, atr, status,
            opened_at, updated_at
        ) VALUES (
            $1, $2, $3, $4, $5, $6,
            $7, $8, $8, $9, $10,
            $11, $12, $13, 'OPEN',
            $14, $14
        );
    """

    _UPDATE_PRICE = """
        UPDATE positions
        SET current_price  = $1,
            unrealized_pnl = $2,
            updated_at     = $3
        WHERE id = $4;
    """

    _UPDATE_SL = """
        UPDATE positions
        SET stop_loss  = $1,
            updated_at = $2
        WHERE id = $3;
    """

    _CLOSE_POSITION = """
        UPDATE positions
        SET status        = 'CLOSED',
            exit_price    = $1,
            realized_pnl  = $2,
            exit_reason   = $3,
            closed_at     = $4,
            updated_at    = $4
        WHERE id = $5;
    """

    _SELECT_DAILY_PNL = """
        SELECT COALESCE(SUM(realized_pnl), 0) AS total_pnl
        FROM positions
        WHERE status    = 'CLOSED'
          AND closed_at >= $1;
    """

    _SELECT_AUDIT_TRAIL = """
        SELECT * FROM positions
        WHERE signal_id = $1
        ORDER BY opened_at ASC;
    """

    # ------------------------------------------------------------------

    def __init__(
        self,
        db_pool: asyncpg.Pool,
        event_bus: EventBus,
    ) -> None:
        self._pool = db_pool
        self._bus = event_bus
        # Hot cache: position_id → Position
        self._positions: Dict[str, Position] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def open_position(
        self,
        signal: TradeSignal,
        order_response: OrderResponse,
    ) -> Position:
        """
        Create a new Position from a filled order and persist it.

        Parameters
        ----------
        signal:
            Originating trade signal (carries SL, target, ATR, side, etc.)
        order_response:
            Broker response with actual fill price and order ID.

        Returns
        -------
        Position
            The newly created position object (also cached in memory).
        """
        position_id = str(uuid.uuid4())
        now = datetime.now(tz=timezone.utc)

        fill_price = order_response.average_price or signal.entry_price

        position = Position(
            position_id=position_id,
            signal_id=str(signal.id),
            broker_order_id=order_response.broker_order_id,
            symbol=signal.symbol,
            exchange=signal.exchange,
            product=signal.product,
            side=PositionSide(signal.direction),  # 'BUY' → LONG, 'SELL' → SHORT
            entry_price=fill_price,
            current_price=fill_price,
            stop_loss=signal.stop_loss,
            target=signal.target,
            sl_order_id=order_response.sl_order_id,
            open_quantity=signal.quantity,
            atr=signal.atr,
            status=PositionStatus.OPEN,
            unrealized_pnl=0.0,
            realized_pnl=None,
            opened_at=now,
        )

        # Persist to DB
        async with self._pool.acquire() as conn:
            await conn.execute(
                self._INSERT_POSITION,
                position_id,
                signal.id,
                order_response.broker_order_id,
                signal.symbol,
                signal.exchange,
                signal.product,
                signal.direction,   # raw string stored; model converts
                fill_price,
                signal.stop_loss,
                signal.target,
                order_response.sl_order_id,
                signal.quantity,
                signal.atr,
                now,
            )

        # Hot cache
        self._positions[position_id] = position

        logger.info(
            "Position opened",
            extra={
                "position_id": position_id,
                "symbol": signal.symbol,
                "side": signal.direction,
                "entry": fill_price,
            },
        )
        return position

    # ------------------------------------------------------------------

    async def update_prices(self, market_data: Dict[str, float]) -> None:
        """
        Mark-to-market all open positions using the latest bid/ask/LTP data.

        Parameters
        ----------
        market_data:
            Mapping of symbol → last traded price.
            e.g. {"NIFTY24JUL21000CE": 125.50, ...}
        """
        now = datetime.now(tz=timezone.utc)

        for position_id, position in list(self._positions.items()):
            ltp = market_data.get(position.symbol)
            if ltp is None:
                continue  # no data for this symbol in this tick

            position.current_price = ltp
            position.unrealized_pnl = self._calc_unrealized_pnl(position, ltp)

            # Persist mark-to-market row
            async with self._pool.acquire() as conn:
                await conn.execute(
                    self._UPDATE_PRICE,
                    ltp,
                    position.unrealized_pnl,
                    now,
                    position_id,
                )

            logger.debug(
                "Price updated",
                extra={
                    "position_id": position_id,
                    "ltp": ltp,
                    "unrealized_pnl": position.unrealized_pnl,
                },
            )

    # ------------------------------------------------------------------

    async def check_sl_target(
        self,
        position: Position,
    ) -> Optional[str]:
        """
        Evaluate whether a position has hit its stop-loss, target, or
        qualifies for a trailing SL adjustment.

        Returns
        -------
        str or None
            One of ``'SL_HIT'``, ``'TARGET_HIT'``, ``'TRAILING'``,
            or ``None`` if no action is required.
        """
        ltp = position.current_price
        entry = position.entry_price
        sl = position.stop_loss
        target = position.target
        atr = position.atr or 0.0

        # Direction-aware deltas
        if position.side == PositionSide.LONG:
            sl_distance = entry - sl        # positive when SL is below entry
            tgt_distance = target - entry   # positive when target is above entry
            profit = ltp - entry
            loss = entry - ltp
        else:
            # SHORT position — profit when price falls
            sl_distance = sl - entry
            tgt_distance = entry - target
            profit = entry - ltp
            loss = ltp - entry

        # ── Stop-loss breached ────────────────────────────────────────
        if loss >= sl_distance:
            logger.info(
                "SL_HIT detected",
                extra={
                    "position_id": position.position_id,
                    "ltp": ltp,
                    "sl": sl,
                },
            )
            await self._bus.publish(EventType.SL_HIT, {"position": position})
            return "SL_HIT"

        # ── Target reached ────────────────────────────────────────────
        if profit >= tgt_distance:
            logger.info(
                "TARGET_HIT detected",
                extra={
                    "position_id": position.position_id,
                    "ltp": ltp,
                    "target": target,
                },
            )
            await self._bus.publish(EventType.TARGET_HIT, {"position": position})
            return "TARGET_HIT"

        # ── Trailing SL logic ─────────────────────────────────────────
        risk = sl_distance  # initial risk in price terms

        if profit >= 2.0 * risk and atr > 0:
            # Trail SL by 0.5 ATR
            if position.side == PositionSide.LONG:
                new_sl = ltp - (0.5 * atr)
            else:
                new_sl = ltp + (0.5 * atr)

            # Only move SL in the profitable direction
            if self._is_sl_improvement(position, new_sl):
                await self.update_stop_loss(position.position_id, new_sl)
                await self._bus.publish(
                    EventType.TRAILING_SL_MOVED,
                    {"position": position, "new_sl": new_sl, "reason": "2x_trail"},
                )
                logger.info(
                    "Trailing SL moved (2x ATR rule)",
                    extra={"position_id": position.position_id, "new_sl": new_sl},
                )
                return "TRAILING"

        elif profit >= 1.5 * risk:
            # Move SL to breakeven
            new_sl = entry
            if self._is_sl_improvement(position, new_sl):
                await self.update_stop_loss(position.position_id, new_sl)
                await self._bus.publish(
                    EventType.TRAILING_SL_MOVED,
                    {"position": position, "new_sl": new_sl, "reason": "breakeven"},
                )
                logger.info(
                    "Trailing SL moved to breakeven",
                    extra={"position_id": position.position_id, "new_sl": new_sl},
                )
                return "TRAILING"

        return None

    # ------------------------------------------------------------------

    async def close_position(
        self,
        position_id: str,
        exit_price: float,
        reason: str,
    ) -> None:
        """
        Mark a position as closed, compute realised P&L, persist to DB,
        and remove from the hot cache.

        Parameters
        ----------
        position_id:
            Internal UUID of the position.
        exit_price:
            Actual fill price of the closing order.
        reason:
            Exit reason tag (e.g. 'TARGET_HIT', 'EMERGENCY').
        """
        position = self._positions.get(position_id)
        if position is None:
            logger.warning(
                "close_position called for unknown position",
                extra={"position_id": position_id},
            )
            return

        realized_pnl = self._calc_realized_pnl(position, exit_price)
        now = datetime.now(tz=timezone.utc)

        async with self._pool.acquire() as conn:
            await conn.execute(
                self._CLOSE_POSITION,
                exit_price,
                realized_pnl,
                reason,
                now,
                position_id,
            )

        # Update in-memory object before evicting
        position.status = PositionStatus.CLOSED
        position.exit_price = exit_price
        position.realized_pnl = realized_pnl

        del self._positions[position_id]

        logger.info(
            "Position closed",
            extra={
                "position_id": position_id,
                "exit_price": exit_price,
                "realized_pnl": realized_pnl,
                "reason": reason,
            },
        )

    # ------------------------------------------------------------------

    def get_open_positions(self) -> List[Position]:
        """Return a snapshot list of all currently open positions."""
        return list(self._positions.values())

    def get_position(self, position_id: str) -> Optional[Position]:
        """Lookup a single open position by ID."""
        return self._positions.get(position_id)

    async def update_stop_loss(self, position_id: str, new_sl: float) -> None:
        """
        Update stop-loss in both the in-memory cache and the database.
        """
        position = self._positions.get(position_id)
        if position is None:
            return

        position.stop_loss = new_sl
        now = datetime.now(tz=timezone.utc)

        async with self._pool.acquire() as conn:
            await conn.execute(self._UPDATE_SL, new_sl, now, position_id)

    async def get_daily_pnl(self) -> float:
        """
        Return the sum of realised P&L for all positions closed today
        (IST calendar day, stored as UTC in DB).

        Returns
        -------
        float
            Total realised P&L in INR for today.
        """
        today_start = datetime.combine(
            date.today(), datetime.min.time()
        ).replace(tzinfo=timezone.utc)

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(self._SELECT_DAILY_PNL, today_start)

        return float(row["total_pnl"]) if row else 0.0

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_unrealized_pnl(position: Position, ltp: float) -> float:
        """Compute unrealised P&L for one position at LTP."""
        if position.side == PositionSide.LONG:
            return (ltp - position.entry_price) * position.open_quantity
        else:
            return (position.entry_price - ltp) * position.open_quantity

    @staticmethod
    def _calc_realized_pnl(position: Position, exit_price: float) -> float:
        """Compute realised P&L on exit."""
        if position.side == PositionSide.LONG:
            return (exit_price - position.entry_price) * position.open_quantity
        else:
            return (position.entry_price - exit_price) * position.open_quantity

    @staticmethod
    def _is_sl_improvement(position: Position, new_sl: float) -> bool:
        """
        Return True only if the new_sl is an improvement (i.e. moves in
        the direction that reduces risk) over the current SL.
        """
        if position.side == PositionSide.LONG:
            return new_sl > position.stop_loss   # higher SL is better for longs
        else:
            return new_sl < position.stop_loss   # lower SL is better for shorts
