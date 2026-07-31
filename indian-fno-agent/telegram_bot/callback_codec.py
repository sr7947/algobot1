"""
telegram_bot/callback_codec.py
==============================
Encode/decode Telegram inline-button callback payloads.

Telegram limits callback_data to 64 bytes. Self-contained approve payloads
let a poller execute a paper fill without looking up the in-memory store:

    ax:BTCUSD:BUY:65200:63500:68600:1:25
    hx:BTCUSD:BUY:65200:63500:68600:1:25   (half size)

Legacy UUID callbacks remain supported:

    approve:<uuid>
    reject:<uuid>
    half_size:<uuid>
    block:<uuid>
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


def encode_approve_payload(signal: Any, leverage: float | None = None) -> str:
    lev = leverage
    if lev is None:
        snap = getattr(signal, "indicators_snapshot", None) or {}
        lev = snap.get("leverage") or getattr(signal, "leverage", None) or 25
    return (
        f"{signal.symbol}:{signal.direction}:"
        f"{float(signal.entry_price):g}:{float(signal.stop_loss):g}:"
        f"{float(signal.target):g}:{int(signal.quantity)}:{float(lev):g}"
    )


def decode_callback_data(data: str) -> DecodedCallback:
    raw = (data or "").strip()
    if not raw or ":" not in raw:
        raise ValueError(f"Invalid callback data: {data!r}")

    # Self-contained compact forms
    if raw.startswith(("ax:", "hx:", "rx:", "bx:")):
        prefix, rest = raw[:2], raw[3:]
        action = {"ax": "approve", "hx": "half_size", "rx": "reject", "bx": "block"}[prefix]
        if prefix in ("ax", "hx"):
            parts = rest.split(":")
            if len(parts) != 7:
                raise ValueError(f"Invalid embedded trade callback: {data!r}")
            symbol, direction, entry, sl, target, qty, lev = parts
            return DecodedCallback(
                action=action,
                signal_id=None,
                embedded={
                    "symbol": symbol,
                    "direction": direction.upper(),
                    "entry_price": float(entry),
                    "stop_loss": float(sl),
                    "target": float(target),
                    "quantity": int(float(qty)),
                    "leverage": float(lev),
                },
            )
        return DecodedCallback(action=action, signal_id=rest)

    # Legacy '<action>:<signal_id>'
    action, signal_id = raw.split(":", 1)
    action = action.strip()
    signal_id = signal_id.strip()
    if action not in {"approve", "reject", "half_size", "block"} or not signal_id:
        raise ValueError(f"Invalid callback data: {data!r}")
    return DecodedCallback(action=action, signal_id=signal_id)


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
