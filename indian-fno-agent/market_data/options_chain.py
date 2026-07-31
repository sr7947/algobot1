"""
market_data/options_chain.py
────────────────────────────
OptionsChainService — fetches, caches, and analyses NSE/BSE F&O option chains.

Provides:
  - Fetch & Redis caching (1-min TTL)
  - ATM strike calculation
  - PCR, Max Pain, IV Skew
  - Unusual OI buildup detection

Dependencies (install via pip):
    redis[asyncio]  pytz
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, Tuple

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Redis TTL
# ─────────────────────────────────────────────────────────────────────────────

CHAIN_CACHE_TTL_SECONDS = 60   # 1-minute TTL for option chain data

# ─────────────────────────────────────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OptionContract:
    """Represents a single option strike (call or put side)."""

    strike: float
    option_type: str        # "CE" or "PE"
    expiry: str             # ISO date string "YYYY-MM-DD"
    ltp: float              # Last Traded Price
    iv: float               # Implied Volatility (%)
    oi: int                 # Open Interest
    oi_change: int          # Change in OI since previous day
    volume: int
    bid: float = 0.0
    ask: float = 0.0
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0


@dataclass
class OptionChain:
    """
    Complete option chain for one underlying + expiry.
    Calls and puts are stored as parallel lists keyed by strike.
    """

    underlying: str
    exchange: str
    expiry: str             # "YYYY-MM-DD"
    spot_price: float
    calls: List[OptionContract] = field(default_factory=list)
    puts: List[OptionContract] = field(default_factory=list)
    fetched_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )

    # ── Convenience lookups ───────────────────────────────────────────────────

    def call_by_strike(self, strike: float) -> Optional[OptionContract]:
        """Return the call contract at the given strike, or None."""
        return next((c for c in self.calls if c.strike == strike), None)

    def put_by_strike(self, strike: float) -> Optional[OptionContract]:
        """Return the put contract at the given strike, or None."""
        return next((p for p in self.puts if p.strike == strike), None)

    @property
    def all_strikes(self) -> List[float]:
        """Sorted unique strike prices present in the chain."""
        strikes = {c.strike for c in self.calls} | {p.strike for p in self.puts}
        return sorted(strikes)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["fetched_at"] = self.fetched_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OptionChain":
        data = dict(data)
        data["fetched_at"] = datetime.fromisoformat(data["fetched_at"])
        data["calls"] = [OptionContract(**c) for c in data.get("calls", [])]
        data["puts"] = [OptionContract(**p) for p in data.get("puts", [])]
        return cls(**data)


# ─────────────────────────────────────────────────────────────────────────────
# Analytics result types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PCRResult:
    simple_pcr: float           # Total Put OI / Total Call OI
    weighted_pcr: float         # OI-weighted version
    total_call_oi: int
    total_put_oi: int


@dataclass
class IVSkewPoint:
    strike: float
    call_iv: float
    put_iv: float
    skew: float                 # put_iv - call_iv


@dataclass
class UnusualOIAlert:
    strike: float
    option_type: str            # "CE" or "PE"
    oi_change: int
    oi_change_pct: float        # percentage change vs. previous OI
    current_oi: int


@dataclass
class GreeksSummary:
    net_delta: float            # Call delta exposure - Put delta exposure
    net_gamma: float
    total_call_delta: float
    total_put_delta: float
    max_gamma_strike: float     # Strike with highest gamma (pin risk)


# ─────────────────────────────────────────────────────────────────────────────
# Broker Adapter Protocol
# ─────────────────────────────────────────────────────────────────────────────

class BrokerAdapter(Protocol):
    """Structural protocol — broker must implement these methods."""

    async def get_option_chain(
        self,
        underlying: str,
        expiry: str,
        exchange: str,
    ) -> OptionChain:
        """Fetch a complete option chain from the broker."""
        ...


# ─────────────────────────────────────────────────────────────────────────────
# Service
# ─────────────────────────────────────────────────────────────────────────────

class OptionsChainService:
    """
    Fetches, caches, and analyses NSE/BSE F&O option chains.

    Usage::

        service = OptionsChainService(broker=adapter, redis_url="redis://localhost:6379/0")
        await service.connect()

        chain = await service.fetch_chain("NIFTY", "2024-11-28", "NFO")
        atm   = service.get_atm_strike(chain.spot_price)
        pcr   = service.calculate_pcr(chain)
        pain  = service.find_max_pain(chain)

        await service.disconnect()
    """

    def __init__(
        self,
        broker: BrokerAdapter,
        redis_url: str = "redis://localhost:6379/0",
    ) -> None:
        self._broker = broker
        self._redis_url = redis_url
        self._redis: Optional[aioredis.Redis] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Initialise Redis connection."""
        self._redis = aioredis.from_url(
            self._redis_url, encoding="utf-8", decode_responses=True
        )
        logger.info("OptionsChainService connected to Redis.")

    async def disconnect(self) -> None:
        """Close Redis connection."""
        if self._redis:
            await self._redis.aclose()
            self._redis = None
        logger.info("OptionsChainService disconnected.")

    # ── Cache Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _cache_key(underlying: str, expiry: str, exchange: str) -> str:
        return f"optchain:{underlying}:{exchange}:{expiry}"

    async def _load_from_cache(
        self, underlying: str, expiry: str, exchange: str
    ) -> Optional[OptionChain]:
        """Try to load a chain from Redis; returns None on miss or error."""
        if not self._redis:
            return None
        key = self._cache_key(underlying, expiry, exchange)
        raw = await self._redis.get(key)
        if not raw:
            return None
        try:
            return OptionChain.from_dict(json.loads(raw))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Corrupt options chain cache for %s: %s", key, exc)
            return None

    async def _store_in_cache(self, chain: OptionChain) -> None:
        """Persist a chain to Redis with CHAIN_CACHE_TTL_SECONDS TTL."""
        if not self._redis:
            return
        key = self._cache_key(chain.underlying, chain.expiry, chain.exchange)
        await self._redis.set(
            key, json.dumps(chain.to_dict()), ex=CHAIN_CACHE_TTL_SECONDS
        )
        logger.debug("Cached option chain for %s @ %s.", chain.underlying, chain.expiry)

    # ── Core Fetch ────────────────────────────────────────────────────────────

    async def fetch_chain(
        self,
        underlying: str,
        expiry: str,
        exchange: str = "NFO",
        force_refresh: bool = False,
    ) -> OptionChain:
        """
        Fetch option chain from cache (if fresh) or broker.

        Args:
            underlying:    Index/stock name e.g. "NIFTY", "BANKNIFTY", "RELIANCE"
            expiry:        Expiry date string "YYYY-MM-DD"
            exchange:      Exchange segment ("NFO", "BSE", "MCX")
            force_refresh: If True, skip cache and fetch from broker directly.

        Returns:
            OptionChain dataclass populated with all strike data.
        """
        if not force_refresh:
            cached = await self._load_from_cache(underlying, expiry, exchange)
            if cached:
                logger.debug(
                    "Option chain cache hit for %s %s.", underlying, expiry
                )
                return cached

        logger.info("Fetching option chain from broker: %s %s.", underlying, expiry)
        chain = await self._broker.get_option_chain(underlying, expiry, exchange)

        # Normalise: ensure calls/puts are sorted by strike
        chain.calls.sort(key=lambda c: c.strike)
        chain.puts.sort(key=lambda p: p.strike)

        await self._store_in_cache(chain)
        return chain

    # ── Strike Utilities ──────────────────────────────────────────────────────

    @staticmethod
    def get_atm_strike(spot_price: float, step: float = 50.0) -> float:
        """
        Round spot_price to the nearest valid strike, given the strike step.

        For NIFTY / BANKNIFTY step is typically 50 or 100.
        For mid-cap stocks it may be 5 or 10.

        Args:
            spot_price: Current spot/index price.
            step:       Strike interval (default 50 for NIFTY).

        Returns:
            Nearest ATM strike as a float.
        """
        return round(spot_price / step) * step

    def get_strikes_around_atm(
        self,
        chain: OptionChain,
        n: int = 5,
        step: float = 50.0,
    ) -> Dict[str, List[float]]:
        """
        Return n strikes above and below the ATM strike.

        Args:
            chain: Populated OptionChain.
            n:     Number of strikes on each side.
            step:  Strike step (default 50).

        Returns:
            Dict with keys 'atm', 'itm_calls' (above atm), 'otm_calls' (below atm),
            'itm_puts' (below atm), 'otm_puts' (above atm), and 'selected_strikes'.
        """
        atm = self.get_atm_strike(chain.spot_price, step)
        all_strikes = chain.all_strikes

        if not all_strikes:
            return {"atm": atm, "above": [], "below": [], "selected_strikes": []}

        # Use available strikes from the chain (more accurate than synthetic)
        atm_idx = min(
            range(len(all_strikes)),
            key=lambda i: abs(all_strikes[i] - atm),
        )

        below = all_strikes[max(0, atm_idx - n): atm_idx]
        above = all_strikes[atm_idx + 1: atm_idx + 1 + n]
        selected = below + [all_strikes[atm_idx]] + above

        return {
            "atm": all_strikes[atm_idx],
            "above": above,
            "below": below,
            "selected_strikes": selected,
        }

    # ── PCR ───────────────────────────────────────────────────────────────────

    def calculate_pcr(self, chain: OptionChain) -> PCRResult:
        """
        Calculate Put-Call Ratio (both simple and OI-weighted).

        Simple  PCR = Sum(Put OI) / Sum(Call OI)
        Weighted PCR uses OI as weight and is computed per-strike.

        Args:
            chain: Populated OptionChain.

        Returns:
            PCRResult dataclass.
        """
        total_call_oi = sum(c.oi for c in chain.calls)
        total_put_oi = sum(p.oi for p in chain.puts)

        simple_pcr = (
            total_put_oi / total_call_oi if total_call_oi > 0 else 0.0
        )

        # Weighted PCR: weight by the average OI at each strike
        strike_map: Dict[float, Dict[str, int]] = {}
        for c in chain.calls:
            strike_map.setdefault(c.strike, {"call": 0, "put": 0})["call"] += c.oi
        for p in chain.puts:
            strike_map.setdefault(p.strike, {"call": 0, "put": 0})["put"] += p.oi

        w_call = w_put = 0.0
        for v in strike_map.values():
            total = v["call"] + v["put"]
            if total > 0:
                w = total
                w_call += v["call"] * w
                w_put  += v["put"]  * w

        w_total = w_call + w_put
        weighted_pcr = (w_put / w_call) if w_call > 0 else 0.0

        return PCRResult(
            simple_pcr=round(simple_pcr, 4),
            weighted_pcr=round(weighted_pcr, 4),
            total_call_oi=total_call_oi,
            total_put_oi=total_put_oi,
        )

    # ── Max Pain ──────────────────────────────────────────────────────────────

    def find_max_pain(self, chain: OptionChain) -> float:
        """
        Compute the Max Pain strike — the strike price at which option writers
        experience the minimum loss (i.e., buyers suffer maximum loss).

        Algorithm:
          For each candidate expiry strike K:
            pain(K) = Sum over all calls [max(0, K - strike) * call_oi]
                    + Sum over all puts  [max(0, strike - K) * put_oi]
          Max Pain = argmin pain(K)

        Args:
            chain: Populated OptionChain with OI data.

        Returns:
            The max-pain strike as a float.
        """
        strikes = chain.all_strikes
        if not strikes:
            return chain.spot_price

        call_data: List[Tuple[float, int]] = [(c.strike, c.oi) for c in chain.calls]
        put_data:  List[Tuple[float, int]] = [(p.strike, p.oi) for p in chain.puts]

        min_pain = float("inf")
        max_pain_strike = strikes[0]

        for candidate in strikes:
            # Call writers' loss: in-the-money calls (candidate > strike)
            call_pain = sum(
                max(0.0, candidate - s) * oi for s, oi in call_data
            )
            # Put writers' loss: in-the-money puts (candidate < strike)
            put_pain = sum(
                max(0.0, s - candidate) * oi for s, oi in put_data
            )
            total_pain = call_pain + put_pain

            if total_pain < min_pain:
                min_pain = total_pain
                max_pain_strike = candidate

        logger.debug("Max pain strike: %.0f (pain=%.0f)", max_pain_strike, min_pain)
        return max_pain_strike

    # ── IV Skew ───────────────────────────────────────────────────────────────

    def get_iv_skew(self, chain: OptionChain) -> List[IVSkewPoint]:
        """
        Build the IV skew table — call IV, put IV, and put-call IV skew
        for every strike that has both call and put data.

        A positive skew means puts have higher IV than calls at that strike
        (typical for equity indices — downside fear premium).

        Args:
            chain: Populated OptionChain with IV data.

        Returns:
            List of IVSkewPoint sorted by strike ascending.
        """
        call_iv_map = {c.strike: c.iv for c in chain.calls}
        put_iv_map  = {p.strike: p.iv for p in chain.puts}

        common_strikes = sorted(
            set(call_iv_map.keys()) & set(put_iv_map.keys())
        )
        skew: List[IVSkewPoint] = []
        for s in common_strikes:
            c_iv = call_iv_map[s]
            p_iv = put_iv_map[s]
            skew.append(
                IVSkewPoint(
                    strike=s,
                    call_iv=round(c_iv, 2),
                    put_iv=round(p_iv, 2),
                    skew=round(p_iv - c_iv, 2),
                )
            )
        return skew

    # ── Unusual OI Buildup ────────────────────────────────────────────────────

    def detect_unusual_oi_buildup(
        self,
        chain: OptionChain,
        threshold_pct: float = 20.0,
    ) -> List[UnusualOIAlert]:
        """
        Flag option contracts where OI has increased by more than
        threshold_pct compared to the previous day's OI.

        Only considers positive OI change (fresh buildup, not unwinding).

        Args:
            chain:         Populated OptionChain with oi_change data.
            threshold_pct: Minimum % OI change to flag as unusual (default 20%).

        Returns:
            List of UnusualOIAlert sorted by oi_change_pct descending.
        """
        alerts: List[UnusualOIAlert] = []

        all_contracts: List[OptionContract] = chain.calls + chain.puts
        for contract in all_contracts:
            if contract.oi_change <= 0:
                continue  # Skip unwinding

            # Previous OI = current OI - OI change
            prev_oi = contract.oi - contract.oi_change
            if prev_oi <= 0:
                # New strike; treat entire OI as new buildup
                change_pct = 100.0
            else:
                change_pct = (contract.oi_change / prev_oi) * 100.0

            if change_pct >= threshold_pct:
                alerts.append(
                    UnusualOIAlert(
                        strike=contract.strike,
                        option_type=contract.option_type,
                        oi_change=contract.oi_change,
                        oi_change_pct=round(change_pct, 2),
                        current_oi=contract.oi,
                    )
                )

        alerts.sort(key=lambda a: a.oi_change_pct, reverse=True)
        logger.debug(
            "Detected %d unusual OI alerts (threshold %.1f%%).",
            len(alerts), threshold_pct,
        )
        return alerts

    # ── Greeks Summary ────────────────────────────────────────────────────────

    def get_option_greeks_summary(self, chain: OptionChain) -> GreeksSummary:
        """
        Aggregate delta and gamma exposure across the entire chain.

        Call deltas are positive; put deltas are negative by convention.
        Net delta > 0 implies bullish dealer positioning.
        High gamma at a strike implies potential pinning around that level.

        Args:
            chain: Populated OptionChain with greeks data.

        Returns:
            GreeksSummary dataclass.
        """
        total_call_delta = sum(c.delta * c.oi for c in chain.calls if c.oi > 0)
        total_put_delta  = sum(abs(p.delta) * p.oi for p in chain.puts if p.oi > 0)
        net_delta = total_call_delta - total_put_delta
        net_gamma = sum(c.gamma * c.oi for c in chain.calls) + \
                    sum(p.gamma * p.oi for p in chain.puts)

        # Find the strike with highest combined gamma exposure (pin risk)
        gamma_by_strike: Dict[float, float] = {}
        for c in chain.calls:
            gamma_by_strike[c.strike] = gamma_by_strike.get(c.strike, 0.0) + c.gamma * c.oi
        for p in chain.puts:
            gamma_by_strike[p.strike] = gamma_by_strike.get(p.strike, 0.0) + p.gamma * p.oi

        max_gamma_strike = max(gamma_by_strike, key=gamma_by_strike.get) \
            if gamma_by_strike else chain.spot_price

        return GreeksSummary(
            net_delta=round(net_delta, 2),
            net_gamma=round(net_gamma, 4),
            total_call_delta=round(total_call_delta, 2),
            total_put_delta=round(total_put_delta, 2),
            max_gamma_strike=max_gamma_strike,
        )

    # ── PCR Interpretation ────────────────────────────────────────────────────

    @staticmethod
    def interpret_pcr(pcr: float) -> str:
        """
        Interpret a PCR value into a directional signal.

        Heuristics (standard market interpretation):
          PCR > 1.2  ->  bullish (excessive put writing / hedging)
          PCR < 0.7  ->  bearish (excessive call writing)
          Otherwise  ->  neutral

        Args:
            pcr: Put-Call Ratio value.

        Returns:
            One of: 'bullish', 'bearish', 'neutral'
        """
        if pcr > 1.2:
            return "bullish"
        if pcr < 0.7:
            return "bearish"
        return "neutral"

    # ── OI Delta ──────────────────────────────────────────────────────────────

    @staticmethod
    def get_oi_delta(chain: OptionChain) -> Dict[str, int]:
        """
        Compute total OI change for calls vs puts.

        Returns:
            Dict with keys 'call_oi_change', 'put_oi_change', 'net_oi_change'.
            Positive net_oi_change = more put OI being added (bearish tilt).
        """
        call_oi_change = sum(c.oi_change for c in chain.calls)
        put_oi_change  = sum(p.oi_change for p in chain.puts)
        return {
            "call_oi_change": call_oi_change,
            "put_oi_change": put_oi_change,
            "net_oi_change": put_oi_change - call_oi_change,
        }
