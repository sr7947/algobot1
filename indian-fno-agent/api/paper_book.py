"""
api/paper_book.py
=================
In-memory paper/testnet open positions used by Telegram approve flows
and the dashboard. Kept free of FastAPI/DB imports so the Telegram bot
can approve trades without pulling in the full API stack.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

ACTIVE_PAPER_POSITIONS: List[Dict[str, Any]] = []


def add_paper_position(signal: Any) -> Dict[str, Any]:
    """Record an approved paper position (F&O or Delta crypto)."""
    pos_id = str(getattr(signal, "id", "pos-new"))
    symbol = str(getattr(signal, "symbol", "NIFTY 24400 CE"))
    qty = int(getattr(signal, "quantity", 50))
    entry = float(getattr(signal, "entry_price", 145.0))
    sl = float(getattr(signal, "stop_loss", 101.5))
    target = float(getattr(signal, "target", 217.5))
    direction = str(getattr(signal, "direction", "BUY"))
    expiry = getattr(signal, "expiry_date", "04-AUG-2026")

    exchange = str(getattr(signal, "exchange", "NFO")).upper()
    is_crypto = "BTC" in symbol.upper() or "ETH" in symbol.upper() or exchange == "DELTA"
    asset_class = "CRYPTO" if is_crypto else "FNO"

    # Prefer leverage from indicators_snapshot when present (Telegram samples)
    snap = getattr(signal, "indicators_snapshot", None) or {}
    signal_leverage = getattr(signal, "leverage", None) or snap.get("leverage")

    pos: Dict[str, Any] = {
        "id": pos_id,
        "symbol": symbol,
        "exchange": exchange if exchange else ("DELTA" if is_crypto else "NFO"),
        "asset_class": asset_class,
        "expiry": "Perpetual" if is_crypto else expiry,
        "direction": direction,
        "qty": qty,
        "entry": entry,
        "current": entry,
        "pnl": 0.0,
        "sl": sl,
        "target": target,
        "trailingSl": entry,
        "time": "Just now",
        "created_at": time.time(),
    }
    if is_crypto:
        try:
            from config.settings import get_settings
            default_leverage = float(get_settings().DELTA_DEFAULT_LEVERAGE)
        except Exception:
            default_leverage = 25.0
        from risk.delta_margin import (
            estimate_position_margin,
            get_default_product_spec,
            position_leverage,
            position_notional,
        )

        lev = float(signal_leverage or default_leverage)
        spec = get_default_product_spec(symbol)
        margin = estimate_position_margin(size=qty, entry_price=entry, leverage=lev, product=spec)
        pos["leverage"] = lev
        pos["margin"] = margin
        pos["notional"] = position_notional(qty, entry, spec)
        pos["position_leverage"] = position_leverage(
            qty, entry, margin, unrealised_pnl=0.0, product=spec
        )

    ACTIVE_PAPER_POSITIONS.insert(0, pos)
    return pos
