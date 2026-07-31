"""
indicators/options.py
─────────────────────
OptionsAnalyticsEngine — computes options-specific analytics for
Indian F&O trading strategy.

Provides:
  - PCR (simple + weighted) and interpretation
  - OI delta (call OI change vs put OI change)
  - IV Rank / IV Percentile
  - Writing activity detection
  - Option greeks summary

Dependencies (install via pip):
    (none beyond standard library — data structures come from options_chain.py)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Shared types (mirrors market_data.options_chain)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OptionContract:
    """Mirrors OptionContract from market_data.options_chain."""
    strike: float
    option_type: str        # "CE" or "PE"
    expiry: str
    ltp: float
    iv: float
    oi: int
    oi_change: int
    volume: int
    bid: float = 0.0
    ask: float = 0.0
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0


@dataclass
class OptionChain:
    """Mirrors OptionChain from market_data.options_chain."""
    underlying: str
    exchange: str
    expiry: str
    spot_price: float
    calls: List[OptionContract] = field(default_factory=list)
    puts:  List[OptionContract] = field(default_factory=list)

    @property
    def all_strikes(self) -> List[float]:
        strikes = {c.strike for c in self.calls} | {p.strike for p in self.puts}
        return sorted(strikes)


# ─────────────────────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PCRResult:
    """Put-Call Ratio analysis results."""
    simple_pcr: float           # Total Put OI / Total Call OI
    weighted_pcr: float         # OI-weighted PCR
    total_call_oi: int
    total_put_oi: int
    signal: str                 # 'bullish', 'bearish', 'neutral'


@dataclass
class IVRankResult:
    """IV Rank / Percentile analysis."""
    current_iv: float
    iv_rank: float              # (current_iv - min_iv) / (max_iv - min_iv) * 100
    iv_percentile: float        # % of historical IVs that are below current_iv
    min_iv: float
    max_iv: float
    mean_iv: float
    interpretation: str         # 'high_iv', 'low_iv', 'normal_iv'


@dataclass
class WritingActivity:
    """Detected option writing activity at a strike."""
    strike: float
    option_type: str            # "CE" or "PE"
    oi: int
    oi_change: int
    ltp: float
    volume: int
    # Writing signal strength: higher = more likely writing
    conviction_score: float


@dataclass
class GreeksSummary:
    """Aggregated greeks exposure across the chain."""
    net_delta: float
    net_gamma: float
    total_call_delta: float
    total_put_delta: float
    total_theta: float          # Time decay — usually negative for holders
    total_vega: float           # Volatility exposure
    max_gamma_strike: float     # Highest combined gamma (pin risk)
    dealer_positioning: str     # 'long_gamma', 'short_gamma', 'neutral_gamma'


# ─────────────────────────────────────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────────────────────────────────────

class OptionsAnalyticsEngine:
    """
    Computes options analytics for F&O strategy decision-making.

    All methods are pure functions (no I/O, no state) and operate on
    OptionChain dataclass instances.

    Usage::

        engine = OptionsAnalyticsEngine()
        pcr    = engine.calculate_pcr(chain)
        print(pcr.simple_pcr, pcr.signal)

        writing = engine.detect_writing_activity(chain)
        greeks  = engine.get_option_greeks_summary(chain)
    """

    # ── PCR ───────────────────────────────────────────────────────────────────

    def calculate_pcr(self, chain: OptionChain) -> PCRResult:
        """
        Calculate Put-Call Ratio (simple and OI-weighted).

        Simple PCR  = Sum(Put OI) / Sum(Call OI)
        Weighted PCR = Per-strike OI-weighted put-call ratio.
                       Gives more weight to heavily traded strikes.

        Args:
            chain: Populated OptionChain.

        Returns:
            PCRResult with both PCR values and directional signal.
        """
        total_call_oi = sum(c.oi for c in chain.calls)
        total_put_oi  = sum(p.oi for p in chain.puts)

        simple_pcr = (
            total_put_oi / total_call_oi if total_call_oi > 0 else 0.0
        )

        # ── Weighted PCR ──────────────────────────────────────────────────────
        # Weight each strike by (call_oi + put_oi) at that strike.
        # w_PCR = sum(put_oi_i * weight_i) / sum(call_oi_i * weight_i)
        strike_data: Dict[float, Dict[str, int]] = {}
        for c in chain.calls:
            strike_data.setdefault(c.strike, {"call": 0, "put": 0})["call"] += c.oi
        for p in chain.puts:
            strike_data.setdefault(p.strike, {"call": 0, "put": 0})["put"] += p.oi

        w_call = w_put = 0.0
        for v in strike_data.values():
            weight = float(v["call"] + v["put"])
            w_call += v["call"] * weight
            w_put  += v["put"]  * weight

        weighted_pcr = (w_put / w_call) if w_call > 0 else 0.0

        signal = self.interpret_pcr(simple_pcr)

        return PCRResult(
            simple_pcr=round(simple_pcr, 4),
            weighted_pcr=round(weighted_pcr, 4),
            total_call_oi=total_call_oi,
            total_put_oi=total_put_oi,
            signal=signal,
        )

    @staticmethod
    def interpret_pcr(pcr: float) -> str:
        """
        Interpret PCR into a directional market signal.

        Thresholds (NSE conventional interpretation):
          PCR > 1.3  ->  'bullish'  (excessive put buying / hedging = contrarian buy)
          PCR > 1.0  ->  'mildly_bullish'
          PCR < 0.6  ->  'bearish'  (excessive call buying = contrarian sell)
          PCR < 0.8  ->  'mildly_bearish'
          Otherwise  ->  'neutral'

        Args:
            pcr: Float PCR value.

        Returns:
            Signal string.
        """
        if pcr > 1.3:
            return "bullish"
        if pcr > 1.0:
            return "mildly_bullish"
        if pcr < 0.6:
            return "bearish"
        if pcr < 0.8:
            return "mildly_bearish"
        return "neutral"

    # ── OI Delta ──────────────────────────────────────────────────────────────

    @staticmethod
    def get_oi_delta(chain: OptionChain) -> Dict[str, Any]:
        """
        Compute the difference in OI build-up between calls and puts.

        A large positive net_oi_delta (put OI rising faster) typically
        signals bearish hedging activity.

        Args:
            chain: Populated OptionChain with oi_change data.

        Returns:
            Dict containing:
              call_oi_change  : Total call OI change
              put_oi_change   : Total put OI change
              net_oi_delta    : put_oi_change - call_oi_change
              bias            : 'put_heavy', 'call_heavy', 'balanced'
        """
        call_delta = sum(c.oi_change for c in chain.calls)
        put_delta  = sum(p.oi_change for p in chain.puts)
        net = put_delta - call_delta

        if net > 0:
            bias = "put_heavy"     # More puts being written/bought
        elif net < 0:
            bias = "call_heavy"    # More calls being written/bought
        else:
            bias = "balanced"

        return {
            "call_oi_change": call_delta,
            "put_oi_change": put_delta,
            "net_oi_delta": net,
            "bias": bias,
        }

    # ── IV Rank ───────────────────────────────────────────────────────────────

    @staticmethod
    def calculate_iv_rank(
        current_iv: float,
        historical_ivs: List[float],
    ) -> IVRankResult:
        """
        Compute IV Rank and IV Percentile from historical IV data.

        IV Rank      = (current_iv - min_iv) / (max_iv - min_iv) * 100
        IV Percentile = % of historical IVs that were BELOW current_iv

        Interpretation:
          IV Rank > 80  ->  'high_iv'   — options are expensive; prefer selling
          IV Rank < 20  ->  'low_iv'    — options are cheap;     prefer buying
          Otherwise     ->  'normal_iv'

        Args:
            current_iv:     Current IV (e.g. 15.5 for 15.5%).
            historical_ivs: List of historical IV readings (same unit).

        Returns:
            IVRankResult dataclass.

        Raises:
            ValueError: If historical_ivs is empty.
        """
        if not historical_ivs:
            raise ValueError("historical_ivs cannot be empty.")

        min_iv  = min(historical_ivs)
        max_iv  = max(historical_ivs)
        mean_iv = sum(historical_ivs) / len(historical_ivs)

        # IV Rank
        iv_range = max_iv - min_iv
        iv_rank = (
            ((current_iv - min_iv) / iv_range) * 100.0
            if iv_range > 0
            else 50.0
        )
        iv_rank = max(0.0, min(100.0, iv_rank))

        # IV Percentile
        below_count = sum(1 for iv in historical_ivs if iv < current_iv)
        iv_percentile = (below_count / len(historical_ivs)) * 100.0

        # Interpretation
        if iv_rank > 80:
            interpretation = "high_iv"
        elif iv_rank < 20:
            interpretation = "low_iv"
        else:
            interpretation = "normal_iv"

        return IVRankResult(
            current_iv=round(current_iv, 4),
            iv_rank=round(iv_rank, 2),
            iv_percentile=round(iv_percentile, 2),
            min_iv=round(min_iv, 4),
            max_iv=round(max_iv, 4),
            mean_iv=round(mean_iv, 4),
            interpretation=interpretation,
        )

    # ── Writing Activity Detection ────────────────────────────────────────────

    def detect_writing_activity(
        self,
        chain: OptionChain,
        min_oi: int = 10_000,
        min_volume: int = 1_000,
    ) -> List[WritingActivity]:
        """
        Identify strikes where heavy option writing activity is occurring.

        Writing signals (premium sellers):
          1. High OI (new positions being added)
          2. Positive OI change (OI increasing)
          3. LTP declining (premium being collected / eroding)
          4. High volume (active participation)

        Conviction score = normalised combination of OI change, volume, and OI.

        Args:
            chain:      Populated OptionChain.
            min_oi:     Minimum OI to consider a strike (filter noise).
            min_volume: Minimum volume to consider a strike.

        Returns:
            List of WritingActivity sorted by conviction_score descending.
        """
        candidates: List[WritingActivity] = []
        all_contracts: List[OptionContract] = chain.calls + chain.puts

        # Normalisation basis
        max_oi     = max((c.oi for c in all_contracts), default=1) or 1
        max_volume = max((c.volume for c in all_contracts), default=1) or 1
        max_oi_chg = max((c.oi_change for c in all_contracts if c.oi_change > 0), default=1) or 1

        for contract in all_contracts:
            # Filter: only consider contracts with meaningful activity
            if contract.oi < min_oi or contract.volume < min_volume:
                continue
            if contract.oi_change <= 0:
                continue   # OI must be growing for fresh writing

            # Score components (each 0–1)
            oi_score  = min(contract.oi / max_oi, 1.0)
            vol_score = min(contract.volume / max_volume, 1.0)
            chg_score = min(contract.oi_change / max_oi_chg, 1.0)

            # Writers sell premium; LTP falling is consistent with writing
            # We use a heuristic: if bid < (ask * 0.9), premium is compressing
            price_compression_bonus = (
                0.1 if (contract.bid > 0 and contract.ltp < contract.bid * 1.05)
                else 0.0
            )

            conviction = (oi_score * 0.4 + vol_score * 0.3 + chg_score * 0.3) + price_compression_bonus

            candidates.append(
                WritingActivity(
                    strike=contract.strike,
                    option_type=contract.option_type,
                    oi=contract.oi,
                    oi_change=contract.oi_change,
                    ltp=contract.ltp,
                    volume=contract.volume,
                    conviction_score=round(min(conviction, 1.0), 4),
                )
            )

        candidates.sort(key=lambda w: w.conviction_score, reverse=True)
        logger.debug(
            "Detected %d writing candidates for %s.", len(candidates), chain.underlying
        )
        return candidates

    # ── Greeks Summary ────────────────────────────────────────────────────────

    def get_option_greeks_summary(self, chain: OptionChain) -> GreeksSummary:
        """
        Aggregate delta, gamma, theta, and vega across the entire option chain.

        Interpretation:
          net_delta   > 0  → dealers are net long delta (bullish tilt)
          dealer_positioning:
            long_gamma  → net gamma > 0 (market makers are long gamma, stabilising)
            short_gamma → net gamma < 0 (market makers are short gamma, amplifying)

        Args:
            chain: Populated OptionChain with greeks data.

        Returns:
            GreeksSummary dataclass.
        """
        # Calls: positive delta & gamma; puts: negative delta, positive gamma
        total_call_delta = sum(c.delta * c.oi for c in chain.calls if c.oi > 0)
        total_put_delta  = sum(p.delta * p.oi for p in chain.puts  if p.oi > 0)
        net_delta = total_call_delta + total_put_delta   # put delta is already negative

        net_gamma = (
            sum(c.gamma * c.oi for c in chain.calls if c.oi > 0) +
            sum(p.gamma * p.oi for p in chain.puts  if p.oi > 0)
        )

        total_theta = (
            sum(c.theta * c.oi for c in chain.calls if c.oi > 0) +
            sum(p.theta * p.oi for p in chain.puts  if p.oi > 0)
        )

        total_vega = (
            sum(c.vega * c.oi for c in chain.calls if c.oi > 0) +
            sum(p.vega * p.oi for p in chain.puts  if p.oi > 0)
        )

        # Find the strike with the highest combined gamma (pin risk / gamma wall)
        gamma_by_strike: Dict[float, float] = {}
        for c in chain.calls:
            gamma_by_strike[c.strike] = gamma_by_strike.get(c.strike, 0.0) + c.gamma * c.oi
        for p in chain.puts:
            gamma_by_strike[p.strike] = gamma_by_strike.get(p.strike, 0.0) + p.gamma * p.oi

        max_gamma_strike = (
            max(gamma_by_strike, key=gamma_by_strike.get)
            if gamma_by_strike else chain.spot_price
        )

        # Dealer positioning heuristic
        if net_gamma > 0:
            dealer_positioning = "long_gamma"
        elif net_gamma < 0:
            dealer_positioning = "short_gamma"
        else:
            dealer_positioning = "neutral_gamma"

        return GreeksSummary(
            net_delta=round(net_delta, 4),
            net_gamma=round(net_gamma, 6),
            total_call_delta=round(total_call_delta, 4),
            total_put_delta=round(total_put_delta, 4),
            total_theta=round(total_theta, 4),
            total_vega=round(total_vega, 4),
            max_gamma_strike=max_gamma_strike,
            dealer_positioning=dealer_positioning,
        )
