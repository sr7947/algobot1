"""
Structured audit logger — writes to PostgreSQL, JSONL files, and stdout.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

import structlog

from core.enums import AuditEventType
from core.models import TradeSignal, OrderRequest, OrderResponse
from config.settings import get_settings

logger = logging.getLogger(__name__)

# Configure structlog for JSON output
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

_struct_logger = structlog.get_logger("audit")


class AuditLogger:
    """
    Complete audit trail for every decision in the trading pipeline.
    Writes to:
      1. PostgreSQL audit_log table (via asyncpg)
      2. JSONL file at LOG_DIR/audit_{date}.jsonl
      3. structlog stdout (for container logging)
    """

    def __init__(self, db_pool=None):
        self.settings = get_settings()
        self.db_pool = db_pool
        self._log_dir = Path(self.settings.LOG_DIR)
        self._log_dir.mkdir(parents=True, exist_ok=True)

    def _get_log_file(self) -> Path:
        """Get today's JSONL log file path."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._log_dir / f"audit_{today}.jsonl"

    def _write_jsonl(self, record: dict) -> None:
        """Append a record to today's JSONL audit file."""
        try:
            with open(self._get_log_file(), "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception as e:
            logger.error(f"Failed to write JSONL audit: {e}")

    async def _write_db(self, record: dict) -> None:
        """Insert audit record into PostgreSQL."""
        if self.db_pool is None:
            return
        try:
            await self.db_pool.execute(
                """
                INSERT INTO audit_log (timestamp, event_type, entity_type, entity_id, payload, actor, severity)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
                """,
                record["timestamp"],
                record["event_type"],
                record.get("entity_type"),
                record.get("entity_id"),
                json.dumps(record.get("payload", {}), default=str),
                record.get("actor", "system"),
                record.get("severity", "INFO"),
            )
        except Exception as e:
            logger.error(f"Failed to write DB audit: {e}")

    # ── Core Log Method ──────────────────────────────────────────────

    async def log(
        self,
        event_type: AuditEventType | str,
        entity_type: str = "",
        entity_id: str = "",
        payload: Optional[dict[str, Any]] = None,
        actor: str = "system",
        severity: str = "INFO",
    ) -> None:
        """
        Log an audit event to all sinks.
        """
        event_str = event_type.value if isinstance(event_type, AuditEventType) else event_type
        record = {
            "timestamp": datetime.now(timezone.utc),
            "event_type": event_str,
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "payload": payload or {},
            "actor": actor,
            "severity": severity,
        }

        # 1. stdout via structlog
        _struct_logger.info(
            "audit_event",
            event_type=event_str,
            entity_type=entity_type,
            entity_id=str(entity_id),
            actor=actor,
        )

        # 2. JSONL file
        self._write_jsonl(record)

        # 3. PostgreSQL
        await self._write_db(record)

    # ── Convenience Shorthand Methods ────────────────────────────────

    async def log_signal(self, signal: TradeSignal, snapshot: Optional[dict] = None) -> None:
        """Log signal generation event."""
        await self.log(
            event_type=AuditEventType.SIGNAL_GENERATED,
            entity_type="signal",
            entity_id=str(signal.id),
            payload={
                "symbol": signal.symbol,
                "strategy": signal.strategy_name,
                "direction": signal.direction,
                "entry": float(signal.entry_price),
                "stop_loss": float(signal.stop_loss),
                "target": float(signal.target),
                "quantity": signal.quantity,
                "confidence": float(signal.confidence_score),
                "regime": signal.regime,
                "rationale": signal.rationale,
                "news_summary": signal.news_summary,
                "snapshot_keys": list(snapshot.keys()) if snapshot else [],
            },
        )

    async def log_telegram_action(
        self,
        signal_id: UUID | str,
        action: str,
        user_id: int | str,
        modified_qty: Optional[int] = None,
    ) -> None:
        """Log Telegram approval/rejection action."""
        event_map = {
            "APPROVE": AuditEventType.TELEGRAM_APPROVED,
            "APPROVED": AuditEventType.TELEGRAM_APPROVED,
            "REJECT": AuditEventType.TELEGRAM_REJECTED,
            "REJECTED": AuditEventType.TELEGRAM_REJECTED,
            "HALF_SIZE": AuditEventType.TELEGRAM_HALF_SIZE,
            "BLOCK": AuditEventType.TELEGRAM_BLOCKED,
            "BLOCKED": AuditEventType.TELEGRAM_BLOCKED,
        }
        event_type = event_map.get(action.upper(), AuditEventType.TELEGRAM_SENT)

        await self.log(
            event_type=event_type,
            entity_type="signal",
            entity_id=str(signal_id),
            payload={
                "action": action,
                "telegram_user_id": str(user_id),
                "modified_quantity": modified_qty,
            },
            actor=f"telegram_user:{user_id}",
        )

    async def log_order(
        self, order_request: OrderRequest, order_response: OrderResponse
    ) -> None:
        """Log order placement event."""
        await self.log(
            event_type=AuditEventType.ORDER_PLACED,
            entity_type="order",
            entity_id=order_response.broker_order_id or str(order_request.signal_id),
            payload={
                "signal_id": str(order_request.signal_id),
                "symbol": order_request.symbol,
                "direction": order_request.direction,
                "quantity": order_request.quantity,
                "price": float(order_request.price) if order_request.price else None,
                "order_type": order_request.order_type,
                "broker": order_request.broker,
                "broker_order_id": order_response.broker_order_id,
                "status": order_response.status,
                "message": order_response.message,
            },
        )

    async def log_risk_check(
        self,
        signal_id: UUID | str,
        passed: bool,
        reasons: list[str],
    ) -> None:
        """Log risk engine decision."""
        event_type = AuditEventType.RISK_CHECK_PASS if passed else AuditEventType.RISK_CHECK_FAIL
        await self.log(
            event_type=event_type,
            entity_type="signal",
            entity_id=str(signal_id),
            payload={"passed": passed, "reasons": reasons},
            severity="INFO" if passed else "WARNING",
        )

    async def log_kill_switch(self, reason: str, actor: str = "system") -> None:
        """Log kill switch activation."""
        await self.log(
            event_type=AuditEventType.KILL_SWITCH_ACTIVATED,
            entity_type="system",
            entity_id="kill_switch",
            payload={"reason": reason},
            actor=actor,
            severity="CRITICAL",
        )

    # ── Query Methods ────────────────────────────────────────────────

    async def get_audit_trail(self, signal_id: str) -> list[dict]:
        """Get all audit events for a signal, ordered by timestamp."""
        if self.db_pool is None:
            return []
        try:
            rows = await self.db_pool.fetch(
                """
                SELECT id, timestamp, event_type, payload, actor, severity
                FROM audit_log
                WHERE entity_id = $1
                ORDER BY timestamp ASC
                """,
                signal_id,
            )
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Failed to fetch audit trail: {e}")
            return []
