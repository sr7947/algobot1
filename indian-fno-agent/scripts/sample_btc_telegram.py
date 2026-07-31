#!/usr/bin/env python3
"""
Start a conflict-tolerant Telegram poller, send a BTCUSD sample trade,
and handle Approve/Reject callbacks.

If another process is also calling getUpdates on the same bot token,
Telegram returns 409 Conflict. This script retries aggressively until it
owns polling, then keeps handling button presses.

Usage:
    PYTHONPATH=. python3 scripts/sample_btc_telegram.py
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("sample_btc_telegram")


async def _build_btc_signal():
    from core.models import TradeSignal, MarketRegime, SignalStatus
    from config.settings import get_settings
    from risk.delta_margin import (
        estimate_position_margin,
        get_default_product_spec,
        initial_margin_pct_for_leverage,
    )

    settings = get_settings()
    leverage = float(settings.DELTA_DEFAULT_LEVERAGE)
    entry, sl, target, qty = 65200.00, 63500.00, 68600.00, 1
    spec = get_default_product_spec("BTCUSD")
    im_pct = initial_margin_pct_for_leverage(leverage, spec, size=qty)
    margin = estimate_position_margin(qty, entry, leverage, product=spec)

    rationale = [
        f"Order leverage {leverage:.0f}x on Delta India (IM {im_pct:.2f}% → margin ~${margin:.2f})",
        "BTCUSD broke above key 4H resistance at $65,000",
        "Open Interest on Delta Exchange +14% with strong buying volume",
        "RSI momentum bullish at 62 with MACD histogram expansion",
        "Reply /approve if the button does not respond",
    ]

    try:
        from agents.gemini_reasoner import GeminiReasoningEngine

        ai_res = await GeminiReasoningEngine().evaluate_trade_signal(
            symbol="BTCUSD",
            direction="BUY",
            entry_price=entry,
            stop_loss=sl,
            target=target,
            technical_indicators={"EMA_Bull_Stack": True, "RSI": 62},
            rationale=rationale,
            news_summary="Bitcoin ETF net inflows reach $450M",
        )
        verdict = f"🤖 Gemini AI Verdict: [{ai_res.get('verdict', 'APPROVE')}]"
        final_rationale = [verdict] + ai_res.get("ai_rationale", rationale)
    except Exception as exc:
        logger.warning("Gemini skipped: %s", exc)
        final_rationale = ["🤖 Gemini AI Verdict: [APPROVE] (offline fallback)"] + rationale

    signal = TradeSignal(
        id=uuid4(),
        created_at=datetime.now(timezone.utc),
        strategy_name="Crypto Trend Breakout",
        symbol="BTCUSD",
        exchange="DELTA",
        instrument_type="FUT",
        direction="BUY",
        entry_price=entry,
        stop_loss=sl,
        target=target,
        quantity=qty,
        lot_size=qty,
        confidence_score=0.82,
        regime=MarketRegime.TRENDING_BULL.value,
        rationale=final_rationale,
        news_summary="Bitcoin ETF net inflows reach $450M; Fed signals upcoming rate cuts",
        indicators_snapshot={"leverage": leverage, "margin": margin, "im_pct": im_pct},
        status=SignalStatus.PENDING_APPROVAL.value,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    return signal, leverage, margin


async def _handle_callback_query(bot, query: dict) -> None:
    """Process one Approve/Reject callback without PTB Application."""
    from telegram import InlineKeyboardMarkup
    from telegram_bot.signal_store import get_signal, register_signal
    from core.enums import SignalStatus
    from api.paper_book import add_paper_position

    cq_id = query.get("id")
    data = query.get("data") or ""
    msg = query.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    from_user = (query.get("from") or {}).get("id")

    from config.settings import get_settings
    allowed = get_settings().TELEGRAM_CHAT_ID
    allowed_i = int(allowed) if allowed and str(allowed).lstrip("-").isdigit() else None
    if allowed_i is not None and from_user != allowed_i and chat_id != allowed_i:
        await bot.answer_callback_query(cq_id, text="⛔ Unauthorized.", show_alert=True)
        return

    if ":" not in data:
        await bot.answer_callback_query(cq_id, text="Unknown action.")
        return

    action, signal_id = data.split(":", 1)
    signal = get_signal(signal_id)
    if signal is None:
        await bot.answer_callback_query(
            cq_id,
            text="Signal not found. Re-send sample while this bot is running.",
            show_alert=True,
        )
        return

    await bot.answer_callback_query(cq_id)

    is_crypto = str(getattr(signal, "exchange", "")).upper() == "DELTA"
    currency = "$" if is_crypto else "₹"
    lev = (getattr(signal, "indicators_snapshot", None) or {}).get("leverage")
    lev_txt = f" @ {float(lev):.0f}x" if lev else ""

    if action in ("approve", "half_size"):
        qty = signal.quantity
        if action == "half_size":
            qty = max(1, qty // 2)
            if hasattr(signal, "model_copy"):
                signal = signal.model_copy(update={"quantity": qty})
        try:
            add_paper_position(signal)
            status_txt = (
                f"PAPER_ORDER_EXECUTED: {qty} qty @ {currency}{signal.entry_price:.2f}{lev_txt}"
            )
            register_signal(
                signal.model_copy(update={"status": SignalStatus.APPROVED})
                if hasattr(signal, "model_copy")
                else signal
            )
            text = f"✅ APPROVED — {signal.symbol}\n🟢 {status_txt}"
        except Exception as exc:
            text = f"✅ Approved but position failed: {exc}"
    elif action == "reject":
        register_signal(
            signal.model_copy(update={"status": SignalStatus.REJECTED})
            if hasattr(signal, "model_copy")
            else signal
        )
        text = f"❌ REJECTED — {signal.symbol}"
    elif action == "block":
        text = f"🚫 BLOCKED TODAY — {signal.symbol}"
    else:
        text = f"Unknown action: {action}"

    if chat_id and message_id:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=InlineKeyboardMarkup([]),
        )
    logger.info("Handled %s for signal %s", action, signal_id)


async def _handle_command(bot, message: dict) -> None:
    """Handle /approve and /reject text commands."""
    from telegram_bot.signal_store import get_signal, get_signal_store, register_signal
    from core.enums import SignalStatus
    from api.paper_book import add_paper_position

    text = (message.get("text") or "").strip()
    chat_id = (message.get("chat") or {}).get("id")
    if not text.startswith("/"):
        return

    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    if cmd not in ("/approve", "/reject"):
        return

    store = get_signal_store()
    signal = None
    signal_id = None
    if len(parts) > 1:
        signal_id = parts[1]
        signal = get_signal(signal_id)
    elif store:
        signal_id, signal = list(store.items())[-1]

    if signal is None:
        await bot.send_message(chat_id=chat_id, text="No pending signal. Send a sample first.")
        return

    if cmd == "/approve":
        add_paper_position(signal)
        register_signal(
            signal.model_copy(update={"status": SignalStatus.APPROVED})
            if hasattr(signal, "model_copy")
            else signal
        )
        lev = (getattr(signal, "indicators_snapshot", None) or {}).get("leverage")
        lev_txt = f" @ {float(lev):.0f}x" if lev else ""
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ APPROVED {signal.symbol}\n"
                f"PAPER_ORDER_EXECUTED: {signal.quantity} qty @ ${signal.entry_price:.2f}{lev_txt}"
            ),
        )
    else:
        register_signal(
            signal.model_copy(update={"status": SignalStatus.REJECTED})
            if hasattr(signal, "model_copy")
            else signal
        )
        await bot.send_message(chat_id=chat_id, text=f"❌ REJECTED {signal.symbol}")


async def poll_forever(bot, stop_event: asyncio.Event) -> None:
    """Short-poll getUpdates with backoff on 409 Conflict."""
    from telegram.error import Conflict, TelegramError

    offset = None
    conflict_streak = 0
    while not stop_event.is_set():
        try:
            updates = await bot.get_updates(
                offset=offset,
                timeout=20,
                allowed_updates=["callback_query", "message"],
            )
            conflict_streak = 0
            for upd in updates:
                offset = upd.update_id + 1
                if upd.callback_query:
                    await _handle_callback_query(bot, upd.callback_query.to_dict())
                elif upd.message:
                    await _handle_command(bot, upd.message.to_dict())
        except Conflict:
            conflict_streak += 1
            wait = min(30, 2 * conflict_streak)
            logger.warning(
                "getUpdates conflict (another bot is polling this token). "
                "Retrying in %ss — stop other instances for reliable Approve.",
                wait,
            )
            try:
                await bot.delete_webhook(drop_pending_updates=False)
            except Exception:
                pass
            await asyncio.sleep(wait)
        except TelegramError as exc:
            logger.error("Telegram poll error: %s", exc)
            await asyncio.sleep(3)
        except Exception as exc:
            logger.exception("Unexpected poll error: %s", exc)
            await asyncio.sleep(3)


async def main() -> None:
    from telegram import Bot
    from config.settings import get_settings
    from telegram_bot.notifier import TelegramNotifier
    from telegram_bot.signal_store import register_signal, pending_count

    settings = get_settings()
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        raise SystemExit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID must be set in .env")

    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as exc:
        logger.warning("delete_webhook: %s", exc)

    trade_signal, leverage, margin = await _build_btc_signal()
    register_signal(trade_signal)

    notifier = TelegramNotifier()
    msg_id = await notifier.send_trade_card(
        trade_signal,
        {"capital": 200.0, "leverage": leverage, "asset_class": "CRYPTO"},
    )
    if not msg_id:
        raise SystemExit("Telegram send failed")

    print(
        f"\n✅ Sample BTCUSD trade sent (message_id={msg_id}).\n"
        f"   Signal: {trade_signal.id}\n"
        f"   Leverage: {leverage:.0f}x | Margin: ${margin:.4f} | pending={pending_count()}\n"
        f"   Tap APPROVE on the card — or send /approve\n"
        f"   Keep this process running. Press Ctrl+C to stop.\n"
        f"   If Approve does nothing: stop any OTHER bot using this token, then retry.\n"
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await poll_forever(bot, stop_event)
    logger.info("Stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
