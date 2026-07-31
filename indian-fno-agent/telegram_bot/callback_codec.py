"""
telegram_bot/callback_codec.py
==============================
Encode/decode Telegram inline-button callback payloads.

Telegram limits callback_data to 64 bytes. Self-contained approve payloads
let a poller execute a paper fill without looking up the in-memory store.

Preferred (compatible with older routers that only accept named actions)::

    approve:BTCUSD:BUY:65200:63500:68600:1:25
    half_size:BTCUSD:BUY:65200:63500:68600:1:25
    reject:<uuid>
    block:<uuid>

Legacy compact aliases (still decoded)::

    ax:BTCUSD:BUY:65200:63500:68600:1:25
    hx:… / rx:… / bx:…

Legacy UUID-only::

    approve:<uuid>
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4


@dataclass
class DecodedCallback:
    action: str  # approve | reject | half_size | block
    signal_id: Optional[str] = None
    embedded: Optional[dict[str, Any]] = None


def _direction_str(direction: Any) -> str:
    if hasattr(direction, "value"):
        return str(direction.value).upper()
    text = str(direction or "BUY").upper()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text


def encode_approve_payload(signal: Any, leverage: float | None = None) -> str:
    """Compact SYM:DIR:ENTRY:SL:TARGET:QTY:LEV body (no action prefix)."""
    lev = leverage
    if lev is None:
        snap = getattr(signal, "indicators_snapshot", None) or {}
        lev = snap.get("leverage") or getattr(signal, "leverage", None) or 25
    return (
        f"{signal.symbol}:{_direction_str(signal.direction)}:"
        f"{float(signal.entry_price):g}:{float(signal.stop_loss):g}:"
        f"{float(signal.target):g}:{int(signal.quantity)}:{float(lev):g}"
    )


def _parse_embedded_parts(parts: list[str]) -> dict[str, Any]:
    if len(parts) != 7:
        raise ValueError(f"Invalid embedded trade parts ({len(parts)}): {parts!r}")
    symbol, direction, entry, sl, target, qty, lev = parts
    return {
        "symbol": symbol,
        "direction": _direction_str(direction),
        "entry_price": float(entry),
        "stop_loss": float(sl),
        "target": float(target),
        "quantity": int(float(qty)),
        "leverage": float(lev),
    }


def _looks_like_embedded(body: str) -> bool:
    """True when body is SYM:DIR:ENTRY:SL:TARGET:QTY:LEV (not a bare UUID)."""
    parts = body.split(":")
    if len(parts) != 7:
        return False
    try:
        float(parts[2])
        float(parts[3])
        float(parts[4])
        float(parts[5])
        float(parts[6])
    except ValueError:
        return False
    return parts[1].upper() in {"BUY", "SELL", "LONG", "SHORT"}


def decode_callback_data(data: str) -> DecodedCallback:
    raw = (data or "").strip()
    if not raw or ":" not in raw:
        raise ValueError(f"Invalid callback data: {data!r}")

    # Compact aliases (older cloud cards)
    if raw.startswith(("ax:", "hx:", "rx:", "bx:")):
        prefix, rest = raw[:2], raw[3:]
        action = {"ax": "approve", "hx": "half_size", "rx": "reject", "bx": "block"}[prefix]
        if prefix in ("ax", "hx"):
            return DecodedCallback(
                action=action,
                signal_id=None,
                embedded=_parse_embedded_parts(rest.split(":")),
            )
        return DecodedCallback(action=action, signal_id=rest)

    # Named actions — may carry UUID or embedded trade params
    if raw.startswith("half_size:"):
        action, body = "half_size", raw[len("half_size:") :]
    else:
        action, body = raw.split(":", 1)
        action = action.strip()
        body = body.strip()

    if action not in {"approve", "reject", "half_size", "block"} or not body:
        raise ValueError(f"Invalid callback data: {data!r}")

    if action in {"approve", "half_size"} and _looks_like_embedded(body):
        return DecodedCallback(
            action=action,
            signal_id=None,
            embedded=_parse_embedded_parts(body.split(":")),
        )

    return DecodedCallback(action=action, signal_id=body)


def signal_from_embedded(embedded: dict[str, Any]) -> Any:
    """Build a TradeSignal (or duck-typed object) from embedded callback params."""
    from core.models import TradeSignal, MarketRegime, SignalStatus

    qty = int(embedded["quantity"])
    lev = float(embedded.get("leverage") or 25)
    return TradeSignal(
        id=uuid4(),
        created_at=datetime.now(timezone.utc),
        strategy_name="Telegram Inline Approve",
        symbol=str(embedded["symbol"]),
        exchange="DELTA",
        instrument_type="FUT",
        direction=str(embedded["direction"]).upper(),
        entry_price=float(embedded["entry_price"]),
        stop_loss=float(embedded["stop_loss"]),
        target=float(embedded["target"]),
        quantity=qty,
        lot_size=qty,
        confidence_score=0.8,
        regime=MarketRegime.TRENDING_BULL.value,
        rationale=["Approved via self-contained Telegram callback"],
        indicators_snapshot={"leverage": lev},
        status=SignalStatus.PENDING_APPROVAL.value,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
