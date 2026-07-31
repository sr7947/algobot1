#!/usr/bin/env python3
"""One-shot: send a sample BTCUSD trade proposal via Telegram."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.models import TradeSignal, MarketRegime, SignalStatus
from config.settings import get_settings
from risk.delta_margin import (
    estimate_position_margin,
    get_default_product_spec,
    initial_margin_pct_for_leverage,
)
from telegram_bot.notifier import TelegramNotifier


async def main() -> None:
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
    ]

    # Optional Gemini verdict (non-fatal if unavailable)
    try:
        from agents.gemini_reasoner import GeminiReasoningEngine

        ai_res = await GeminiReasoningEngine().evaluate_trade_signal(
            symbol="BTCUSD",
            direction="BUY",
            entry_price=entry,
            stop_loss=sl,
            target=target,
            technical_indicators={"EMA_Bull_Stack": True, "VWAP_Support": True, "RSI": 62, "Volume": "2.1x"},
            rationale=rationale,
            news_summary="Bitcoin ETF net inflows reach $450M; Fed signals upcoming rate cuts",
        )
        verdict = f"🤖 Gemini AI Verdict: [{ai_res.get('verdict', 'APPROVE')}]"
        final_rationale = [verdict] + ai_res.get("ai_rationale", rationale)
    except Exception as exc:
        print(f"Gemini skipped: {exc}")
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

    notifier = TelegramNotifier()
    msg_id = await notifier.send_trade_card(
        signal,
        {"capital": 200.0, "leverage": leverage, "asset_class": "CRYPTO"},
    )
    print(
        f"OK signal_id={signal.id} telegram_message_id={msg_id} "
        f"leverage={leverage}x margin=${margin:.4f} chat_id={settings.TELEGRAM_CHAT_ID}"
    )
    if not msg_id:
        raise SystemExit("Telegram send failed (no message id)")


if __name__ == "__main__":
    asyncio.run(main())
