"""
telegram_bot/notifier.py
========================
TelegramNotifier — sends all outbound Telegram messages from the trading agent.

Architecture
------------
- Every public method is ``async`` and should be awaited from async contexts
  (orchestrator, position monitor, etc.).
- The bot token and chat ID are read from environment variables at construction
  time.  The class does NOT own the ``Application`` lifecycle; it only holds a
  reference to the ``Bot`` object so that it can call ``send_message`` and
  ``edit_message_text`` independently of the polling loop.
- All user-visible strings use Telegram MarkdownV2 formatting.  Every dynamic
  value is run through ``escape_md`` before interpolation.
- The inline keyboard callback_data format is always ``'action:signal_id'``
  (e.g. ``'approve:3fa85f64-...'``), which is parsed by handlers.py.

Thread-safety
-------------
python-telegram-bot's ``Bot`` object is async-safe; multiple coroutines can
call it concurrently.  No extra locking is required.
"""

from __future__ import annotations

import logging
import os
from config.settings import get_settings
from datetime import datetime, timezone
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError

from core.enums import MarketRegime
from core.models import OrderResponse, Position, RiskState, Trade, TradeSignal
from telegram_bot.templates import (
    escape_md,
    format_confidence_bar,
    format_currency,
    format_direction,
    format_pct_change,
    format_regime,
    format_risk_reward,
    format_sl_pct,
    format_target_pct,
    format_time_ist,
    format_trade_id,
)

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """
    Sends structured, MarkdownV2-formatted Telegram messages for all
    significant trading agent events.

    Parameters
    ----------
    bot_token:
        Telegram Bot API token.  Falls back to the ``TELEGRAM_BOT_TOKEN``
        environment variable if not supplied.
    chat_id:
        The Telegram chat / user ID to send messages to.  Falls back to
        ``TELEGRAM_CHAT_ID`` env var.

    Attributes
    ----------
    _bot:
        The underlying ``telegram.Bot`` instance used for all API calls.
    _chat_id:
        Destination chat ID (coerced to ``int``).
    """

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: int | str | None = None,
    ) -> None:
        token = bot_token or get_settings().TELEGRAM_BOT_TOKEN
        self._chat_id = int(chat_id or get_settings().TELEGRAM_CHAT_ID)
        self._bot: Bot = Bot(token=token)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _send(
        self,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        parse_mode: str = ParseMode.MARKDOWN_V2,
    ) -> int | None:
        """
        Send a message to the admin chat.

        Returns the ``message_id`` of the sent message, or ``None`` if the
        send fails (error is logged but not re-raised so the agent keeps running).
        """
        try:
            msg = await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            return msg.message_id
        except TelegramError as exc:
            logger.error("TelegramNotifier._send failed: %s", exc)
            return None

    @staticmethod
    def _trade_card_keyboard(signal_id: str) -> InlineKeyboardMarkup:
        """
        Build the four-button inline keyboard for a trade proposal card.

        Callback data format: '<action>:<signal_id>'
        """
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ APPROVE",    callback_data=f"approve:{signal_id}"),
                    InlineKeyboardButton("❌ REJECT",     callback_data=f"reject:{signal_id}"),
                ],
                [
                    InlineKeyboardButton("💹 HALF SIZE",  callback_data=f"half_size:{signal_id}"),
                    InlineKeyboardButton("🚫 BLOCK TODAY", callback_data=f"block:{signal_id}"),
                ],
            ]
        )

    # ------------------------------------------------------------------
    # Trade proposal card
    # ------------------------------------------------------------------

    async def send_trade_card(
        self,
        signal: TradeSignal,
        agent_context: dict[str, Any],
    ) -> int | None:
        """
        Send a rich trade proposal card with an inline approval keyboard.

        Parameters
        ----------
        signal:
            The ``TradeSignal`` to present to the operator.
        agent_context:
            Supplementary context dict.  Recognised keys:

            - ``capital`` (float): total trading capital in INR (used to
              compute max-risk percentage)
            - ``strategy_version`` (str): version string for the strategy

        Returns
        -------
        int | None
            Telegram message ID of the sent card, or ``None`` on failure.
            Store this in ``signal.telegram_message_id`` for later edits.
        """
        signal_id = str(signal.id)
        capital: float = float(agent_context.get("capital", 0) or 0)
        strategy_version: str = str(agent_context.get("strategy_version", "1.0"))
        is_crypto = (
            str(agent_context.get("asset_class", "")).upper() == "CRYPTO"
            or str(signal.exchange).upper() == "DELTA"
            or "BTC" in signal.symbol.upper()
            or "ETH" in signal.symbol.upper()
        )
        currency = "$" if is_crypto else "₹"
        leverage = agent_context.get("leverage") or (signal.indicators_snapshot or {}).get("leverage")

        # Compute derived values
        lots = max(1, signal.quantity // max(1, signal.lot_size))
        confidence_pct = int(signal.confidence_score * 100)
        bar = format_confidence_bar(signal.confidence_score)
        risk_per_unit = abs(signal.entry_price - signal.stop_loss)
        # Crypto (vanilla): PnL/risk uses contract_value multiplier
        if is_crypto:
            try:
                from risk.delta_margin import get_default_product_spec
                cv = get_default_product_spec(signal.symbol).contract_value
            except Exception:
                cv = 0.001
            max_risk_amt = risk_per_unit * signal.quantity * cv
        else:
            max_risk_amt = risk_per_unit * signal.quantity
        max_risk_pct = (max_risk_amt / capital * 100) if capital > 0 else 0.0

        sl_pct = format_sl_pct(signal.entry_price, signal.stop_loss)
        tgt_pct = format_target_pct(signal.entry_price, signal.target)

        # Build rationale bullet list (max 5 items to keep card concise)
        rationale_lines = "\n".join(
            f"• {escape_md(r)}" for r in signal.rationale[:5]
        )
        if not rationale_lines:
            rationale_lines = "• No rationale provided"

        # News summary (optional)
        news_line = (
            f"\n📰 *News:* {escape_md(signal.news_summary)}"
            if signal.news_summary
            else ""
        )

        # Instrument type label
        inst_type_label = {
            "CE": "CALL OPTION",
            "PE": "PUT OPTION",
            "FUT": "FUTURES",
            "EQ": "EQUITY",
        }.get(str(signal.instrument_type), str(signal.instrument_type))

        trade_id = escape_md(format_trade_id(signal.id))
        symbol_esc = escape_md(signal.symbol)
        exchange_esc = escape_md(str(signal.exchange))
        inst_label_esc = escape_md(inst_type_label)
        strategy_esc = escape_md(signal.strategy_name)
        version_esc = escape_md(strategy_version)
        regime_esc = escape_md(format_regime(str(signal.regime)))
        direction_esc = escape_md(format_direction(str(signal.direction)))
        entry_esc = escape_md(f"{currency}{signal.entry_price:,.2f}")
        sl_esc = escape_md(f"{currency}{signal.stop_loss:,.2f} ({sl_pct})")
        tgt_esc = escape_md(f"{currency}{signal.target:,.2f} ({tgt_pct})")
        if is_crypto:
            qty_esc = escape_md(f"{signal.quantity} contract{'s' if signal.quantity != 1 else ''}")
        else:
            qty_esc = escape_md(f"{signal.quantity} units ({lots} lot{'s' if lots != 1 else ''})")
        conf_esc = escape_md(f"{confidence_pct}% {bar}")
        rr_esc = escape_md(format_risk_reward(signal.risk_reward))
        if is_crypto:
            max_risk_esc = escape_md(f"${max_risk_amt:,.2f} ({max_risk_pct:.2f}% of wallet)")
        else:
            max_risk_esc = escape_md(
                f"{format_currency(max_risk_amt)} ({max_risk_pct:.2f}% of capital)"
            )
        expiry_esc = escape_md(format_time_ist(signal.expires_at))
        leverage_line = ""
        if is_crypto and leverage:
            leverage_line = f"Leverage: *{escape_md(f'{float(leverage):.0f}x')}*\n"

        text = (
            f"🎯 *TRADE PROPOSAL {trade_id}*\n"
            f"{'\\=' * 30}\n"
            f"📊 *{symbol_esc}* \\| {exchange_esc} \\| {inst_label_esc}\n"
            f"Strategy: {strategy_esc} \\| Version: {version_esc}\n"
            f"Market Regime: {regime_esc}\n"
            f"\n"
            f"Direction: *{direction_esc}*\n"
            f"{leverage_line}"
            f"Entry: {entry_esc}\n"
            f"Stop Loss: {sl_esc}\n"
            f"Target: {tgt_esc}\n"
            f"Quantity: {qty_esc}\n"
            f"\n"
            f"Confidence: {conf_esc}\n"
            f"Risk/Reward: {rr_esc}\n"
            f"Max Risk: {max_risk_esc}\n"
            f"\n"
            f"🔍 *Top Reasons:*\n"
            f"{rationale_lines}"
            f"{news_line}\n"
            f"\n"
            f"⏰ Valid until: {expiry_esc}\n"
            f"{'\\=' * 30}"
        )

        keyboard = self._trade_card_keyboard(signal_id)
        return await self._send(text, reply_markup=keyboard)

    # ------------------------------------------------------------------
    # Order lifecycle notifications
    # ------------------------------------------------------------------

    async def send_order_placed(
        self,
        order_response: OrderResponse,
        signal: TradeSignal,
    ) -> None:
        """Send a confirmation that an order was successfully placed."""
        broker_id = escape_md(order_response.broker_order_id)
        symbol = escape_md(signal.symbol)
        price = escape_md(f"₹{signal.entry_price:,.2f}")
        trade_id = escape_md(format_trade_id(signal.id))
        text = (
            f"🟢 *Order Placed*\n"
            f"Trade: {trade_id} \\| Symbol: {symbol}\n"
            f"Broker Order ID: `{broker_id}`\n"
            f"Entry Price: {price}\n"
            f"Qty: {escape_md(str(signal.quantity))} units"
        )
        await self._send(text)

    async def send_order_failed(
        self,
        signal: TradeSignal,
        error: str,
    ) -> None:
        """Send an alert when order placement fails at the broker."""
        symbol = escape_md(signal.symbol)
        error_esc = escape_md(error)
        trade_id = escape_md(format_trade_id(signal.id))
        text = (
            f"🔴 *Order Failed*\n"
            f"Trade: {trade_id} \\| Symbol: {symbol}\n"
            f"Reason: {error_esc}\n"
            f"Action required: check broker dashboard"
        )
        await self._send(text)

    # ------------------------------------------------------------------
    # Position event notifications
    # ------------------------------------------------------------------

    async def send_sl_hit(self, position: Position) -> None:
        """
        Notify that a stop-loss was triggered and the position was closed.
        """
        symbol = escape_md(position.symbol)
        exit_price = escape_md(f"₹{position.current_price:,.2f}")
        pnl = escape_md(format_currency(position.unrealized_pnl))
        pnl_emoji = "🔴" if position.unrealized_pnl < 0 else "🟢"
        direction = escape_md(format_direction(str(position.direction)))
        text = (
            f"🟠 *Stop Loss Hit*\n"
            f"Symbol: {symbol} \\| Direction: {direction}\n"
            f"Exit Price: {exit_price}\n"
            f"P&L: {pnl_emoji} {pnl}"
        )
        await self._send(text)

    async def send_target_hit(self, position: Position) -> None:
        """
        Notify that the profit target was reached and the position was closed.
        """
        symbol = escape_md(position.symbol)
        exit_price = escape_md(f"₹{position.current_price:,.2f}")
        pnl = escape_md(format_currency(position.unrealized_pnl))
        direction = escape_md(format_direction(str(position.direction)))
        text = (
            f"🟢 *Target Hit* 🎉\n"
            f"Symbol: {symbol} \\| Direction: {direction}\n"
            f"Exit Price: {exit_price}\n"
            f"Profit: \\+{pnl}"
        )
        await self._send(text)

    async def send_trailing_sl_update(
        self,
        position: Position,
        new_sl: float,
    ) -> None:
        """
        Notify that the trailing stop-loss level has been ratcheted up/down.
        """
        symbol = escape_md(position.symbol)
        new_sl_esc = escape_md(f"₹{new_sl:,.2f}")
        old_sl_esc = escape_md(f"₹{position.stop_loss:,.2f}")
        text = (
            f"📐 *Trailing SL Updated*\n"
            f"Symbol: {symbol}\n"
            f"Previous SL: {old_sl_esc}\n"
            f"New SL: *{new_sl_esc}*"
        )
        await self._send(text)

    # ------------------------------------------------------------------
    # Daily P&L summary
    # ------------------------------------------------------------------

    async def send_daily_summary(
        self,
        risk_state: RiskState,
        trades: list[Trade],
    ) -> None:
        """
        Send an end-of-day P&L summary card.

        Parameters
        ----------
        risk_state:
            The final daily risk state containing aggregate P&L counters.
        trades:
            All completed trades for the day (used to compute win rate, etc.).
        """
        total = len(trades)
        wins = sum(1 for t in trades if t.win)
        losses = total - wins
        win_rate = (wins / total * 100) if total > 0 else 0.0
        gross_pnl = sum(t.realized_pnl for t in trades)
        total_charges = sum(t.total_charges for t in trades)
        net_pnl = sum(t.net_pnl for t in trades)
        pnl_emoji = "🟢" if net_pnl >= 0 else "🔴"

        date_esc = escape_md(str(risk_state.date))
        net_esc = escape_md(format_currency(net_pnl))
        gross_esc = escape_md(format_currency(gross_pnl))
        charges_esc = escape_md(format_currency(total_charges))
        dd_esc = escape_md(format_currency(risk_state.max_drawdown_today))
        consec_esc = escape_md(str(risk_state.consecutive_losses))

        # Build trade rows (max 10 to avoid message length limits)
        rows: list[str] = []
        for t in trades[:10]:
            direction_char = "▲" if str(t.direction) == "BUY" else "▼"
            pnl_char = "+" if t.net_pnl >= 0 else ""
            rows.append(
                f"  {escape_md(t.symbol)} {direction_char} "
                f"{escape_md(pnl_char + format_currency(t.net_pnl))} "
                f"\\({escape_md(t.exit_reason)}\\)"
            )
        trade_rows = "\n".join(rows) if rows else "  No trades today"

        text = (
            f"📊 *Daily Summary — {date_esc}*\n"
            f"{'=' * 40}\n"
            f"{pnl_emoji} Net P&L: *{net_esc}*\n"
            f"Gross P&L: {gross_esc}\n"
            f"Total Charges: {charges_esc}\n"
            f"\n"
            f"Trades: {escape_md(str(total))} "
            f"\\| Wins: {escape_md(str(wins))} "
            f"\\| Losses: {escape_md(str(losses))}\n"
            f"Win Rate: {escape_md(f'{win_rate:.1f}%')}\n"
            f"Max Drawdown: {dd_esc}\n"
            f"Consecutive Losses: {consec_esc}\n"
            f"\n"
            f"*Trade Log:*\n"
            f"{trade_rows}\n"
            f"{'=' * 40}"
        )
        await self._send(text)

    # ------------------------------------------------------------------
    # Emergency / system alerts
    # ------------------------------------------------------------------

    async def send_kill_switch_alert(self, reason: str) -> None:
        """
        Send an EMERGENCY STOP alert to the admin.

        This message uses HTML-like emphasis with MarkdownV2 bold and is
        intentionally stark to grab operator attention.
        """
        reason_esc = escape_md(reason)
        text = (
            "🚨🚨🚨 *EMERGENCY STOP ACTIVATED* 🚨🚨🚨\n"
            f"{'=' * 40}\n"
            f"Reason: {reason_esc}\n"
            f"\n"
            f"ALL new order placement is *BLOCKED*\\.\n"
            f"Existing positions are still monitored\\.\n"
            f"\n"
            f"Use /resume to deactivate the kill switch\\."
        )
        await self._send(text)

    async def send_system_alert(
        self,
        message: str,
        severity: str = "INFO",
    ) -> None:
        """
        Send a general system alert.

        Parameters
        ----------
        message:
            The alert message body.
        severity:
            One of 'INFO', 'WARNING', 'ERROR', 'CRITICAL'.
        """
        severity_emojis: dict[str, str] = {
            "INFO":     "ℹ️",
            "WARNING":  "⚠️",
            "ERROR":    "🔴",
            "CRITICAL": "🚨",
        }
        emoji = severity_emojis.get(severity.upper(), "ℹ️")
        severity_esc = escape_md(severity.upper())
        message_esc = escape_md(message)
        now_esc = escape_md(
            format_time_ist(datetime.now(timezone.utc))
        )
        text = (
            f"{emoji} *System Alert \\[{severity_esc}\\]*\n"
            f"Time: {now_esc}\n"
            f"\n"
            f"{message_esc}"
        )
        await self._send(text)
