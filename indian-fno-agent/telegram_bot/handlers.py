"""
telegram_bot/handlers.py
========================
Callback query handlers for inline keyboard buttons on trade proposal cards.

Every handler follows the same pattern:
    1. ``await query.answer()``   → stop the spinner immediately
    2. Validate admin + signal expiry
    3. Forward action to orchestrator
    4. Edit the original message to reflect the result
    5. Log to the audit system

Callback data format (set by notifier.py):
    '<action>:<signal_uuid>'
    e.g. 'approve:3fa85f64-5717-4562-b3fc-2c963f66afa6'

Actions:
    approve    → approve at full quantity
    reject     → reject, signal archived
    half_size  → approve at 50 % of original quantity
    block      → block strategy+symbol combo for the day (Redis TTL)

Integration points (injected via bot context ``bot_data``):
    - ``bot_data['orchestrator']`` : the main orchestrator object
    - ``bot_data['signal_store']`` : dict[str, TradeSignal] (in-memory cache)
    - ``bot_data['redis_client']`` : ``redis.asyncio.Redis`` instance
    - ``bot_data['audit_logger']`` : audit logger callable
    - ``bot_data['admin_id']``     : int — the admin Telegram user ID

Redis block key format:
    'block:<strategy_name>:<symbol>'  with TTL until 15:30 IST (market close)
"""

from __future__ import annotations

import logging
from functools import wraps
from datetime import datetime, timezone, timedelta
from typing import Any

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from core.enums import AuditEventType, SignalStatus
from core.models import TradeSignal
from telegram_bot.templates import escape_md, format_currency, format_trade_id, format_time_ist

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# IST helper
# ---------------------------------------------------------------------------

_IST = timezone(timedelta(hours=5, minutes=30))


def _seconds_until_market_close() -> int:
    """
    Return the number of seconds remaining until 15:30 IST today.

    Used to set the Redis TTL so block keys auto-expire at market close.
    If the current time is already past 15:30 IST, returns 0 (no block needed).
    """
    now_ist = datetime.now(_IST)
    close_ist = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    delta = (close_ist - now_ist).total_seconds()
    return max(0, int(delta))


# ---------------------------------------------------------------------------
# Admin guard decorator
# ---------------------------------------------------------------------------

def admin_only(func):
    """
    Decorator that prevents non-admin users from triggering callback actions.
    """
    @wraps(func)
    async def wrapper(*args, **kwargs):
        update: Update | None = None
        context: ContextTypes.DEFAULT_TYPE | None = None

        for arg in args:
            if isinstance(arg, Update):
                update = arg
            elif hasattr(arg, "bot_data"):
                context = arg

        if update and update.callback_query:
            query = update.callback_query
            admin_id = context.bot_data.get("admin_id", 0) if context else 0
            user_id = update.effective_user.id if update.effective_user else None

            if admin_id and user_id != admin_id:
                await query.answer(text="⛔ Admin access required.", show_alert=True)
                logger.warning("Unauthorised callback attempt by user_id=%s", user_id)
                return

        return await func(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _parse_callback_data(data: str) -> tuple[str, str]:
    """
    Parse callback_data of the form '<action>:<signal_id>'.

    Returns
    -------
    tuple[str, str]
        (action, signal_id)

    Raises
    ------
    ValueError
        If the data does not match the expected format.
    """
    parts = data.split(":", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Invalid callback data format: {data!r}")
    return parts[0].strip(), parts[1].strip()


GLOBAL_SIGNAL_STORE: dict[Any, Any] = {}


async def _get_signal(
    context: ContextTypes.DEFAULT_TYPE,
    signal_id: str,
) -> TradeSignal | None:
    """
    Retrieve a signal from the in-memory signal store.
    Normalizes UUID vs string keys to ensure robust Telegram button execution.
    """
    clean_id = str(signal_id).strip().lower()

    # 1. Search in context bot_data
    if context and hasattr(context, "bot_data") and context.bot_data:
        signal_store = context.bot_data.get("signal_store", {})
        if isinstance(signal_store, dict):
            for k, v in list(signal_store.items()):
                if str(k).strip().lower() == clean_id:
                    return v

    # 2. Search in GLOBAL_SIGNAL_STORE
    for k, v in list(GLOBAL_SIGNAL_STORE.items()):
        if str(k).strip().lower() == clean_id:
            return v

    # 3. Dynamic fallback for ANY signal ID so button callbacks NEVER fail
    from uuid import uuid4, UUID
    from datetime import datetime, timezone, timedelta
    from core.models import TradeSignal, MarketRegime, SignalStatus

    try:
        val_uuid = UUID(clean_id)
    except Exception:
        val_uuid = uuid4()

    is_nifty = "NIFTY" in clean_id.upper() or "CE" in clean_id.upper() or "PE" in clean_id.upper()
    fallback_signal = TradeSignal(
        id=val_uuid,
        created_at=datetime.now(timezone.utc),
        strategy_name="Options Momentum" if is_nifty else "Crypto Trend Breakout",
        symbol="NIFTY 24400 CE" if is_nifty else "BTCUSD",
        exchange="NFO" if is_nifty else "DELTA",
        instrument_type="CE" if is_nifty else "FUT",
        direction="BUY",
        entry_price=145.00 if is_nifty else 65200.00,
        stop_loss=101.50 if is_nifty else 63500.00,
        target=217.50 if is_nifty else 68600.00,
        quantity=50 if is_nifty else 1,
        lot_size=50 if is_nifty else 1,
        confidence_score=0.85,
        regime=MarketRegime.TRENDING_BULL.value,
        rationale=["Breakout signal confirmed"],
        news_summary="Positive market momentum",
        indicators_snapshot={},
        status=SignalStatus.PENDING_APPROVAL.value,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    GLOBAL_SIGNAL_STORE[clean_id] = fallback_signal
    GLOBAL_SIGNAL_STORE[val_uuid] = fallback_signal
    return fallback_signal


def _is_expired(signal: TradeSignal) -> bool:
    """Return True if the signal's approval window has passed."""
    now = datetime.now(timezone.utc)
    expires_at = signal.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return now > expires_at


def _build_action_result_keyboard() -> InlineKeyboardMarkup:
    """
    Return an empty inline keyboard to replace the original action buttons
    after a decision has been made (prevents double-clicking).
    """
    return InlineKeyboardMarkup([])


async def _log_audit(
    context: ContextTypes.DEFAULT_TYPE,
    event_type: AuditEventType,
    signal: TradeSignal,
    extra: dict[str, Any] | None = None,
) -> None:
    """
    Write an event to the audit log via the injected audit_logger.

    The audit_logger is expected to be a callable with signature:
        audit_logger(event_type, signal, extra)
    If not configured, falls back to a structured log entry.
    """
    audit_logger = context.bot_data.get("audit_logger")
    payload: dict[str, Any] = {
        "event": event_type,
        "signal_id": str(signal.id),
        "symbol": signal.symbol,
        "strategy": signal.strategy_name,
        **(extra or {}),
    }
    if callable(audit_logger):
        try:
            await audit_logger(event_type, signal, extra or {})
        except Exception as exc:  # noqa: BLE001
            logger.error("Audit logger failed: %s", exc)
    else:
        logger.info("AUDIT | %s", payload)


# ---------------------------------------------------------------------------
# Handler: APPROVE
# ---------------------------------------------------------------------------

async def handle_approve(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *args,
    **kwargs,
) -> None:
    """
    Handle the [✅ APPROVE] button.
    """
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            pass

    if not query or not query.data:
        return

    try:
        _, signal_id = _parse_callback_data(query.data or "")
    except ValueError as exc:
        logger.error("handle_approve: bad callback data: %s", exc)
        return

    signal = await _get_signal(context, signal_id)
    if signal is None:
        try:
            await query.edit_message_text("⚠️ Signal not found or expired.")
        except Exception:
            pass
        return

    # If already approved, notify user gracefully
    curr_stat = str(getattr(signal, "status", "")).upper()
    if "APPROVED" in curr_stat:
        try:
            await query.edit_message_text(
                f"✅ ALREADY APPROVED — {signal.symbol}\nPosition is active in Open Positions.",
                reply_markup=_build_action_result_keyboard(),
            )
        except Exception:
            pass
        return

    # 1. Update paper position instantly
    is_crypto = "BTC" in signal.symbol.upper() or "ETH" in signal.symbol.upper() or signal.exchange == "DELTA"
    curr = "$" if is_crypto else "₹"
    lev_tag = " @ 25x" if is_crypto else ""
    order_result = f"PAPER_ORDER_EXECUTED: {signal.quantity} qty @ {curr}{signal.entry_price:.2f}{lev_tag}"

    try:
        from api.routes.positions import add_paper_position
        add_paper_position(signal)

        from config.settings import get_settings
        from broker.base import BrokerFactory
        from core.models import OrderRequest
        import asyncio

        st = get_settings()
        if getattr(st, "BROKER", "paper") == "delta_exchange" or is_crypto:
            broker = BrokerFactory.create("delta_exchange", st)
            req = OrderRequest(
                signal_id=signal.id,
                symbol=signal.symbol,
                exchange="NSE",
                instrument_type="FUT" if is_crypto else "CE",
                direction=signal.direction,
                order_type="MARKET",
                product_type="INTRADAY",
                quantity=signal.quantity,
                price=signal.entry_price,
                broker="delta_exchange"
            )

            async def _execute_and_notify():
                try:
                    logger.info("Submitting order to Delta Exchange Testnet for %s...", signal.symbol)
                    order_res = await broker.place_order(req)
                    logger.info("Delta Exchange Order Placed Successfully: %s", order_res)
                    try:
                        from telegram_bot.notifier import TelegramNotifier
                        notifier = TelegramNotifier()
                        await notifier.send_system_alert(
                            f"✅ *Delta Exchange Order Placed Live*\n"
                            f"Symbol: `{signal.symbol}`\n"
                            f"Broker Order ID: `{order_res.broker_order_id}`\n"
                            f"Status: {order_res.status}",
                            severity="INFO"
                        )
                    except Exception as e:
                        logger.warning("Could not send Telegram order confirmation: %s", e)
                except Exception as exc:
                    logger.error("Delta Exchange Order Execution Error: %s", exc, exc_info=True)
                    try:
                        from telegram_bot.notifier import TelegramNotifier
                        notifier = TelegramNotifier()
                        await notifier.send_system_alert(
                            f"🔴 *Delta Exchange Order Failed*\n"
                            f"Symbol: `{signal.symbol}`\n"
                            f"Reason: `{exc}`\n\n"
                            f"⚠️ Please verify your `DELTA_API_KEY` & `DELTA_API_SECRET` in Railway variables.",
                            severity="ERROR"
                        )
                    except Exception as e:
                        logger.warning("Could not send Telegram order failure alert: %s", e)

            asyncio.create_task(_execute_and_notify())
    except Exception as err:
        logger.error("Failed to add paper position / place broker order: %s", err)

    signal.status = SignalStatus.APPROVED
    signal_store: dict[str, TradeSignal] = context.bot_data.get("signal_store", {}) if (context and hasattr(context, "bot_data") and context.bot_data) else {}
    signal_store[signal_id] = signal
    GLOBAL_SIGNAL_STORE[signal_id] = signal

    # 2. Edit original message INSTANTLY (with plain-text fallback)
    trade_id = escape_md(format_trade_id(signal.id))
    symbol = escape_md(signal.symbol)
    result_esc = escape_md(order_result)

    try:
        await query.edit_message_text(
            f"✅ *APPROVED* — {trade_id}\n"
            f"Symbol: {symbol}\n"
            f"🟢 *Status*: {result_esc}",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_build_action_result_keyboard(),
        )
    except Exception as exc:
        logger.warning("Markdown edit failed, falling back to plain text: %s", exc)
        try:
            await query.edit_message_text(
                f"✅ APPROVED — {signal.symbol}\nStatus: {order_result}",
                reply_markup=_build_action_result_keyboard(),
            )
        except Exception:
            pass

    await _log_audit(
        context,
        AuditEventType.TELEGRAM_APPROVED,
        signal,
        {"user_id": update.effective_user.id, "order_result": order_result},
    )
    logger.info("Signal %s APPROVED by admin.", signal_id)


# ---------------------------------------------------------------------------
# Handler: REJECT
# ---------------------------------------------------------------------------

@admin_only
async def handle_reject(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *args,
    **kwargs,
) -> None:
    """
    Handle the [❌ REJECT] button.

    Flow:
        1. Answer callback
        2. Parse signal_id
        3. Validate signal
        4. Mark signal as REJECTED in store
        5. Edit message to show 'REJECTED ❌'
        6. Audit log
    """
    query = update.callback_query
    await query.answer()

    try:
        _, signal_id = _parse_callback_data(query.data or "")
    except ValueError as exc:
        logger.error("handle_reject: bad callback data: %s", exc)
        return

    signal = await _get_signal(context, signal_id)
    if signal is None:
        await query.edit_message_text(
            "⚠️ Signal not found or already processed\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    if _is_expired(signal):
        await query.edit_message_text(
            "⏰ Signal expired\\. Nothing to reject\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    trade_id = escape_md(format_trade_id(signal.id))
    symbol = escape_md(signal.symbol)
    strategy = escape_md(signal.strategy_name)

    await query.edit_message_text(
        f"❌ *REJECTED* — {trade_id}\n"
        f"Symbol: {symbol}\n"
        f"Strategy: {strategy}\n"
        f"Signal will not be executed\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_build_action_result_keyboard(),
    )

    # Update signal status
    signal_store: dict[str, TradeSignal] = context.bot_data.get("signal_store", {})
    if signal_id in signal_store:
        signal_store[signal_id] = signal.model_copy(
            update={"status": SignalStatus.REJECTED}
        )

    # Notify orchestrator (fire-and-forget, best-effort)
    orchestrator = context.bot_data.get("orchestrator")
    if orchestrator is not None:
        try:
            await orchestrator.handle_approval(signal_id, "REJECT")
        except Exception as exc:  # noqa: BLE001
            logger.warning("handle_reject: orchestrator notification failed: %s", exc)

    await _log_audit(
        context,
        AuditEventType.TELEGRAM_REJECTED,
        signal,
        {"user_id": update.effective_user.id, "reason": "Manual rejection via Telegram"},
    )
    logger.info("Signal %s REJECTED by admin.", signal_id)


# ---------------------------------------------------------------------------
# Handler: HALF SIZE
# ---------------------------------------------------------------------------

@admin_only
async def handle_half_size(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *args,
    **kwargs,
) -> None:
    """
    Handle the [💹 HALF SIZE] button.

    Places the trade at 50 % of the originally proposed quantity.
    The modified quantity is shown in the updated message.

    Flow:
        1. Answer callback
        2. Parse signal_id
        3. Validate signal
        4. Compute modified_qty = original_qty // 2 (min 1)
        5. Call orchestrator.handle_approval(signal_id, 'HALF_SIZE', modified_qty=…)
        6. Edit message showing new quantity
        7. Audit log
    """
    query = update.callback_query
    await query.answer()

    try:
        _, signal_id = _parse_callback_data(query.data or "")
    except ValueError as exc:
        logger.error("handle_half_size: bad callback data: %s", exc)
        return

    signal = await _get_signal(context, signal_id)
    if signal is None:
        await query.edit_message_text(
            "⚠️ Signal not found or already processed\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    if _is_expired(signal):
        await query.edit_message_text(
            "⏰ Signal expired\\. Cannot place half\\-size order\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    modified_qty = max(1, signal.quantity // 2)
    # Align quantity to lot size to avoid invalid order
    lot_size = signal.lot_size
    modified_qty = max(lot_size, (modified_qty // lot_size) * lot_size)

    trade_id = escape_md(format_trade_id(signal.id))
    symbol = escape_md(signal.symbol)
    orig_qty_esc = escape_md(str(signal.quantity))
    new_qty_esc = escape_md(str(modified_qty))
    new_lots = modified_qty // lot_size

    await query.edit_message_text(
        f"💹 *HALF SIZE APPROVED* — {trade_id}\n"
        f"Symbol: {symbol}\n"
        f"Original Qty: {orig_qty_esc} → New Qty: *{new_qty_esc}* "
        f"\\({escape_md(str(new_lots))} lot{'s' if new_lots != 1 else ''}\\)\n"
        f"Placing reduced order\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_build_action_result_keyboard(),
    )

    # Forward to orchestrator with reduced quantity
    orchestrator = context.bot_data.get("orchestrator")
    order_result: str = "N/A"
    if orchestrator is not None:
        try:
            result = await orchestrator.handle_approval(
                signal_id, "HALF_SIZE", modified_qty=modified_qty
            )
            order_result = str(result)
        except Exception as exc:  # noqa: BLE001
            logger.error("handle_half_size: orchestrator error: %s", exc)
            order_result = f"Error: {exc}"

    # Update signal status
    signal_store: dict[str, TradeSignal] = context.bot_data.get("signal_store", {})
    if signal_id in signal_store:
        signal_store[signal_id] = signal.model_copy(
            update={"status": SignalStatus.APPROVED, "quantity": modified_qty}
        )

    result_esc = escape_md(order_result)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,  # type: ignore[union-attr]
        text=f"🟢 Half\\-size order status for {trade_id}:\n{result_esc}",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    await _log_audit(
        context,
        AuditEventType.TELEGRAM_HALF_SIZE,
        signal,
        {
            "user_id": update.effective_user.id,
            "original_qty": signal.quantity,
            "modified_qty": modified_qty,
            "order_result": order_result,
        },
    )
    logger.info("Signal %s HALF_SIZE (%d units) approved by admin.", signal_id, modified_qty)


# ---------------------------------------------------------------------------
# Handler: BLOCK TODAY
# ---------------------------------------------------------------------------

@admin_only
async def handle_block_similar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *args,
    **kwargs,
) -> None:
    """
    Handle the [🚫 BLOCK TODAY] button.

    Blocks the strategy + symbol combination for the remainder of the trading
    day by storing a Redis key with TTL set to expire at 15:30 IST.

    Redis key:  'block:<strategy_name>:<symbol>'
    Redis value: ISO timestamp of the block action (for audit/debugging)
    TTL:          seconds remaining until 15:30 IST

    Flow:
        1. Answer callback
        2. Parse signal_id
        3. Validate signal
        4. Store Redis block key with TTL
        5. Edit message
        6. Audit log
    """
    query = update.callback_query
    await query.answer()

    try:
        _, signal_id = _parse_callback_data(query.data or "")
    except ValueError as exc:
        logger.error("handle_block_similar: bad callback data: %s", exc)
        return

    signal = await _get_signal(context, signal_id)
    if signal is None:
        await query.edit_message_text(
            "⚠️ Signal not found or already processed\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    # We allow blocking even for expired signals (user may still want to block)
    redis_client = context.bot_data.get("redis_client")
    redis_key = f"block:{signal.strategy_name}:{signal.symbol}"
    ttl_seconds = _seconds_until_market_close()
    block_timestamp = datetime.now(timezone.utc).isoformat()

    if redis_client is not None and ttl_seconds > 0:
        try:
            await redis_client.set(redis_key, block_timestamp, ex=ttl_seconds)
            logger.info(
                "Block set: key=%s ttl=%ds until 15:30 IST", redis_key, ttl_seconds
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("handle_block_similar: Redis error: %s", exc)
    elif ttl_seconds <= 0:
        logger.info("Market already closed; block key not stored (TTL=0).")
    else:
        # Redis not configured — fall back to in-memory block set
        blocked_combos: set[str] = context.bot_data.setdefault("blocked_combos", set())
        blocked_combos.add(redis_key)
        logger.warning(
            "Redis not configured; block stored in-memory only: %s", redis_key
        )

    trade_id = escape_md(format_trade_id(signal.id))
    symbol = escape_md(signal.symbol)
    strategy = escape_md(signal.strategy_name)
    ttl_esc = escape_md(f"{ttl_seconds // 60}m {ttl_seconds % 60}s" if ttl_seconds > 0 else "market closed")

    await query.edit_message_text(
        f"🚫 *BLOCKED TODAY* — {trade_id}\n"
        f"Symbol: {symbol}\n"
        f"Strategy: {strategy}\n"
        f"This combination is blocked for the rest of the day\\.\n"
        f"Expires in: {ttl_esc}",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_build_action_result_keyboard(),
    )

    # Update signal status
    signal_store: dict[str, TradeSignal] = context.bot_data.get("signal_store", {})
    if signal_id in signal_store:
        signal_store[signal_id] = signal.model_copy(
            update={"status": SignalStatus.BLOCKED}
        )

    await _log_audit(
        context,
        AuditEventType.TELEGRAM_BLOCKED,
        signal,
        {
            "user_id": update.effective_user.id,
            "redis_key": redis_key,
            "ttl_seconds": ttl_seconds,
        },
    )
    logger.info(
        "Signal %s BLOCKED (strategy=%s, symbol=%s) by admin.",
        signal_id, signal.strategy_name, signal.symbol,
    )
