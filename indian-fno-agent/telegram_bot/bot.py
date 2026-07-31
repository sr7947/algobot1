"""
Telegram bot — main entry point, command handlers, and lifecycle.
"""
from __future__ import annotations

import logging
from functools import wraps
from typing import Any, Optional

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from config.settings import get_settings
from telegram_bot.handlers import (
    handle_approve,
    handle_block_similar,
    handle_half_size,
    handle_reject,
)
from telegram_bot import signal_store as shared_signals

logger = logging.getLogger(__name__)

settings = get_settings()


def admin_only(func):
    """Decorator to restrict commands to the configured admin chat ID."""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        update = None
        for arg in args:
            if isinstance(arg, Update):
                update = arg
                break

        if update and update.effective_user:
            user_id = update.effective_user.id
            allowed = int(settings.TELEGRAM_CHAT_ID) if settings.TELEGRAM_CHAT_ID and str(settings.TELEGRAM_CHAT_ID).lstrip("-").isdigit() else None
            if allowed and user_id != allowed:
                if update.message:
                    await update.message.reply_text("⛔ Unauthorized. You are not the admin.")
                logger.warning(f"Unauthorized access attempt from user {user_id}")
                return

        return await func(*args, **kwargs)

    return wrapper


class TelegramBot:
    """
    Central Telegram bot that handles commands and inline button callbacks.
    Uses python-telegram-bot v21 async architecture.
    """

    def __init__(self, orchestrator=None, risk_engine=None, position_tracker=None, kill_switch=None):
        self.orchestrator = orchestrator
        self.risk_engine = risk_engine
        self.position_tracker = position_tracker
        self.kill_switch = kill_switch
        self._app: Optional[Application] = None
        # Always use the process-wide store so sample scripts / API routes
        # and the polling bot share the same pending signals.
        self._signal_store = shared_signals.get_signal_store()

    def register_signal(self, signal: Any) -> None:
        """Register a signal so Approve/Reject callbacks can look it up."""
        signal_id = shared_signals.register_signal(signal)
        if self._app is not None:
            self._app.bot_data["signal_store"] = self._signal_store
            self._app.bot_data["signal_store"][signal_id] = signal

    async def start(self) -> None:
        """Build and start the Telegram bot (polling mode)."""
        if not settings.TELEGRAM_BOT_TOKEN:
            logger.warning("TELEGRAM_BOT_TOKEN not set — Telegram bot disabled.")
            return

        builder = Application.builder().token(settings.TELEGRAM_BOT_TOKEN)
        self._app = builder.build()
        self._signal_store = shared_signals.get_signal_store()
        self._app.bot_data["signal_store"] = self._signal_store
        self._app.bot_data["orchestrator"] = self.orchestrator
        chat = settings.TELEGRAM_CHAT_ID
        self._app.bot_data["admin_id"] = (
            int(chat) if chat and str(chat).lstrip("-").isdigit() else 0
        )

        # Register command handlers
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("positions", self._cmd_positions))
        self._app.add_handler(CommandHandler("signals", self._cmd_signals))
        self._app.add_handler(CommandHandler("pnl", self._cmd_pnl))
        self._app.add_handler(CommandHandler("killswitch", self._cmd_killswitch))
        self._app.add_handler(CommandHandler("resume", self._cmd_resume))
        self._app.add_handler(CommandHandler("mode", self._cmd_mode))
        self._app.add_handler(CommandHandler("approve", self._cmd_approve))
        self._app.add_handler(CommandHandler("reject", self._cmd_reject))

        # Register inline button callback handler
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))

        # Error handler
        self._app.add_error_handler(self._error_handler)

        # Set bot commands menu
        await self._app.bot.set_my_commands([
            BotCommand("start", "Start bot & welcome"),
            BotCommand("status", "System status & daily P&L"),
            BotCommand("positions", "Open positions"),
            BotCommand("signals", "Pending signals"),
            BotCommand("pnl", "Today's P&L summary"),
            BotCommand("killswitch", "Emergency stop"),
            BotCommand("resume", "Deactivate kill switch"),
            BotCommand("mode", "Switch paper/live mode"),
            BotCommand("approve", "Approve latest (or /approve <id>)"),
            BotCommand("reject", "Reject latest (or /reject <id>)"),
            BotCommand("help", "All commands"),
        ])

        # Clear webhook so polling owns getUpdates (avoids 409 Conflict)
        try:
            await self._app.bot.delete_webhook(drop_pending_updates=True)
            logger.info("Cleared Telegram webhook before polling.")
        except Exception as exc:
            logger.warning("delete_webhook failed (continuing): %s", exc)

        # Start polling
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )
        logger.info("Telegram bot started (polling mode).")

    async def stop(self) -> None:
        """Gracefully stop the bot."""
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            logger.info("Telegram bot stopped.")

    @property
    def application(self) -> Optional[Application]:
        return self._app

    # ── Command Handlers ─────────────────────────────────────────────

    @admin_only
    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        mode = settings.TRADING_MODE
        await update.message.reply_text(
            f"🤖 *Indian F&O Trading Agent*\n\n"
            f"Mode: `{mode}`\n"
            f"Broker: `{settings.BROKER}`\n"
            f"Status: Online ✅\n\n"
            f"Use /help for all commands\\.",
            parse_mode="MarkdownV2",
        )

    @admin_only
    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "📋 *Available Commands*\n\n"
            "/status \\— System status & daily P\\&L\n"
            "/positions \\— Open positions\n"
            "/signals \\— Pending signals\n"
            "/pnl \\— Today's P\\&L summary\n"
            "/killswitch \\— 🚨 Emergency stop\n"
            "/resume \\— Resume after kill switch\n"
            "/mode paper\\|live \\— Switch mode\n"
            "/approve \\[id\\] \\— Approve latest or by id\n"
            "/reject \\[id\\] \\— Reject latest or by id\n"
            "/help \\— This message\n",
            parse_mode="MarkdownV2",
        )

    @admin_only
    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        mode = settings.TRADING_MODE
        broker_connected = "✅" if self.orchestrator and hasattr(self.orchestrator, "broker_adapter") else "❓"
        kill_active = "🔴 ACTIVE" if (self.kill_switch and await self.kill_switch.is_active()) else "🟢 OFF"

        positions_count = 0
        daily_pnl = 0.0
        if self.position_tracker:
            positions_count = len(self.position_tracker.get_open_positions())
            daily_pnl = self.position_tracker.get_daily_pnl()

        pnl_emoji = "🟢" if daily_pnl >= 0 else "🔴"

        await update.message.reply_text(
            f"📊 *System Status*\n\n"
            f"Mode: `{mode}`\n"
            f"Broker: {broker_connected} `{settings.BROKER}`\n"
            f"Kill Switch: {kill_active}\n"
            f"Open Positions: `{positions_count}`\n"
            f"Daily P&L: {pnl_emoji} ₹{daily_pnl:,.2f}\n",
            parse_mode="Markdown",
        )

    @admin_only
    async def _cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.position_tracker:
            await update.message.reply_text("Position tracker not available.")
            return

        positions = self.position_tracker.get_open_positions()
        if not positions:
            await update.message.reply_text("📭 No open positions.")
            return

        lines = ["📊 *Open Positions*\n"]
        for pos in positions:
            pnl = pos.unrealized_pnl or 0
            emoji = "🟢" if pnl >= 0 else "🔴"
            lines.append(
                f"{emoji} `{pos.symbol}` {pos.direction}\n"
                f"   Entry: ₹{pos.entry_price:,.2f} | Current: ₹{pos.current_price or 0:,.2f}\n"
                f"   P&L: ₹{pnl:,.2f} | SL: ₹{pos.stop_loss:,.2f}\n"
            )

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    @admin_only
    async def _cmd_signals(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.orchestrator:
            await update.message.reply_text("Orchestrator not available.")
            return

        pending = getattr(self.orchestrator, "pending_signals", {})
        if not pending:
            await update.message.reply_text("📭 No pending signals.")
            return

        lines = ["⏳ *Pending Signals*\n"]
        for sig_id, sig in list(pending.items())[:10]:
            lines.append(
                f"• `{sig.symbol}` {sig.direction} @ ₹{sig.entry_price:,.2f}\n"
                f"  Strategy: {sig.strategy_name} | Conf: {sig.confidence_score:.0%}\n"
            )

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    @admin_only
    async def _cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        daily_pnl = 0.0
        if self.position_tracker:
            daily_pnl = self.position_tracker.get_daily_pnl()

        emoji = "🟢" if daily_pnl >= 0 else "🔴"
        await update.message.reply_text(
            f"💰 *Today's P&L*\n\n{emoji} ₹{daily_pnl:,.2f}",
            parse_mode="Markdown",
        )

    @admin_only
    async def _cmd_killswitch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.kill_switch:
            await self.kill_switch.activate(reason="Manual Telegram command", actor=f"telegram:{update.effective_user.id}")
            await update.message.reply_text("🚨 *KILL SWITCH ACTIVATED*\nAll trading halted. Use /resume to deactivate.", parse_mode="Markdown")
        else:
            await update.message.reply_text("Kill switch service not available.")

    @admin_only
    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.kill_switch:
            await self.kill_switch.deactivate(actor=f"telegram:{update.effective_user.id}")
            await update.message.reply_text("✅ Kill switch deactivated. Trading resumed.")
        else:
            await update.message.reply_text("Kill switch service not available.")

    @admin_only
    async def _cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args
        if not args or args[0] not in ("paper", "live"):
            await update.message.reply_text("Usage: `/mode paper` or `/mode live`", parse_mode="Markdown")
            return

        new_mode = args[0].upper()
        if new_mode == "LIVE":
            await update.message.reply_text(
                "⚠️ *WARNING*: Switching to LIVE mode.\n"
                "Real orders will be placed. Confirm by sending `/mode live` again.",
                parse_mode="Markdown",
            )
            # In production, implement a confirmation flow
            return

        await update.message.reply_text(f"✅ Mode switched to `{new_mode}`.", parse_mode="Markdown")

    @admin_only
    async def _cmd_approve(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Approve a pending signal by id, or the most recently registered one."""
        await self._cmd_decide(update, context, action="APPROVE")

    @admin_only
    async def _cmd_reject(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Reject a pending signal by id, or the most recently registered one."""
        await self._cmd_decide(update, context, action="REJECT")

    async def _cmd_decide(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        action: str,
    ) -> None:
        from telegram_bot.signal_store import get_signal, get_signal_store, register_signal
        from core.enums import SignalStatus
        from api.paper_book import add_paper_position

        args = context.args or []
        store = get_signal_store()
        signal = None
        signal_id = None

        if args:
            signal_id = args[0].strip()
            signal = get_signal(signal_id)
        elif store:
            # Most recent pending
            signal_id, signal = list(store.items())[-1]

        if signal is None:
            await update.message.reply_text(
                "No pending signal found.\n"
                "Send a sample trade first, then /approve or /approve <signal_id>."
            )
            return

        is_crypto = (
            str(getattr(signal, "exchange", "")).upper() == "DELTA"
            or "BTC" in str(signal.symbol).upper()
        )
        currency = "$" if is_crypto else "₹"
        lev = (getattr(signal, "indicators_snapshot", None) or {}).get("leverage")
        lev_txt = f" @ {float(lev):.0f}x" if lev else ""

        if action == "APPROVE":
            try:
                add_paper_position(signal)
                register_signal(
                    signal.model_copy(update={"status": SignalStatus.APPROVED})
                    if hasattr(signal, "model_copy")
                    else signal
                )
                await update.message.reply_text(
                    f"✅ APPROVED {signal.symbol}\n"
                    f"PAPER_ORDER_EXECUTED: {signal.quantity} qty @ {currency}{signal.entry_price:.2f}{lev_txt}\n"
                    f"id: {signal_id}"
                )
            except Exception as exc:
                await update.message.reply_text(f"Approve failed: {exc}")
        else:
            try:
                if hasattr(signal, "model_copy"):
                    register_signal(signal.model_copy(update={"status": SignalStatus.REJECTED}))
                await update.message.reply_text(f"❌ REJECTED {signal.symbol}\nid: {signal_id}")
            except Exception as exc:
                await update.message.reply_text(f"Reject failed: {exc}")

    # ── Callback Query Router ────────────────────────────────────────

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Route inline button callbacks to the correct handler."""
        query = update.callback_query
        if not query or not query.data:
            return

        # Verify admin — allow either matching user id or matching chat id
        # (private chats: user_id == chat_id; groups: chat is negative).
        user_id = query.from_user.id if query.from_user else None
        chat_id = update.effective_chat.id if update.effective_chat else None
        allowed_raw = settings.TELEGRAM_CHAT_ID
        allowed = int(allowed_raw) if allowed_raw and str(allowed_raw).lstrip("-").isdigit() else None
        if allowed is not None and user_id != allowed and chat_id != allowed:
            await query.answer("⛔ Unauthorized.", show_alert=True)
            logger.warning(
                "Unauthorized callback user_id=%s chat_id=%s allowed=%s",
                user_id,
                chat_id,
                allowed,
            )
            return

        # Ensure callback handlers always see the shared store + orchestrator
        context.bot_data["signal_store"] = self._signal_store
        context.bot_data["orchestrator"] = self.orchestrator
        context.bot_data.setdefault("admin_id", allowed or 0)

        data = query.data
        try:
            if data.startswith("approve:"):
                await handle_approve(update, context, self.orchestrator)
            elif data.startswith("reject:"):
                await handle_reject(update, context, self.orchestrator)
            elif data.startswith("half_size:"):
                await handle_half_size(update, context, self.orchestrator)
            elif data.startswith("block:"):
                await handle_block_similar(update, context, self.orchestrator)
            else:
                await query.answer("Unknown action.")
        except Exception as exc:
            logger.exception("Callback handler failed for data=%s: %s", data, exc)
            try:
                await query.answer(f"Error: {str(exc)[:160]}", show_alert=True)
            except Exception:
                pass

    # ── Error Handler ────────────────────────────────────────────────

    async def _error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Global error handler for the bot."""
        logger.error(f"Telegram bot error: {context.error}", exc_info=context.error)
        if isinstance(update, Update) and update.effective_chat:
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"⚠️ Bot error: {str(context.error)[:200]}",
                )
            except Exception:
                pass
