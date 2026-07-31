"""
agents/options_agent.py
-----------------------
Derives directional bias and key price levels from the option chain.

Expected context keys
---------------------
option_chain : list[dict]
    Each entry represents one strike with fields:
        strike          : float
        call_oi         : int     — call open interest
        put_oi          : int     — put open interest
        call_oi_change  : int     — change in call OI vs previous session
        put_oi_change   : int     — change in put OI vs previous session
        call_iv         : float   — implied volatility of call (in %)
        put_iv          : float   — implied volatility of put  (in %)
        call_ltp        : float   — last traded price of call
        put_ltp         : float   — last traded price of put
        expiry          : str     — expiry date (YYYY-MM-DD)

spot_price  : float — current underlying spot price
iv_rank     : float (optional) — pre-computed IV rank (0–100)
                                   0 = historically low IV
                                  100 = historically high IV

iv_percentile : float (optional) — IV percentile (alternative to IV rank)
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from agents.base_agent import BaseAgent

logger = structlog.get_logger(__name__)

# ------------------------------------------------------------------ #
# Threshold constants                                                 #
# ------------------------------------------------------------------ #
PCR_BULLISH_THRESHOLD: float = 1.2   # PCR > 1.2 → contrarian bullish
PCR_BEARISH_THRESHOLD: float = 0.8   # PCR < 0.8 → contrarian bearish
IV_RANK_HIGH: float = 70.0           # IV rank > 70 → prefer selling premium
IV_RANK_LOW: float  = 30.0           # IV rank < 30 → prefer buying options
UNUSUAL_OI_MULTIPLIER: float = 3.0   # OI change > 3× median → unusual


class OptionsAgent(BaseAgent):
    """
    Derives directional bias from option chain data:

    - Put-Call Ratio (PCR)           — contrarian sentiment indicator
    - OI Wall analysis               — support/resistance from OI concentration
    - Max Pain                       — strike where options sellers profit most
    - IV Rank                        — determines option strategy preference
    - Unusual OI Activity            — detects smart money positioning
    """

    agent_name: str = "options_agent"
    weight: float = 0.25

    # ------------------------------------------------------------------ #
    # Public interface                                                     #
    # ------------------------------------------------------------------ #

    async def analyze(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        Analyze the option chain and return a structured options signal.

        Returns
        -------
        dict
            {
              "agent"          : "options_agent",
              "options_score"  : float (0–1),
              "confidence"     : float (= options_score),
              "pcr"            : float,
              "max_pain"       : float,
              "iv_rank"        : float,
              "bias"           : "BULLISH" | "BEARISH" | "NEUTRAL",
              "key_levels"     : {
                  "call_wall"        : float,
                  "put_wall"         : float,
                  "unusual_activity" : list[dict],
              },
              "strategy_hint"  : str,   e.g. "SELL_OPTIONS" / "BUY_OPTIONS"
              "reasons"        : list[str],
              "latency_ms"     : float,
            }
        """
        start = time.monotonic()
        self._log.info("options_analysis_start")

        chain: list[dict] = context.get("option_chain", [])
        spot: float = float(context.get("spot_price", 0.0))
        iv_rank: float = float(context.get("iv_rank", -1.0))
        iv_percentile: float = float(context.get("iv_percentile", -1.0))

        reasons: list[str] = []

        if not chain or spot <= 0:
            self._log.warning("options_insufficient_data", chain_len=len(chain), spot=spot)
            result: dict[str, Any] = {
                "options_score": 0.5,
                "confidence"   : 0.5,
                "pcr"          : 1.0,
                "max_pain"     : spot,
                "iv_rank"      : iv_rank if iv_rank >= 0 else iv_percentile,
                "bias"         : "NEUTRAL",
                "key_levels"   : {
                    "call_wall": 0.0,
                    "put_wall" : 0.0,
                    "unusual_activity": [],
                },
                "strategy_hint": "NO_DATA",
                "reasons"      : ["Insufficient option chain data"],
            }
            return self._timed_result(result, start)

        # ----------------------------------------------------------------
        # 1. Put-Call Ratio
        # ----------------------------------------------------------------
        pcr, pcr_bias, pcr_score, pcr_reasons = self._compute_pcr(chain)
        reasons.extend(pcr_reasons)

        # ----------------------------------------------------------------
        # 2. OI Walls (call wall = resistance, put wall = support)
        # ----------------------------------------------------------------
        call_wall, put_wall, oi_reasons = self._compute_oi_walls(chain, spot)
        reasons.extend(oi_reasons)

        # ----------------------------------------------------------------
        # 3. Max Pain
        # ----------------------------------------------------------------
        max_pain, mp_reasons = self._compute_max_pain(chain)
        reasons.extend(mp_reasons)
        max_pain_bias = self._max_pain_bias(max_pain, spot)
        if max_pain_bias == "BULLISH":
            reasons.append(
                f"Max pain ({max_pain:.2f}) is ABOVE spot ({spot:.2f}) "
                "— price may drift up toward expiry"
            )
        elif max_pain_bias == "BEARISH":
            reasons.append(
                f"Max pain ({max_pain:.2f}) is BELOW spot ({spot:.2f}) "
                "— price may drift down toward expiry"
            )

        # ----------------------------------------------------------------
        # 4. IV Rank / Strategy preference
        # ----------------------------------------------------------------
        effective_iv_rank = iv_rank if iv_rank >= 0 else iv_percentile
        strategy_hint, iv_reasons = self._iv_strategy(effective_iv_rank)
        reasons.extend(iv_reasons)

        # ----------------------------------------------------------------
        # 5. Unusual OI Activity
        # ----------------------------------------------------------------
        unusual_strikes, unusual_reasons = self._detect_unusual_oi(chain)
        reasons.extend(unusual_reasons)

        # ----------------------------------------------------------------
        # Aggregate score and bias
        # ----------------------------------------------------------------
        bias_scores: dict[str, float] = {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0}

        # PCR contribution
        if pcr_bias == "BULLISH":
            bias_scores["BULLISH"] += pcr_score
        elif pcr_bias == "BEARISH":
            bias_scores["BEARISH"] += pcr_score
        else:
            bias_scores["NEUTRAL"] += pcr_score

        # Max pain contribution
        if max_pain_bias == "BULLISH":
            bias_scores["BULLISH"] += 0.15
        elif max_pain_bias == "BEARISH":
            bias_scores["BEARISH"] += 0.15

        # OI wall contribution (price between walls is mildly bullish if below mid)
        if call_wall > 0 and put_wall > 0:
            mid_oi = (call_wall + put_wall) / 2.0
            if spot > mid_oi:
                bias_scores["BULLISH"] += 0.10
                reasons.append(
                    f"Spot ({spot:.2f}) above OI mid-range ({mid_oi:.2f})"
                )
            else:
                bias_scores["BEARISH"] += 0.10
                reasons.append(
                    f"Spot ({spot:.2f}) below OI mid-range ({mid_oi:.2f})"
                )

        # Determine dominant bias
        dominant_bias = max(bias_scores, key=lambda k: bias_scores[k])
        dominant_score = bias_scores[dominant_bias]

        # Normalise score into 0-1 range (max possible ≈ 0.50)
        options_score = self._clamp(dominant_score / 0.50)

        self._log.info(
            "options_scored",
            bias=dominant_bias,
            options_score=round(options_score, 3),
            pcr=round(pcr, 3),
            max_pain=round(max_pain, 2),
            iv_rank=round(effective_iv_rank, 1) if effective_iv_rank >= 0 else "N/A",
        )

        result = {
            "options_score" : round(options_score, 4),
            "confidence"    : round(options_score, 4),
            "pcr"           : round(pcr, 4),
            "max_pain"      : round(max_pain, 2),
            "iv_rank"       : round(effective_iv_rank, 2) if effective_iv_rank >= 0 else -1,
            "bias"          : dominant_bias,
            "key_levels"    : {
                "call_wall"        : round(call_wall, 2),
                "put_wall"         : round(put_wall, 2),
                "unusual_activity" : unusual_strikes,
            },
            "strategy_hint" : strategy_hint,
            "reasons"       : reasons,
        }
        return self._timed_result(result, start)

    async def health_check(self) -> bool:
        """Pure computation — always healthy."""
        return True

    # ------------------------------------------------------------------ #
    # Component computations                                               #
    # ------------------------------------------------------------------ #

    def _compute_pcr(
        self, chain: list[dict]
    ) -> tuple[float, str, float, list[str]]:
        """
        Compute the Put-Call Ratio based on total OI across all strikes.

        PCR > 1.2 → excessive put OI → contrarian BULLISH (market may rebound)
        PCR < 0.8 → excessive call OI → contrarian BEARISH (market may fall)
        """
        total_call_oi = sum(int(s.get("call_oi", 0)) for s in chain)
        total_put_oi  = sum(int(s.get("put_oi",  0)) for s in chain)

        if total_call_oi == 0:
            return 1.0, "NEUTRAL", 0.0, ["PCR unavailable — no call OI data"]

        pcr = total_put_oi / total_call_oi
        reasons: list[str] = [
            f"PCR = {pcr:.3f} "
            f"(put OI={total_put_oi:,} | call OI={total_call_oi:,})"
        ]

        if pcr > PCR_BULLISH_THRESHOLD:
            score = self._clamp(0.50 + (pcr - PCR_BULLISH_THRESHOLD) * 0.20)
            reasons.append(
                f"PCR {pcr:.2f} > {PCR_BULLISH_THRESHOLD} → contrarian BULLISH "
                "(put writers are heavily positioned)"
            )
            return pcr, "BULLISH", score, reasons
        elif pcr < PCR_BEARISH_THRESHOLD:
            score = self._clamp(0.50 + (PCR_BEARISH_THRESHOLD - pcr) * 0.20)
            reasons.append(
                f"PCR {pcr:.2f} < {PCR_BEARISH_THRESHOLD} → contrarian BEARISH "
                "(call writers are heavily positioned)"
            )
            return pcr, "BEARISH", score, reasons
        else:
            reasons.append(
                f"PCR {pcr:.2f} in neutral range "
                f"[{PCR_BEARISH_THRESHOLD}, {PCR_BULLISH_THRESHOLD}]"
            )
            return pcr, "NEUTRAL", 0.25, reasons

    def _compute_oi_walls(
        self, chain: list[dict], spot: float
    ) -> tuple[float, float, list[str]]:
        """
        Identify the call wall (highest call OI above spot → resistance)
        and the put wall (highest put OI below spot → support).
        """
        reasons: list[str] = []

        above_spot = [s for s in chain if float(s.get("strike", 0)) > spot]
        below_spot = [s for s in chain if float(s.get("strike", 0)) <= spot]

        call_wall: float = 0.0
        put_wall: float  = 0.0

        if above_spot:
            call_wall_entry = max(above_spot, key=lambda s: int(s.get("call_oi", 0)))
            call_wall = float(call_wall_entry["strike"])
            reasons.append(
                f"Call wall (max call OI) at strike {call_wall:.2f} "
                f"— OI={call_wall_entry.get('call_oi', 0):,} "
                "(acts as resistance)"
            )

        if below_spot:
            put_wall_entry = max(below_spot, key=lambda s: int(s.get("put_oi", 0)))
            put_wall = float(put_wall_entry["strike"])
            reasons.append(
                f"Put wall (max put OI) at strike {put_wall:.2f} "
                f"— OI={put_wall_entry.get('put_oi', 0):,} "
                "(acts as support)"
            )

        return call_wall, put_wall, reasons

    def _compute_max_pain(
        self, chain: list[dict]
    ) -> tuple[float, list[str]]:
        """
        Max Pain = the strike at which the total dollar value of expiring
        options (both calls AND puts) is minimised for option BUYERS
        (maximised loss for buyers = maximum gain for sellers).

        Algorithm: for each possible expiry strike, sum up the intrinsic
        value of all calls and puts if they were to expire at that strike.
        The strike with the minimum total payout = max pain.
        """
        reasons: list[str] = []
        if not chain:
            return 0.0, ["Max pain not computable — empty chain"]

        strikes = [float(s.get("strike", 0)) for s in chain]
        pain_at_strike: dict[float, float] = {}

        for expiry_price in strikes:
            call_pain = sum(
                max(0.0, expiry_price - float(s.get("strike", 0)))
                * int(s.get("call_oi", 0))
                for s in chain
            )
            put_pain = sum(
                max(0.0, float(s.get("strike", 0)) - expiry_price)
                * int(s.get("put_oi", 0))
                for s in chain
            )
            pain_at_strike[expiry_price] = call_pain + put_pain

        max_pain_strike = min(pain_at_strike, key=lambda k: pain_at_strike[k])
        reasons.append(
            f"Max pain computed at {max_pain_strike:.2f} "
            f"(total option payout = {pain_at_strike[max_pain_strike]:,.0f})"
        )
        return max_pain_strike, reasons

    def _max_pain_bias(self, max_pain: float, spot: float) -> str:
        """
        If max pain is significantly above spot, there is a gravitational pull
        upward toward expiry (bullish). Below spot → bearish pull.
        Uses a ±0.5% threshold to declare neutral.
        """
        if spot <= 0:
            return "NEUTRAL"
        pct = (max_pain - spot) / spot * 100
        if pct > 0.5:
            return "BULLISH"
        elif pct < -0.5:
            return "BEARISH"
        return "NEUTRAL"

    def _iv_strategy(
        self, iv_rank: float
    ) -> tuple[str, list[str]]:
        """
        Maps IV rank to a preferred options strategy.

        High IV (>70): sell premium (straddles, iron condors, credit spreads)
        Low  IV (<30): buy options (debit spreads, directional plays)
        Mid  IV      : neutral — either strategy acceptable
        """
        reasons: list[str] = []
        if iv_rank < 0:
            reasons.append("IV rank not available — strategy preference undetermined")
            return "UNKNOWN", reasons

        if iv_rank >= IV_RANK_HIGH:
            reasons.append(
                f"IV rank {iv_rank:.1f} is HIGH (≥{IV_RANK_HIGH}) "
                "— options are richly priced, prefer SELLING premium"
            )
            return "SELL_OPTIONS", reasons
        elif iv_rank <= IV_RANK_LOW:
            reasons.append(
                f"IV rank {iv_rank:.1f} is LOW (≤{IV_RANK_LOW}) "
                "— options are cheaply priced, prefer BUYING options"
            )
            return "BUY_OPTIONS", reasons
        else:
            reasons.append(
                f"IV rank {iv_rank:.1f} is moderate "
                f"[{IV_RANK_LOW}–{IV_RANK_HIGH}] — no strong strategy preference"
            )
            return "NEUTRAL_STRATEGY", reasons

    def _detect_unusual_oi(
        self, chain: list[dict]
    ) -> tuple[list[dict], list[str]]:
        """
        Flags strikes where the OI change (vs previous session) is more than
        UNUSUAL_OI_MULTIPLIER × the median absolute OI change across all strikes.

        These can indicate smart money or institutional positioning.
        """
        reasons: list[str] = []
        unusual: list[dict] = []

        call_changes = [abs(int(s.get("call_oi_change", 0))) for s in chain]
        put_changes  = [abs(int(s.get("put_oi_change",  0))) for s in chain]
        all_changes  = call_changes + put_changes

        if not all_changes:
            reasons.append("OI change data unavailable — unusual activity check skipped")
            return unusual, reasons

        sorted_changes = sorted(all_changes)
        mid = len(sorted_changes) // 2
        median_change = (
            sorted_changes[mid]
            if len(sorted_changes) % 2 == 1
            else (sorted_changes[mid - 1] + sorted_changes[mid]) / 2.0
        )

        if median_change == 0:
            reasons.append("Median OI change is zero — skipping unusual activity detection")
            return unusual, reasons

        threshold = UNUSUAL_OI_MULTIPLIER * median_change

        for s in chain:
            strike = float(s.get("strike", 0))
            c_chg  = int(s.get("call_oi_change", 0))
            p_chg  = int(s.get("put_oi_change",  0))

            if abs(c_chg) > threshold:
                entry = {
                    "strike"    : strike,
                    "type"      : "CALL",
                    "oi_change" : c_chg,
                    "multiple"  : round(abs(c_chg) / median_change, 1),
                }
                unusual.append(entry)
                reasons.append(
                    f"Unusual CALL OI buildup at {strike:.2f}: "
                    f"change={c_chg:+,} ({entry['multiple']}× median)"
                )

            if abs(p_chg) > threshold:
                entry = {
                    "strike"    : strike,
                    "type"      : "PUT",
                    "oi_change" : p_chg,
                    "multiple"  : round(abs(p_chg) / median_change, 1),
                }
                unusual.append(entry)
                reasons.append(
                    f"Unusual PUT OI buildup at {strike:.2f}: "
                    f"change={p_chg:+,} ({entry['multiple']}× median)"
                )

        if not unusual:
            reasons.append("No unusual OI activity detected at any strike")

        return unusual, reasons
