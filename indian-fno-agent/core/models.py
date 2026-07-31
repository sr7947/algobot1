"""
core/models.py
==============
Pydantic v2 domain models for the Indian F&O Trading Agent.

Design decisions
----------------
- All models use ``model_config = ConfigDict(use_enum_values=True)`` so that
  enum fields are stored and serialised as plain strings.
- UUIDs default to ``uuid4`` so every record is self-identifying.
- ``datetime`` fields are always timezone-aware (UTC).  Use
  ``datetime.now(timezone.utc)`` at call sites.
- ``Optional[X]`` is used for fields that may genuinely be absent; never
  substitute ``None`` for "not yet computed" numeric fields – prefer a
  dedicated sentinel value or a separate flag.
- The ``risk_reward`` property on ``TradeSignal`` is a computed field so
  that it is always consistent with the underlying price levels.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, computed_field

from core.enums import (
    Exchange,
    InstrumentType,
    MarketRegime,
    NewsSentiment,
    EventSeverity,
    OrderStatus,
    OrderType,
    ProductType,
    SignalStatus,
    TradeDirection,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    """Return the current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Market / instrument models
# ---------------------------------------------------------------------------

class Instrument(BaseModel):
    """
    Represents a tradable instrument (equity, futures, or options contract).

    ``token`` is the exchange-assigned numeric/alphanumeric scrip token used
    when placing orders via the broker API.
    """

    model_config = ConfigDict(use_enum_values=True)

    id: UUID = Field(default_factory=uuid4, description="Internal unique identifier")
    symbol: str = Field(..., description="Trading symbol, e.g. 'NIFTY23JUNFUT'")
    exchange: Exchange = Field(..., description="Exchange where the instrument is listed")
    instrument_type: InstrumentType = Field(..., description="EQ / FUT / CE / PE / IDX")
    lot_size: int = Field(..., gt=0, description="Contract lot size (1 for equities)")
    tick_size: float = Field(..., gt=0, description="Minimum price movement, e.g. 0.05")
    expiry: Optional[date] = Field(None, description="Expiry date for F&O contracts")
    strike: Optional[float] = Field(None, description="Strike price for options")
    option_type: Optional[str] = Field(None, description="'CE' or 'PE' – redundant but convenient")
    underlying: Optional[str] = Field(None, description="Parent symbol, e.g. 'NIFTY' for options")
    token: str = Field(..., description="Broker / exchange scrip token")
    is_active: bool = Field(default=True, description="False for expired / delisted instruments")


class Candle(BaseModel):
    """OHLCV + Open Interest candle for any timeframe."""

    model_config = ConfigDict(use_enum_values=True)

    time: datetime = Field(..., description="Candle open timestamp (timezone-aware)")
    open: float = Field(..., description="Open price")
    high: float = Field(..., description="High price")
    low: float = Field(..., description="Low price")
    close: float = Field(..., description="Close / last traded price")
    volume: float = Field(..., ge=0, description="Traded volume for the candle period")
    oi: Optional[float] = Field(None, ge=0, description="Open interest (F&O contracts only)")


class Tick(BaseModel):
    """Real-time market tick – the smallest unit of live market data."""

    model_config = ConfigDict(use_enum_values=True)

    symbol: str = Field(..., description="Trading symbol")
    ltp: float = Field(..., description="Last traded price")
    bid: float = Field(..., description="Best bid price")
    ask: float = Field(..., description="Best ask price")
    volume: float = Field(..., ge=0, description="Cumulative volume for the session")
    oi: float = Field(..., ge=0, description="Current open interest")
    timestamp: datetime = Field(default_factory=_utcnow, description="Tick receipt timestamp (UTC)")


# ---------------------------------------------------------------------------
# Options chain models
# ---------------------------------------------------------------------------

class OptionChainEntry(BaseModel):
    """
    A single strike row in the option chain.

    Greeks are optional because they may not be available from all data sources
    or may need to be computed separately.
    """

    model_config = ConfigDict(use_enum_values=True)

    strike: float = Field(..., description="Strike price")

    # Call side
    call_oi: float = Field(..., ge=0, description="Call open interest (contracts)")
    call_oi_change: float = Field(..., description="Change in call OI vs previous snapshot")
    call_iv: float = Field(..., ge=0, description="Implied volatility for call (annualised %)")
    call_ltp: float = Field(..., ge=0, description="Last traded price of the call option")
    call_greeks: Optional[dict[str, float]] = Field(
        None, description="Greeks: delta, gamma, theta, vega, rho"
    )

    # Put side
    put_oi: float = Field(..., ge=0, description="Put open interest (contracts)")
    put_oi_change: float = Field(..., description="Change in put OI vs previous snapshot")
    put_iv: float = Field(..., ge=0, description="Implied volatility for put (annualised %)")
    put_ltp: float = Field(..., ge=0, description="Last traded price of the put option")
    put_greeks: Optional[dict[str, float]] = Field(
        None, description="Greeks: delta, gamma, theta, vega, rho"
    )


class OptionChain(BaseModel):
    """
    Complete option chain snapshot for an underlying at a given expiry.

    ``pcr`` (Put-Call Ratio) is computed as total put OI / total call OI.
    """

    model_config = ConfigDict(use_enum_values=True)

    underlying: str = Field(..., description="Underlying symbol, e.g. 'NIFTY'")
    expiry: date = Field(..., description="Expiry date of this chain")
    spot_price: float = Field(..., description="Current spot price of the underlying")
    entries: list[OptionChainEntry] = Field(..., description="Strike-wise option chain rows")
    pcr: float = Field(..., ge=0, description="Put-Call Ratio (total put OI / total call OI)")
    timestamp: datetime = Field(default_factory=_utcnow, description="Snapshot timestamp (UTC)")


# ---------------------------------------------------------------------------
# Technical indicators
# ---------------------------------------------------------------------------

class TechnicalIndicators(BaseModel):
    """
    Computed technical indicator values for a given symbol and timeframe.

    All fields are Optional because not every indicator may be computable
    (e.g. insufficient historical data, missing volume for VWAP, etc.).
    """

    model_config = ConfigDict(use_enum_values=True)

    # Exponential Moving Averages
    ema_9: Optional[float] = Field(None, description="9-period EMA")
    ema_21: Optional[float] = Field(None, description="21-period EMA")
    ema_50: Optional[float] = Field(None, description="50-period EMA")
    ema_200: Optional[float] = Field(None, description="200-period EMA")

    # Simple Moving Averages
    sma_20: Optional[float] = Field(None, description="20-period SMA")
    sma_50: Optional[float] = Field(None, description="50-period SMA")

    # Momentum
    rsi_14: Optional[float] = Field(None, ge=0, le=100, description="14-period RSI (0–100)")

    # MACD (12, 26, 9 defaults)
    macd_line: Optional[float] = Field(None, description="MACD line (fast EMA – slow EMA)")
    macd_signal: Optional[float] = Field(None, description="MACD signal line (9-period EMA of MACD)")
    macd_hist: Optional[float] = Field(None, description="MACD histogram (macd_line – macd_signal)")

    # Volume-weighted
    vwap: Optional[float] = Field(None, description="Volume Weighted Average Price (session VWAP)")

    # Bollinger Bands (20, 2σ defaults)
    upper_bb: Optional[float] = Field(None, description="Upper Bollinger Band")
    middle_bb: Optional[float] = Field(None, description="Middle Bollinger Band (SMA-20)")
    lower_bb: Optional[float] = Field(None, description="Lower Bollinger Band")

    # Volatility / Trend
    atr_14: Optional[float] = Field(None, ge=0, description="14-period Average True Range")
    adx_14: Optional[float] = Field(None, ge=0, le=100, description="14-period ADX (trend strength)")

    # Supertrend
    supertrend: Optional[float] = Field(None, description="Supertrend line value")
    supertrend_direction: Optional[str] = Field(
        None, description="'UP' if price is above Supertrend, 'DOWN' otherwise"
    )

    # Volume
    volume_sma: Optional[float] = Field(None, ge=0, description="20-period SMA of volume")


# ---------------------------------------------------------------------------
# Market snapshot
# ---------------------------------------------------------------------------

class MarketSnapshot(BaseModel):
    """
    Complete picture of a symbol at a point-in-time for a specific timeframe.

    This is the primary input fed to strategy and AI analysis modules.
    """

    model_config = ConfigDict(use_enum_values=True)

    symbol: str = Field(..., description="Trading symbol")
    timeframe: str = Field(..., description="Candle timeframe, e.g. '5m', '15m', '1h'")
    timestamp: datetime = Field(default_factory=_utcnow, description="Snapshot creation time (UTC)")
    candle: Candle = Field(..., description="Latest completed candle for this timeframe")
    indicators: TechnicalIndicators = Field(..., description="Computed indicator values")
    regime: MarketRegime = Field(..., description="Current market regime label")
    option_chain: Optional[OptionChain] = Field(
        None, description="Option chain snapshot – available for index and stock F&O only"
    )


# ---------------------------------------------------------------------------
# Trade signal
# ---------------------------------------------------------------------------

class TradeSignal(BaseModel):
    """
    A fully formed trade recommendation produced by a strategy or the AI layer.

    The ``risk_reward`` computed field is always consistent with the entry,
    stop-loss, and target prices stored in the model, eliminating the risk of
    stale cached values.
    """

    model_config = ConfigDict(use_enum_values=True)

    id: UUID = Field(default_factory=uuid4, description="Unique signal identifier")
    created_at: datetime = Field(default_factory=_utcnow, description="Signal generation timestamp (UTC)")
    strategy_name: str = Field(..., description="Name of the strategy that generated this signal")

    # Instrument details
    symbol: str = Field(..., description="Trading symbol")
    exchange: Exchange = Field(..., description="Exchange")
    instrument_type: InstrumentType = Field(..., description="Instrument type")
    direction: TradeDirection = Field(..., description="BUY or SELL")

    # Price levels
    entry_price: float = Field(..., gt=0, description="Expected / limit entry price")
    stop_loss: float = Field(..., gt=0, description="Stop-loss price")
    target: float = Field(..., gt=0, description="Profit target price")

    # Sizing
    quantity: int = Field(..., gt=0, description="Number of shares / units")
    lot_size: int = Field(..., gt=0, description="Contract lot size for F&O")

    # Scoring
    confidence_score: float = Field(
        ..., ge=0.0, le=1.0, description="AI/strategy confidence score (0 = no confidence, 1 = full)"
    )

    # Context
    regime: MarketRegime = Field(..., description="Market regime at signal generation time")
    rationale: list[str] = Field(
        default_factory=list,
        description="Human-readable reasons supporting this signal, one per list item",
    )
    news_summary: Optional[str] = Field(None, description="Relevant news context from the NLP layer")
    indicators_snapshot: dict[str, Any] = Field(
        default_factory=dict,
        description="Flat dict of indicator values captured at signal time for audit purposes",
    )

    # Lifecycle
    status: SignalStatus = Field(default=SignalStatus.PENDING_APPROVAL, description="Current lifecycle status")
    expires_at: datetime = Field(
        ..., description="Timestamp after which the signal is considered stale and should not be executed"
    )
    telegram_message_id: Optional[int] = Field(
        None, description="Telegram message ID used to track user approval callbacks"
    )

    @computed_field  # type: ignore[misc]
    @property
    def risk_reward(self) -> float:
        """
        Compute the Risk:Reward ratio as reward / risk.

        For a BUY trade:  risk = entry – stop_loss, reward = target – entry
        For a SELL trade: risk = stop_loss – entry, reward = entry – target

        Returns 0.0 if the computed risk is zero to avoid division by zero.
        """
        direction = TradeDirection(self.direction) if isinstance(self.direction, str) else self.direction

        if direction == TradeDirection.BUY:
            risk   = self.entry_price - self.stop_loss
            reward = self.target - self.entry_price
        else:
            risk   = self.stop_loss - self.entry_price
            reward = self.entry_price - self.target

        if risk <= 0:
            return 0.0
        return round(reward / risk, 2)


# ---------------------------------------------------------------------------
# Order models
# ---------------------------------------------------------------------------

class OrderRequest(BaseModel):
    """
    Fully specified order to be submitted to the broker API.

    ``signal_id`` links every order back to its originating ``TradeSignal``
    for a complete audit trail.
    """

    model_config = ConfigDict(use_enum_values=True)

    signal_id: UUID = Field(..., description="ID of the originating TradeSignal")
    symbol: str = Field(..., description="Trading symbol")
    exchange: Exchange = Field(..., description="Exchange")
    instrument_type: InstrumentType = Field(..., description="Instrument type")
    direction: TradeDirection = Field(..., description="BUY or SELL")
    order_type: OrderType = Field(..., description="MARKET / LIMIT / SL / SL_M")
    product_type: ProductType = Field(..., description="INTRADAY / CARRY / DELIVERY")
    quantity: int = Field(..., gt=0, description="Number of shares / units")
    price: Optional[float] = Field(None, gt=0, description="Limit price (required for LIMIT and SL orders)")
    trigger_price: Optional[float] = Field(
        None, gt=0, description="Trigger price (required for SL and SL_M orders)"
    )
    broker: str = Field(..., description="Broker identifier, e.g. 'angel_one', 'dhan'")


class OrderResponse(BaseModel):
    """
    Normalised response returned after an order is submitted to the broker.

    ``raw_response`` stores the unmodified broker API payload for debugging.
    """

    model_config = ConfigDict(use_enum_values=True)

    broker_order_id: str = Field(..., description="Order ID assigned by the broker / exchange")
    status: OrderStatus = Field(..., description="Normalised order status")
    message: str = Field(default="", description="Human-readable status message from broker")
    timestamp: datetime = Field(default_factory=_utcnow, description="Response timestamp (UTC)")
    raw_response: dict[str, Any] = Field(
        default_factory=dict, description="Unmodified broker API response payload"
    )


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------

class Position(BaseModel):
    """
    An open (or recently closed) position held by the agent.

    ``unrealized_pnl`` should be refreshed on every tick update.
    ``trailing_sl`` is optional and managed by the position monitor.
    """

    model_config = ConfigDict(use_enum_values=True)

    id: UUID = Field(default_factory=uuid4, description="Internal position identifier")
    order_id: str = Field(..., description="Broker order ID that opened this position")
    symbol: str = Field(..., description="Trading symbol")
    direction: TradeDirection = Field(..., description="BUY (long) or SELL (short)")
    quantity: int = Field(..., gt=0, description="Position size in shares / units")

    # Price levels
    entry_price: float = Field(..., gt=0, description="Average fill price")
    current_price: float = Field(..., gt=0, description="Latest market price (updated on ticks)")
    unrealized_pnl: float = Field(default=0.0, description="Mark-to-market P&L (not yet realised)")
    stop_loss: float = Field(..., gt=0, description="Active stop-loss level")
    target: float = Field(..., gt=0, description="Active profit target level")
    trailing_sl: Optional[float] = Field(
        None, description="Current trailing stop-loss level (None if trailing is disabled)"
    )

    # Timestamps
    opened_at: datetime = Field(default_factory=_utcnow, description="Position open timestamp (UTC)")
    closed_at: Optional[datetime] = Field(None, description="Position close timestamp (UTC)")
    exit_reason: Optional[str] = Field(
        None, description="Why the position was closed, e.g. 'SL_HIT', 'TARGET_HIT', 'MANUAL'"
    )


# ---------------------------------------------------------------------------
# Completed trade
# ---------------------------------------------------------------------------

class Trade(BaseModel):
    """
    An immutable record of a completed (fully closed) trade including P&L and charges.

    ``net_pnl`` = ``realized_pnl`` – ``total_charges``
    ``win`` is True when ``net_pnl > 0``.
    """

    model_config = ConfigDict(use_enum_values=True)

    id: UUID = Field(default_factory=uuid4, description="Trade record identifier")
    position_id: UUID = Field(..., description="ID of the Position this trade closes")
    signal_id: UUID = Field(..., description="ID of the originating TradeSignal")
    symbol: str = Field(..., description="Trading symbol")
    exchange: Exchange = Field(..., description="Exchange")
    instrument_type: InstrumentType = Field(..., description="Instrument type")
    direction: TradeDirection = Field(..., description="Trade direction")

    # Fill details
    quantity: int = Field(..., gt=0, description="Number of shares / units traded")
    entry_price: float = Field(..., gt=0, description="Average entry fill price")
    exit_price: float = Field(..., gt=0, description="Average exit fill price")

    # P&L
    realized_pnl: float = Field(..., description="Gross P&L before charges")
    brokerage: float = Field(default=0.0, ge=0, description="Brokerage charged")
    stt: float = Field(default=0.0, ge=0, description="Securities Transaction Tax")
    exchange_charges: float = Field(default=0.0, ge=0, description="NSE / BSE exchange transaction charges")
    sebi_charges: float = Field(default=0.0, ge=0, description="SEBI regulatory charges")
    gst: float = Field(default=0.0, ge=0, description="GST on brokerage and exchange charges")
    stamp_duty: float = Field(default=0.0, ge=0, description="Stamp duty")
    total_charges: float = Field(default=0.0, ge=0, description="Sum of all charges")
    net_pnl: float = Field(..., description="Net P&L after all charges")
    win: bool = Field(..., description="True if net_pnl > 0")

    # Metadata
    strategy_name: str = Field(..., description="Strategy that generated the originating signal")
    entry_time: datetime = Field(..., description="Position entry timestamp (UTC)")
    exit_time: datetime = Field(..., description="Position exit timestamp (UTC)")
    exit_reason: str = Field(..., description="Exit reason, e.g. 'TARGET_HIT', 'SL_HIT', 'MANUAL'")
    holding_duration_seconds: float = Field(
        ..., ge=0, description="Duration the position was held, in seconds"
    )


# ---------------------------------------------------------------------------
# Margin info
# ---------------------------------------------------------------------------

class MarginInfo(BaseModel):
    """
    Snapshot of the account's margin and cash position from the broker.

    All amounts are in INR.
    """

    model_config = ConfigDict(use_enum_values=True)

    available_cash: float = Field(..., ge=0, description="Uninvested cash balance (INR)")
    used_margin: float = Field(..., ge=0, description="Margin currently locked in open positions (INR)")
    available_margin: float = Field(..., ge=0, description="Net margin available for new trades (INR)")
    collateral: float = Field(default=0.0, ge=0, description="Pledge-based collateral credit (INR)")


# ---------------------------------------------------------------------------
# Risk state
# ---------------------------------------------------------------------------

class RiskState(BaseModel):
    """
    Daily risk counters maintained by the risk management engine.

    The kill-switch is automatically evaluated against these counters on
    every new trade signal.
    """

    model_config = ConfigDict(use_enum_values=True)

    date: date_type = Field(..., description="Trading date this risk state applies to")
    daily_pnl: float = Field(default=0.0, description="Cumulative net P&L for the day (INR)")
    daily_trades: int = Field(default=0, ge=0, description="Total trades executed today")
    daily_losses: int = Field(default=0, ge=0, description="Number of losing trades today")
    consecutive_losses: int = Field(
        default=0, ge=0, description="Current streak of consecutive losing trades"
    )
    max_drawdown_today: float = Field(
        default=0.0, description="Maximum intraday drawdown reached today (INR, negative)"
    )
    kill_switch_active: bool = Field(
        default=False,
        description="When True the agent must not place any new orders until manually reset",
    )


# ---------------------------------------------------------------------------
# News event
# ---------------------------------------------------------------------------

class NewsEvent(BaseModel):
    """
    A processed news or corporate event that may affect trading decisions.

    ``is_blocked_window`` signals that the agent should pause new entries
    around this event (e.g. 15 minutes before/after an RBI policy statement).
    """

    model_config = ConfigDict(use_enum_values=True)

    id: UUID = Field(default_factory=uuid4, description="Unique news event identifier")
    ingested_at: datetime = Field(default_factory=_utcnow, description="Timestamp when ingested (UTC)")
    source: str = Field(..., description="News source identifier, e.g. 'moneycontrol', 'reuters'")
    headline: str = Field(..., description="Original news headline")
    summary: str = Field(default="", description="AI-generated or source summary")
    sentiment: NewsSentiment = Field(..., description="Sentiment classification")
    severity: EventSeverity = Field(..., description="Event impact severity")
    symbols_affected: list[str] = Field(
        default_factory=list,
        description="List of symbols likely affected by this event",
    )
    event_type: str = Field(
        ..., description="Category of event, e.g. 'earnings', 'rbi_policy', 'geopolitical'"
    )
    is_blocked_window: bool = Field(
        default=False,
        description="True if the agent should avoid new entries during/around this event",
    )
