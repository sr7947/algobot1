"""
telegram_bot/signal_store.py
============================
Process-wide + disk-backed signal registry shared by the notifier,
sample scripts, and the polling TelegramBot so Approve/Reject
callbacks can resolve signals across restarts and senders.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_SIGNAL_STORE: Dict[str, Any] = {}

# Persist under data/ so any bot process on this machine can resolve approvals
_STORE_PATH = Path(__file__).resolve().parents[1] / "data" / "pending_telegram_signals.json"


def _ensure_parent() -> None:
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)


def _signal_to_dict(signal: Any) -> dict:
    if hasattr(signal, "model_dump"):
        data = signal.model_dump(mode="json")
    elif hasattr(signal, "dict"):
        data = signal.dict()
    elif isinstance(signal, dict):
        data = dict(signal)
    else:
        data = {"id": str(getattr(signal, "id", signal))}
    # Ensure JSON-safe
    return json.loads(json.dumps(data, default=str))


def _dict_to_signal(data: dict) -> Any:
    try:
        from core.models import TradeSignal
        return TradeSignal.model_validate(data)
    except Exception:
        # Return raw dict if rehydration fails — handlers use attribute access
        class _Obj:
            pass
        obj = _Obj()
        for k, v in data.items():
            setattr(obj, k, v)
        return obj


def _load_disk() -> None:
    if not _STORE_PATH.exists():
        return
    try:
        raw = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        for sid, payload in raw.items():
            if sid not in _SIGNAL_STORE:
                _SIGNAL_STORE[sid] = _dict_to_signal(payload)
    except Exception as exc:
        logger.warning("Failed to load pending Telegram signals: %s", exc)


def _save_disk() -> None:
    try:
        _ensure_parent()
        payload = {sid: _signal_to_dict(sig) for sid, sig in _SIGNAL_STORE.items()}
        tmp = _STORE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        tmp.replace(_STORE_PATH)
    except Exception as exc:
        logger.warning("Failed to persist pending Telegram signals: %s", exc)


def get_signal_store() -> Dict[str, Any]:
    """Return the shared mutable signal store (loads disk once)."""
    with _LOCK:
        if not _SIGNAL_STORE:
            _load_disk()
        return _SIGNAL_STORE


def register_signal(signal: Any) -> str:
    """Register *signal* in memory + disk and return its string id."""
    signal_id = str(getattr(signal, "id", signal))
    with _LOCK:
        _load_disk()
        _SIGNAL_STORE[signal_id] = signal
        _save_disk()
    logger.info("Registered Telegram signal %s (pending=%d)", signal_id, len(_SIGNAL_STORE))
    return signal_id


def get_signal(signal_id: str) -> Optional[Any]:
    """Lookup a registered signal by id (memory, then disk)."""
    sid = str(signal_id)
    with _LOCK:
        if sid in _SIGNAL_STORE:
            return _SIGNAL_STORE[sid]
        _load_disk()
        return _SIGNAL_STORE.get(sid)


def remove_signal(signal_id: str) -> None:
    """Drop a signal after it is fully processed."""
    with _LOCK:
        _SIGNAL_STORE.pop(str(signal_id), None)
        _save_disk()


def pending_count() -> int:
    with _LOCK:
        _load_disk()
        return len(_SIGNAL_STORE)
