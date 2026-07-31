"""
api/paper_book.py
=================
Paper/testnet open positions used by Telegram approve flows and the
dashboard. Persists to data/paper_positions.json so the API process can
see fills created by a Telegram poller on the same host.

Also merges data/paper_positions.seed.json when the live book is empty
(so git-pulled demo fills appear on a fresh local API), and can HTTP-sync
new fills to PAPER_BOOK_SYNC_URL (default http://127.0.0.1:8000/...).
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
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_PAPER_PATH = _DATA_DIR / "paper_positions.json"
_SEED_PATH = _DATA_DIR / "paper_positions.seed.json"

# Mutable list — positions.py iterates / mutates this directly.
ACTIVE_PAPER_POSITIONS: List[Dict[str, Any]] = []


def _read_positions_file(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return []
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if isinstance(raw, dict):
        positions = raw.get("positions") or []
        if isinstance(positions, list):
            return [r for r in positions if isinstance(r, dict)]
        if isinstance(positions, dict):
            return [r for r in positions.values() if isinstance(r, dict)]
    return []


def _load_disk() -> None:
    rows = _read_positions_file(_PAPER_PATH)
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


def _merge_rows_unlocked(rows: List[Dict[str, Any]]) -> int:
    existing = {str(p.get("id") or "") for p in ACTIVE_PAPER_POSITIONS}
    existing |= {str(p.get("signal_id") or "") for p in ACTIVE_PAPER_POSITIONS}
    added = 0
    for row in rows:
        rid = str(row.get("id") or "")
        sid = str(row.get("signal_id") or "")
        if (rid and rid in existing) or (sid and sid in existing):
            continue
        ACTIVE_PAPER_POSITIONS.insert(0, dict(row))
        if rid:
            existing.add(rid)
        if sid:
            existing.add(sid)
        added += 1
    return added


def merge_seed(*, force: bool = False) -> int:
    """
    Merge checked-in seed fills into the live book.

    When *force* is False, only runs if the live book is currently empty
    (fresh local API after git pull).
    """
    with _LOCK:
        _load_disk()
        if ACTIVE_PAPER_POSITIONS and not force:
            return 0
        added = _merge_rows_unlocked(_read_positions_file(_SEED_PATH))
        if added:
            _save_disk()
            logger.info("Merged %d seed paper position(s)", added)
        return added


def upsert_paper_position_dict(pos: Dict[str, Any]) -> Dict[str, Any]:
    """Insert or replace a paper position dict and persist."""
    with _LOCK:
        _load_disk()
        pid = str(pos.get("id") or "")
        ACTIVE_PAPER_POSITIONS[:] = [
            p
            for p in ACTIVE_PAPER_POSITIONS
            if str(p.get("id")) != pid and str(p.get("signal_id")) != pid
        ]
        ACTIVE_PAPER_POSITIONS.insert(0, dict(pos))
        _save_disk()
        return pos


def reload() -> None:
    """Reload from disk, bootstrap seed if empty, sync APPROVED Telegram signals."""
    with _LOCK:
        _load_disk()
        if not ACTIVE_PAPER_POSITIONS:
            added = _merge_rows_unlocked(_read_positions_file(_SEED_PATH))
            if added:
                _save_disk()
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
    if hasattr(direction, "value"):
        direction = str(direction.value)
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
        "direction": str(direction).upper(),
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


def _http_sync_position(pos: Dict[str, Any]) -> None:
    """Best-effort POST to local/remote API so dashboard process sees the fill."""
    try:
        from config.settings import get_settings

        url = (get_settings().PAPER_BOOK_SYNC_URL or "").strip()
    except Exception:
        url = "http://127.0.0.1:8000/api/v1/positions/paper"
    if not url:
        return
    try:
        import urllib.request

        req = urllib.request.Request(
            url,
            data=json.dumps(pos).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            resp.read()
    except Exception as exc:
        logger.debug("Paper book HTTP sync skipped: %s", exc)


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
    _http_sync_position(pos)
    return pos


# Load once at import so early readers see disk state
_load_disk()
if not ACTIVE_PAPER_POSITIONS:
    try:
        merge_seed(force=False)
    except Exception:
        pass
