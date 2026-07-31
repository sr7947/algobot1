"""
api/paper_book.py
=================
Paper/testnet open positions used by Telegram approve flows and the
dashboard. Persists to data/paper_positions.json so the API process can
see fills created by a Telegram poller on the same host.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_PAPER_PATH = Path(__file__).resolve().parent.parent / "data" / "paper_positions.json"

# Mutable list — positions.py iterates / mutates this directly.
ACTIVE_PAPER_POSITIONS: List[Dict[str, Any]] = []


def _load_disk() -> None:
    if not _PAPER_PATH.exists():
        return
    try:
        raw = json.loads(_PAPER_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to load paper positions: %s", exc)
        return
    rows: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        rows = [r for r in raw if isinstance(r, dict)]
    elif isinstance(raw, dict):
        positions = raw.get("positions") or []
        if isinstance(positions, list):
            rows = [r for r in positions if isinstance(r, dict)]
        elif isinstance(positions, dict):
            rows = [r for r in positions.values() if isinstance(r, dict)]
    ACTIVE_PAPER_POSITIONS.clear()
    ACTIVE_PAPER_POSITIONS.extend(rows)


def _save_disk() -> None:
    try:
        _PAPER_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PAPER_PATH.with_suffix(".tmp")
        payload = {"positions": list(ACTIVE_PAPER_POSITIONS)}
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        tmp.replace(_PAPER_PATH)
    except Exception as exc:
        logger.warning("Failed to persist paper positions: %s", exc)


def save_paper_positions() -> None:
    """Persist current ACTIVE_PAPER_POSITIONS (call after external mutations)."""
    with _LOCK:
        _save_disk()


def reload() -> None:
    """Reload from disk and materialize any APPROVED Telegram signals missing here."""
    with _LOCK:
        _load_disk()
        _sync_approved_from_signal_store_unlocked()


def _signal_status(signal: Any) -> str:
    status = getattr(signal, "status", None)
    if status is None and isinstance(signal, dict):
        status = signal.get("status")
    return str(getattr(status, "value", status) or "").upper()


def _sync_approved_from_signal_store_unlocked() -> None:
    try:
        from telegram_bot.signal_store import get_signal_store
    except Exception:
        return

    existing_ids = {str(p.get("id") or "") for p in ACTIVE_PAPER_POSITIONS}
    existing_ids |= {str(p.get("signal_id") or "") for p in ACTIVE_PAPER_POSITIONS}

    try:
        store = get_signal_store()
    except Exception:
        return

    changed = False
    for sid, signal in list(store.items()):
        if _signal_status(signal) != "APPROVED":
            continue
        if str(sid) in existing_ids or str(getattr(signal, "id", "")) in existing_ids:
            continue
        try:
            pos = _build_position_from_signal(signal)
            ACTIVE_PAPER_POSITIONS.insert(0, pos)
            existing_ids.add(str(pos["id"]))
            changed = True
        except Exception as exc:
            logger.warning("Could not sync approved signal %s: %s", sid, exc)
    if changed:
        _save_disk()


def _build_position_from_signal(signal: Any) -> Dict[str, Any]:
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

    snap = getattr(signal, "indicators_snapshot", None) or {}
    if isinstance(signal, dict):
        snap = signal.get("indicators_snapshot") or snap
    signal_leverage = getattr(signal, "leverage", None) or snap.get("leverage")

    pos: Dict[str, Any] = {
        "id": pos_id,
        "signal_id": pos_id,
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
        "source": "telegram_paper",
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
        margin = estimate_position_margin(
            size=qty, entry_price=entry, leverage=lev, product=spec
        )
        pos["leverage"] = lev
        pos["margin"] = margin
        pos["notional"] = position_notional(qty, entry, spec)
        pos["position_leverage"] = position_leverage(
            qty, entry, margin, unrealised_pnl=0.0, product=spec
        )
    return pos


def add_paper_position(signal: Any) -> Dict[str, Any]:
    """Record an approved paper position (F&O or Delta crypto) and persist."""
    with _LOCK:
        _load_disk()
        pos_id = str(getattr(signal, "id", "pos-new"))
        for existing in ACTIVE_PAPER_POSITIONS:
            if str(existing.get("id")) == pos_id or str(existing.get("signal_id")) == pos_id:
                return existing
        pos = _build_position_from_signal(signal)
        ACTIVE_PAPER_POSITIONS.insert(0, pos)
        _save_disk()
        return pos


# Load once at import so early readers see disk state
_load_disk()
