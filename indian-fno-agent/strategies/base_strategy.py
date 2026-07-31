"""
strategies/base_strategy.py
============================
Abstract base class for all Indian F&O trading strategies.

Every concrete strategy MUST subclass ``IStrategy`` and implement:
  - ``generate_signal``  – async coroutine that returns an Optional[TradeSignal]
  - ``validate_config``  – synchronous method that validates a raw config dict

Design goals
------------
* Framework-agnostic: no hard dependency on any broker SDK here.
* Fully typed: all public surfaces carry type annotations.
* YAML-driven config: each strategy reads its own YAML stanza; the helper
  ``get_config()`` exposes that stanza as a plain dict so strategies remain
  decoupled from the config-loading mechanism.
* Logging: each instance gets a child logger under the ``strategies`` namespace.

Instrument types (InstrumentType)
----------------------------------
  FUT  – Futures contract
  CE   – Call option
  PE   – Put option
  EQ   – Equity (cash segment, rarely used in F&O agent)
  IDX  – Index (underlying reference, not directly traded)

Trade signals (TradeSignal)
----------------------------
A dataclass carrying everything the order-execution layer needs to place,
size, and manage a trade.  See ``TradeSignal`` docstring for field semantics.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

import yaml

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class InstrumentType(str, Enum):
    """Instrument types supported by the F&O agent."""

    FUT = "FUT"    # Futures
    CE = "CE"      # Call option
    PE = "PE"      # Put option
    EQ = "EQ"      # Equity (cash)
    IDX = "IDX"    # Index (reference only)


class SignalDirection(str, Enum):
    """Trade direction."""

    BUY = "BUY"
    SELL = "SELL"
    NEUTRAL = "NEUTRAL"   # No actionable signal


class SignalStrength(str, Enum):
    """Qualitative signal strength bucket derived from confidence score."""

    STRONG = "STRONG"      # confidence >= 0.85
    MODERATE = "MODERATE"  # confidence >= 0.65
    WEAK = "WEAK"          # confidence < 0.65


class MarketRegime(str, Enum):
    """Detected market regime passed in context."""

    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGE_BOUND = "RANGE_BOUND"
    REVERSAL = "REVERSAL"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# TradeSignal dataclass
# ---------------------------------------------------------------------------


@dataclass
class TradeSignal:
    """
    Immutable signal returned by ``IStrategy.generate_signal()``.

    Parameters
    ----------
    strategy_name : str
        Name of the strategy that produced this signal.
    instrument_type : InstrumentType
        Type of instrument to trade.
    symbol : str
        Trading symbol (e.g. ``"NIFTY26AUG24000CE"``).
    underlying : str
        Underlying asset symbol (e.g. ``"NIFTY"``).
    direction : SignalDirection
        BUY or SELL.
    entry_price : float
        Suggested entry price (LTP at signal time).
    stop_loss : float
        Absolute stop-loss price level.
    targets : list[float]
        Ordered list of take-profit price levels (1st target, 2nd target …).
    quantity : int
        Number of units / lots.
    confidence : float
        Signal confidence in [0.0, 1.0].  Used by risk layer to gate orders.
    strength : SignalStrength
        Derived from confidence automatically on ``__post_init__``.
    rationale : list[str]
        Human-readable list of reasons / conditions that fired.
    expiry : Optional[str]
        Option/futures expiry date string ``"DDMMMYYYY"`` (e.g. ``"25JUL2024"``).
    strike : Optional[float]
        Option strike price.
    timestamp : datetime
        UTC timestamp when the signal was generated.
    metadata : dict[str, Any]
        Arbitrary extra data (IV rank, ATR value, etc.) for downstream use.
    """

    strategy_name: str
    instrument_type: InstrumentType
    symbol: str
    underlying: str
    direction: SignalDirection
    entry_price: float
    stop_loss: float
    targets: list[float]
    quantity: int
    confidence: float

    # Optional / derived fields
    strength: SignalStrength = field(init=False)
    rationale: list[str] = field(default_factory=list)
    expiry: Optional[str] = None
    strike: Optional[float] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate ranges and derive strength from confidence."""
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"confidence must be in [0.0, 1.0], got {self.confidence}"
            )
        if self.entry_price <= 0:
            raise ValueError(f"entry_price must be positive, got {self.entry_price}")
        if not self.targets:
            raise ValueError("At least one target must be specified.")

        # Derive qualitative strength
        if self.confidence >= 0.85:
            self.strength = SignalStrength.STRONG
        elif self.confidence >= 0.65:
            self.strength = SignalStrength.MODERATE
        else:
            self.strength = SignalStrength.WEAK

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    @property
    def risk(self) -> float:
        """Absolute risk per unit (|entry - stop_loss|)."""
        return abs(self.entry_price - self.stop_loss)

    @property
    def reward_to_risk(self) -> float:
        """R:R ratio to first target."""
        if self.risk == 0:
            return 0.0
        return abs(self.targets[0] - self.entry_price) / self.risk

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict (JSON-safe types)."""
        return {
            "strategy_name": self.strategy_name,
            "instrument_type": self.instrument_type.value,
            "symbol": self.symbol,
            "underlying": self.underlying,
            "direction": self.direction.value,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "targets": self.targets,
            "quantity": self.quantity,
            "confidence": round(self.confidence, 4),
            "strength": self.strength.value,
            "rationale": self.rationale,
            "expiry": self.expiry,
            "strike": self.strike,
            "timestamp": self.timestamp.isoformat(),
            "risk": round(self.risk, 2),
            "reward_to_risk": round(self.reward_to_risk, 2),
            "metadata": self.metadata,
        }

    def __repr__(self) -> str:
        return (
            f"<TradeSignal {self.strategy_name} | {self.direction.value} "
            f"{self.symbol} @ {self.entry_price} | "
            f"SL={self.stop_loss} T={self.targets} | "
            f"conf={self.confidence:.2f} [{self.strength.value}]>"
        )


# ---------------------------------------------------------------------------
# IStrategy – abstract base class
# ---------------------------------------------------------------------------


class IStrategy(ABC):
    """
    Abstract base for every F&O trading strategy.

    Subclasses MUST define:
      - ``strategy_name``      class-level str constant
      - ``supported_instruments`` class-level list of InstrumentType
      - ``generate_signal()``  async method
      - ``validate_config()``  sync method

    Class-level attributes (can be overridden per subclass)
    --------------------------------------------------------
    version : str
        Semantic version of the strategy implementation.
    is_enabled : bool
        Master on/off flag; the signal router checks this before calling
        ``generate_signal()``.
    min_confidence_threshold : float
        Minimum confidence below which the strategy should return ``None``
        instead of a weak signal.  Defaults to 0.60.
    """

    # Subclasses should override these class attributes -------------------
    strategy_name: str = "base_strategy"
    version: str = "1.0.0"
    is_enabled: bool = True
    supported_instruments: list[InstrumentType] = []
    min_confidence_threshold: float = 0.60
    # ---------------------------------------------------------------------

    def __init__(self, config: Optional[dict[str, Any]] = None) -> None:
        """
        Parameters
        ----------
        config : dict, optional
            Strategy-level configuration dict.  If ``None``, the constructor
            will attempt to load it via ``get_config()`` (YAML-based).
        """
        # Per-instance logger: strategies.<strategy_name>
        self._logger: logging.Logger = logging.getLogger(
            f"strategies.{self.strategy_name}"
        )

        # Config precedence: explicitly passed dict > YAML > empty dict
        if config is not None:
            self._config: dict[str, Any] = config
        else:
            try:
                self._config = self.get_config()
            except FileNotFoundError:
                self._logger.warning(
                    "Config file not found for strategy '%s'; using empty config.",
                    self.strategy_name,
                )
                self._config = {}

        # Validate the loaded config immediately
        if self._config:
            if not self.validate_config(self._config):
                raise ValueError(
                    f"Invalid configuration for strategy '{self.strategy_name}'. "
                    "Check validate_config() implementation."
                )

        self._logger.info(
            "Strategy '%s' v%s initialised. enabled=%s instruments=%s",
            self.strategy_name,
            self.version,
            self.is_enabled,
            [i.value for i in self.supported_instruments],
        )

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    async def generate_signal(
        self, context: dict[str, Any]
    ) -> Optional[TradeSignal]:
        """
        Core signal generation coroutine.

        Parameters
        ----------
        context : dict
            Market context bundle expected to contain (at minimum):

            =========================================  ===========================================
            Key                                        Description
            =========================================  ===========================================
            ``symbol``          str                    Trading symbol of the instrument
            ``underlying``      str                    Underlying asset name
            ``instrument_type`` InstrumentType         Instrument category
            ``ltp``             float                  Last traded price
            ``ohlcv``           list[dict]             OHLCV candle list (newest last)
            ``volume``          float                  Current bar volume
            ``avg_volume``      float                  Average volume (N-period)
            ``vwap``            float                  Current VWAP
            ``atr``             float                  ATR(14) value
            ``rsi``             float                  RSI(14) value
            ``adx``             float                  ADX(14) value
            ``ema_21``          float                  EMA(21)
            ``ema_50``          float                  EMA(50)
            ``ema_200``         float                  EMA(200)
            ``macd``            dict                   {"macd", "signal", "histogram"}
            ``regime``          MarketRegime           Current market regime
            ``has_news_block``  bool                   True if a news event blocks trading
            ``timestamp``       datetime               Bar close timestamp (IST)
            ``option_chain``    Optional[dict]         Option-chain snapshot (for options)
            ``iv_rank``         Optional[float]        IV rank [0–100] for options
            ``pcr``             Optional[float]        Put-call ratio
            ``dte``             Optional[int]          Days to expiry
            =========================================  ===========================================

        Returns
        -------
        Optional[TradeSignal]
            A fully populated ``TradeSignal`` or ``None`` if no signal fires.
        """
        ...

    @abstractmethod
    def validate_config(self, config: dict[str, Any]) -> bool:
        """
        Validate strategy-specific configuration.

        Parameters
        ----------
        config : dict
            Raw configuration dictionary.

        Returns
        -------
        bool
            ``True`` if the config is valid, ``False`` otherwise.
            Implementations should log specific validation failures.
        """
        ...

    # ------------------------------------------------------------------
    # Concrete helpers available to all subclasses
    # ------------------------------------------------------------------

    def get_config(self) -> dict[str, Any]:
        """
        Load and return this strategy's config section from the project YAML.

        The method searches for a config file in the following order:
          1. ``config/strategies.yaml``  (relative to CWD)
          2. ``config/strategies.yaml``  (relative to this file's parent-parent dir)

        Within the YAML the strategy stanza is keyed by ``strategy_name``.

        Returns
        -------
        dict
            Strategy-specific config dict (possibly empty).

        Raises
        ------
        FileNotFoundError
            If neither config path resolves to an existing file.
        """
        candidate_paths = [
            os.path.join(os.getcwd(), "config", "strategies.yaml"),
            os.path.join(
                os.path.dirname(__file__), "..", "config", "strategies.yaml"
            ),
        ]

        config_path: Optional[str] = None
        for path in candidate_paths:
            if os.path.isfile(path):
                config_path = path
                break

        if config_path is None:
            raise FileNotFoundError(
                f"strategies.yaml not found. Searched: {candidate_paths}"
            )

        with open(config_path, "r", encoding="utf-8") as fh:
            full_config: dict[str, Any] = yaml.safe_load(fh) or {}

        strategy_cfg: dict[str, Any] = full_config.get(self.strategy_name, {})
        self._logger.debug(
            "Loaded config for '%s' from '%s': %s",
            self.strategy_name,
            config_path,
            strategy_cfg,
        )
        return strategy_cfg

    # ------------------------------------------------------------------
    # Guard helpers
    # ------------------------------------------------------------------

    def is_instrument_supported(self, instrument_type: InstrumentType) -> bool:
        """Return True if the given instrument type is in ``supported_instruments``."""
        return instrument_type in self.supported_instruments

    def _check_enabled(self) -> bool:
        """
        Log a warning and return False if the strategy is disabled.
        Convenience guard to be called at the top of ``generate_signal()``.
        """
        if not self.is_enabled:
            self._logger.warning(
                "Strategy '%s' is disabled; skipping signal generation.",
                self.strategy_name,
            )
            return False
        return True

    def _check_instrument(self, instrument_type: InstrumentType) -> bool:
        """
        Log a warning and return False if ``instrument_type`` is not supported.
        """
        if not self.is_instrument_supported(instrument_type):
            self._logger.warning(
                "Strategy '%s' does not support instrument type '%s'. "
                "Supported: %s",
                self.strategy_name,
                instrument_type.value,
                [i.value for i in self.supported_instruments],
            )
            return False
        return True

    @staticmethod
    def _confidence_from_conditions(
        conditions_met: int, total_conditions: int
    ) -> float:
        """
        Compute a [0.0, 1.0] confidence score from a simple condition count.

        Parameters
        ----------
        conditions_met : int
            Number of conditions that evaluated to True.
        total_conditions : int
            Total number of conditions checked.

        Returns
        -------
        float
            Normalised confidence score.
        """
        if total_conditions == 0:
            return 0.0
        return round(conditions_met / total_conditions, 4)

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} "
            f"name='{self.strategy_name}' "
            f"version='{self.version}' "
            f"enabled={self.is_enabled} "
            f"instruments={[i.value for i in self.supported_instruments]}>"
        )

    def __str__(self) -> str:
        return (
            f"{self.strategy_name} v{self.version} "
            f"({'enabled' if self.is_enabled else 'disabled'})"
        )
