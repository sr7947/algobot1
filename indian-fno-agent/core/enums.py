"""
core/enums.py
=============
Central enumeration definitions for the Indian F&O Trading Agent.

All enums inherit from (str, Enum) so that their values are directly
serialisable to JSON / stored in databases without manual conversion.
"""

from __future__ import annotations

from enum import Enum


# ---------------------------------------------------------------------------
# Exchange
# ---------------------------------------------------------------------------

class Exchange(str, Enum):
    """Supported Indian exchanges."""

    NSE = "NSE"   # National Stock Exchange – cash segment
    BSE = "BSE"   # Bombay Stock Exchange – cash segment
    NFO = "NFO"   # NSE Futures & Options
    BFO = "BFO"   # BSE Futures & Options
    MCX = "MCX"   # Multi Commodity Exchange
    DELTA = "DELTA" # Delta Exchange (Crypto Futures & Options)
    CRYPTO = "CRYPTO"


# ---------------------------------------------------------------------------
# Instrument type
# ---------------------------------------------------------------------------

class InstrumentType(str, Enum):
    """Canonical instrument types traded by the agent."""

    EQ  = "EQ"   # Equity / stock
    FUT = "FUT"  # Futures contract
    CE  = "CE"   # European Call Option
    PE  = "PE"   # European Put Option
    IDX = "IDX"  # Index (e.g. NIFTY, BANKNIFTY) – used for spot reference


# ---------------------------------------------------------------------------
# Trade direction
# ---------------------------------------------------------------------------

class TradeDirection(str, Enum):
    """Direction of a trade or position."""

    BUY  = "BUY"
    SELL = "SELL"


# ---------------------------------------------------------------------------
# Order type
# ---------------------------------------------------------------------------

class OrderType(str, Enum):
    """Order execution type as understood by most Indian brokers."""

    MARKET = "MARKET"  # Execute at best available market price
    LIMIT  = "LIMIT"   # Execute only at the specified price or better
    SL     = "SL"      # Stop-loss limit – triggers at trigger_price, executes at limit price
    SL_M   = "SL_M"    # Stop-loss market – triggers at trigger_price, executes at market


# ---------------------------------------------------------------------------
# Product type
# ---------------------------------------------------------------------------

class ProductType(str, Enum):
    """Margin product type that determines settlement and margining rules."""

    INTRADAY  = "INTRADAY"   # MIS – must be squared off same day
    CARRY     = "CARRY"      # NRML/Positional – can be carried overnight
    DELIVERY  = "DELIVERY"   # CNC – equity delivery, full payment required


# ---------------------------------------------------------------------------
# Order status
# ---------------------------------------------------------------------------

class OrderStatus(str, Enum):
    """Lifecycle states of a broker order."""

    PENDING   = "PENDING"    # Submitted but not yet acknowledged by exchange
    OPEN      = "OPEN"       # Resting in the order book
    COMPLETE  = "COMPLETE"   # Fully filled
    CANCELLED = "CANCELLED"  # Cancelled by user or system
    REJECTED  = "REJECTED"   # Rejected by broker or exchange
    PARTIAL   = "PARTIAL"    # Partially filled (remainder still in book)


# ---------------------------------------------------------------------------
# Signal status
# ---------------------------------------------------------------------------

class SignalStatus(str, Enum):
    """Lifecycle states of a generated trade signal."""

    PENDING_APPROVAL = "PENDING_APPROVAL"  # Waiting for Telegram user approval
    APPROVED         = "APPROVED"          # Approved – ready to be sent to broker
    REJECTED         = "REJECTED"          # Rejected by the user via Telegram
    BLOCKED          = "BLOCKED"           # Blocked by a news / event filter
    EXPIRED          = "EXPIRED"           # Approval window closed without action
    EXECUTED         = "EXECUTED"          # Order has been placed successfully
    RISK_REJECTED    = "RISK_REJECTED"     # Rejected by the risk management engine


# ---------------------------------------------------------------------------
# Market regime
# ---------------------------------------------------------------------------

class MarketRegime(str, Enum):
    """Macro market regime label produced by the regime detection module."""

    TRENDING_BULL    = "TRENDING_BULL"     # Clear uptrend with breadth confirmation
    TRENDING_BEAR    = "TRENDING_BEAR"     # Clear downtrend with breadth confirmation
    RANGE_BOUND      = "RANGE_BOUND"       # Price oscillating inside a defined range
    VOLATILE_BREAKOUT = "VOLATILE_BREAKOUT" # Breakout attempt with elevated volatility
    REVERSAL         = "REVERSAL"          # Potential trend reversal in progress
    NEWS_DRIVEN      = "NEWS_DRIVEN"       # Regime overridden by a high-impact news event
    UNKNOWN          = "UNKNOWN"           # Insufficient data to determine regime


# ---------------------------------------------------------------------------
# News sentiment
# ---------------------------------------------------------------------------

class NewsSentiment(str, Enum):
    """Sentiment classification for news events, produced by the NLP layer."""

    VERY_POSITIVE = "VERY_POSITIVE"
    POSITIVE      = "POSITIVE"
    NEUTRAL       = "NEUTRAL"
    NEGATIVE      = "NEGATIVE"
    VERY_NEGATIVE = "VERY_NEGATIVE"


# ---------------------------------------------------------------------------
# Event severity
# ---------------------------------------------------------------------------

class EventSeverity(str, Enum):
    """Impact severity of an external event (earnings, RBI policy, etc.)."""

    LOW      = "LOW"
    MEDIUM   = "MEDIUM"
    HIGH     = "HIGH"
    CRITICAL = "CRITICAL"  # May trigger the kill-switch or news-block window


# ---------------------------------------------------------------------------
# Trading mode
# ---------------------------------------------------------------------------

class TradingMode(str, Enum):
    """
    Operational mode of the agent.

    - PAPER : Simulate trades without hitting the broker API.
    - LIVE  : Execute real orders via the broker API.
    - SHADOW: Run all analysis and logging but do not place even paper trades.
              Useful for strategy observation without any side-effects.
    """

    PAPER  = "PAPER"
    LIVE   = "LIVE"
    SHADOW = "SHADOW"


# ---------------------------------------------------------------------------
# Audit event type
# ---------------------------------------------------------------------------

class AuditEventType(str, Enum):
    """
    Granular event types written to the immutable audit log.

    Every significant action in the agent lifecycle emits one of these events
    so that the full decision trail can be reconstructed after the fact.
    """

    SIGNAL_GENERATED      = "SIGNAL_GENERATED"
    RISK_CHECK_PASS       = "RISK_CHECK_PASS"
    RISK_CHECK_FAIL       = "RISK_CHECK_FAIL"
    TELEGRAM_SENT         = "TELEGRAM_SENT"
    TELEGRAM_APPROVED     = "TELEGRAM_APPROVED"
    TELEGRAM_REJECTED     = "TELEGRAM_REJECTED"
    TELEGRAM_HALF_SIZE    = "TELEGRAM_HALF_SIZE"   # User chose to take half position
    TELEGRAM_BLOCKED      = "TELEGRAM_BLOCKED"     # Blocked before Telegram send
    ORDER_PLACED          = "ORDER_PLACED"
    ORDER_FILLED          = "ORDER_FILLED"
    ORDER_CANCELLED       = "ORDER_CANCELLED"
    ORDER_REJECTED        = "ORDER_REJECTED"
    SL_HIT                = "SL_HIT"               # Stop-loss triggered
    TARGET_HIT            = "TARGET_HIT"           # Profit target reached
    TRAILING_SL_MOVED     = "TRAILING_SL_MOVED"    # Trailing stop-loss ratcheted up/down
    KILL_SWITCH_ACTIVATED = "KILL_SWITCH_ACTIVATED"
    BROKER_ERROR          = "BROKER_ERROR"
