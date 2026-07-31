"""Unit tests for Telegram callback encoding/decoding."""

from __future__ import annotations

from types import SimpleNamespace

from telegram_bot.callback_codec import (
    decode_callback_data,
    encode_approve_payload,
    signal_from_embedded,
)


def test_named_approve_embedded_roundtrip():
    sig = SimpleNamespace(
        symbol="BTCUSD",
        direction="BUY",
        entry_price=65200.0,
        stop_loss=63500.0,
        target=68600.0,
        quantity=1,
        indicators_snapshot={"leverage": 25},
    )
    payload = encode_approve_payload(sig)
    data = f"approve:{payload}"
    assert len(data.encode()) <= 64
    decoded = decode_callback_data(data)
    assert decoded.action == "approve"
    assert decoded.embedded is not None
    assert decoded.embedded["symbol"] == "BTCUSD"
    assert decoded.embedded["leverage"] == 25.0
    built = signal_from_embedded(decoded.embedded)
    assert built.symbol == "BTCUSD"
    assert built.quantity == 1


def test_half_size_embedded():
    data = "half_size:BTCUSD:BUY:65200:63500:68600:1:25"
    decoded = decode_callback_data(data)
    assert decoded.action == "half_size"
    assert decoded.embedded["entry_price"] == 65200.0


def test_legacy_ax_alias_still_works():
    decoded = decode_callback_data("ax:BTCUSD:BUY:65200:63500:68600:1:25")
    assert decoded.action == "approve"
    assert decoded.embedded["symbol"] == "BTCUSD"


def test_uuid_approve_still_works():
    uid = "3b81bd05-688b-4aec-929c-39a06c55a2ca"
    decoded = decode_callback_data(f"approve:{uid}")
    assert decoded.action == "approve"
    assert decoded.signal_id == uid
    assert decoded.embedded is None


def test_reject_block():
    assert decode_callback_data("reject:abc").action == "reject"
    assert decode_callback_data("block:abc").signal_id == "abc"
