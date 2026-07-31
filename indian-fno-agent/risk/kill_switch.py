"""
risk/kill_switch.py
-------------------
Emergency kill switch for the Indian F&O trading agent.

The kill switch is a circuit-breaker that immediately halts all trading
activity. Its state is persisted in Redis so that multiple agent
processes share the same single source of truth.

Key behaviours:
  - Redis key  : ``kill_switch:{YYYY-MM-DD}`` (auto-resets the next day)
  - On activation: publishes a Redis ``KILL_SWITCH`` pub/sub event,
    sends a Telegram alert, and optionally cancels all open orders.
  - Telegram commands ``/killswitch`` and ``/resume`` are restricted
    to the configured admin chat ID.

Author: F&O Trading Agent
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime
from typing import TYPE_CHECKING

import pytz

if TYPE_CHECKING:
    import redis.asyncio as aioredis
    from telegram import Bot, Update
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# Redis pub/sub channel for inter-process kill-switch events
KILL_SWITCH_CHANNEL = "fno_agent:kill_switch"


def _today_key() -> str:
    """Return the Redis key for today's kill-switch state (IST date)."""
    today = date.today().isoformat()
    return f"kill_switch:{today}"


class KillSwitch:
    """
    Distributed kill switch backed by Redis.

    Parameters
    ----------
    redis_client : aioredis.Redis
        Async Redis client instance.
    broker_adapter : object
        Broker adapter exposing ``cancel_all_orders()`` coroutine.
    telegram_bot : Bot | None
        Telegram Bot instance for alert messages (optional).
    admin_chat_id : int | str | None
        Telegram chat ID that is authorised to toggle the kill switch.
    settings : object
        Application settings – used for ``MAX_DAILY_LOSS_PCT`` and
        ``CAPITAL`` to support auto-check threshold logic.
    """

    def __init__(
        self,
        redis_client: "aioredis.Redis",
        broker_adapter: object,
        telegram_bot: "Bot | None" = None,
        admin_chat_id: "int | str | None" = None,
        settings: object = None,
    ) -> None:
        self._redis = redis_client
        self._broker = broker_adapter
        self._bot = telegram_bot
        self._admin_chat_id = admin_chat_id
        self._settings = settings

    # ------------------------------------------------------------------
    # Core state management
    # ------------------------------------------------------------------

    async def activate(self, reason: str, actor: str) -> None:
        """
        Activate the kill switch.

        Steps:
          1. Set Redis key with reason + actor metadata.
          2. Publish a ``KILL_SWITCH`` event on the pub/sub channel.
          3. Send a Telegram alert to the admin chat (if configured).

        Parameters
        ----------
        reason : str
            Human-readable explanation for activation.
        actor : str
            Who triggered the activation (e.g. 'auto', 'admin', 'user:123').
        """
        key = _today_key()
        payload = {
            "active": True,
            "reason": reason,
            "actor": actor,
            "activated_at": datetime.now(IST).isoformat(),
        }
        # Persist state – key expires at midnight (86 400 s)
        await self._redis.set(key, json.dumps(payload), ex=86_400)

        logger.critical(
            "KILL SWITCH ACTIVATED | actor=%s reason=%s", actor, reason
        )

        # Publish event so all subscriber processes halt immediately
        event = json.dumps({"event": "KILL_SWITCH", "actor": actor, "reason": reason})
        await self._redis.publish(KILL_SWITCH_CHANNEL, event)

        # Telegram alert
        await self._send_telegram_alert(
            f"🚨 *KILL SWITCH ACTIVATED*\n"
            f"Actor: `{actor}`\n"
            f"Reason: {reason}\n"
            f"Time: {datetime.now(IST).strftime('%H:%M:%S IST')}\n\n"
            f"Use /resume to deactivate after manual review."
        )

    async def deactivate(self, actor: str) -> None:
        """
        Deactivate the kill switch and resume normal trading.

        Parameters
        ----------
        actor : str
            Who triggered the deactivation.
        """
        key = _today_key()
        payload_raw = await self._redis.get(key)

        if payload_raw:
            payload = json.loads(payload_raw)
            payload["active"] = False
            payload["deactivated_at"] = datetime.now(IST).isoformat()
            payload["deactivated_by"] = actor
            await self._redis.set(key, json.dumps(payload), ex=86_400)
        else:
            # Key missing – kill switch was never activated today
            logger.info("deactivate called but kill switch was not active.")
            return

        logger.warning(
            "KILL SWITCH DEACTIVATED | actor=%s | Trading can resume.", actor
        )

        # Publish resume event
        event = json.dumps({"event": "KILL_SWITCH_CLEARED", "actor": actor})
        await self._redis.publish(KILL_SWITCH_CHANNEL, event)

        await self._send_telegram_alert(
            f"✅ *Kill switch deactivated* by `{actor}`.\n"
            f"Time: {datetime.now(IST).strftime('%H:%M:%S IST')}\n"
            "Trading may now resume."
        )

    async def is_active(self) -> bool:
        """
        Check whether the kill switch is currently active.

        Returns
        -------
        bool
            True if the kill switch is active; False otherwise.
        """
        key = _today_key()
        payload_raw = await self._redis.get(key)
        if not payload_raw:
            return False
        try:
            payload = json.loads(payload_raw)
            return bool(payload.get("active", False))
        except (json.JSONDecodeError, AttributeError):
            logger.error("Corrupted kill_switch Redis value; treating as inactive.")
            return False

    # ------------------------------------------------------------------
    # Automatic threshold check
    # ------------------------------------------------------------------

    async def auto_check(self, daily_pnl: float, max_daily_loss: float) -> bool:
        """
        Automatically activate the kill switch if the daily loss limit is breached.

        Intended to be called after every trade close so that the agent
        self-halts before any additional losses are incurred.

        Parameters
        ----------
        daily_pnl : float
            Current day's realised P&L in INR (negative = loss).
        max_daily_loss : float
            Maximum acceptable daily loss in INR (positive magnitude).
            The kill switch fires when daily_pnl < -max_daily_loss.

        Returns
        -------
        bool
            True if the kill switch was activated by this call; False otherwise.
        """
        threshold = -abs(max_daily_loss)
        if daily_pnl < threshold:
            logger.critical(
                "auto_check: daily_pnl=%.2f breached threshold=%.2f – auto-activating kill switch.",
                daily_pnl,
                threshold,
            )
            already_active = await self.is_active()
            if not already_active:
                await self.activate(
                    reason=(
                        f"Auto-triggered: daily P&L Rs.{daily_pnl:,.2f} breached "
                        f"loss limit of Rs.{threshold:,.2f}"
                    ),
                    actor="auto_risk_engine",
                )
                return True
        return False

    # ------------------------------------------------------------------
    # Emergency stop
    # ------------------------------------------------------------------

    async def emergency_stop(self) -> None:
        """
        Activate the kill switch AND immediately cancel all open orders.

        This is the hardest stop: it first halts new trading (kill switch),
        then instructs the broker adapter to cancel every open order in the
        account. Use only in true emergency situations.
        """
        await self.activate(
            reason="Emergency stop requested – cancelling all open orders.",
            actor="emergency_stop",
        )

        logger.critical("Cancelling all open orders via broker adapter...")
        try:
            cancelled = await self._broker.cancel_all_orders()
            logger.critical(
                "emergency_stop: cancelled %d open orders.",
                cancelled if isinstance(cancelled, int) else "N/A",
            )
            await self._send_telegram_alert(
                "⛔ *EMERGENCY STOP executed.*\n"
                f"All open orders cancelled.\n"
                f"Time: {datetime.now(IST).strftime('%H:%M:%S IST')}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to cancel all orders during emergency stop: %s", exc)
            await self._send_telegram_alert(
                "⛔ *EMERGENCY STOP activated but order cancellation FAILED.*\n"
                f"Error: {exc}\n"
                "Please cancel orders MANUALLY via the broker terminal."
            )

    # ------------------------------------------------------------------
    # Telegram command handlers
    # ------------------------------------------------------------------

    async def handle_killswitch_command(
        self,
        update: "Update",
        context: "ContextTypes.DEFAULT_TYPE",
    ) -> None:
        """
        Telegram command handler: /killswitch

        Only the configured admin chat ID may use this command.
        Usage: /killswitch [optional reason text]
        """
        if not self._is_admin(update):
            await update.message.reply_text(
                "⛔ Unauthorised. This command is restricted to admin users."
            )
            logger.warning(
                "Unauthorised /killswitch attempt from chat_id=%s",
                update.effective_chat.id,
            )
            return

        args = context.args or []
        reason = " ".join(args) if args else "Manual activation via Telegram /killswitch command"
        actor = f"telegram:{update.effective_user.id}"

        await self.activate(reason=reason, actor=actor)
        await update.message.reply_text(
            f"🚨 Kill switch ACTIVATED.\nReason: {reason}\nUse /resume to deactivate."
        )

    async def handle_resume_command(
        self,
        update: "Update",
        context: "ContextTypes.DEFAULT_TYPE",
    ) -> None:
        """
        Telegram command handler: /resume

        Only the configured admin chat ID may use this command.
        Deactivates the kill switch and allows trading to resume.
        """
        if not self._is_admin(update):
            await update.message.reply_text(
                "⛔ Unauthorised. This command is restricted to admin users."
            )
            logger.warning(
                "Unauthorised /resume attempt from chat_id=%s",
                update.effective_chat.id,
            )
            return

        actor = f"telegram:{update.effective_user.id}"
        is_active = await self.is_active()

        if not is_active:
            await update.message.reply_text("ℹ️ Kill switch is not currently active.")
            return

        await self.deactivate(actor=actor)
        await update.message.reply_text("✅ Kill switch DEACTIVATED. Trading may resume.")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _is_admin(self, update: "Update") -> bool:
        """Return True if the message originates from the configured admin chat."""
        if self._admin_chat_id is None:
            # No admin chat configured – restrict to nobody for safety
            logger.warning("ADMIN_CHAT_ID not configured; rejecting all kill-switch commands.")
            return False
        return str(update.effective_chat.id) == str(self._admin_chat_id)

    async def _send_telegram_alert(self, message: str) -> None:
        """
        Send a Markdown-formatted alert to the admin Telegram chat.

        Fails silently with a log entry if the bot is not configured or if
        the send operation errors out.
        """
        if self._bot is None or self._admin_chat_id is None:
            logger.debug("Telegram bot not configured; skipping alert: %s", message)
            return
        try:
            await self._bot.send_message(
                chat_id=self._admin_chat_id,
                text=message,
                parse_mode="Markdown",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to send Telegram kill-switch alert: %s", exc)
