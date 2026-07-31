"""
agents/risk_arbiter.py
-----------------------
The final decision gate in the agent pipeline.

The RiskArbiter receives the outputs of all other agents, computes a
weighted ensemble score, applies hard-block rules, and returns a
clear `should_propose` verdict that the Orchestrator uses to decide
whether to run strategy generation.

Expected context keys
---------------------
agent_results : dict[str, dict]
    Keyed by agent_name; each value is the dict returned by that agent's
    analyze() call.  The arbiter expects at minimum:
        "regime_agent"    → {confidence, regime}
        "technical_agent" → {technical_score, signal_direction, confidence}
        "options_agent"   → {options_score, bias, confidence}
        "news_agent"      → {sentiment_score, is_blocked_window,
                              block_reason, confidence}

risk_state : dict (optional)
    Current portfolio risk state — reserved for future max-drawdown checks.
    Typical keys:
        daily_pnl_pct  : float  — today's realised PnL as % of capital
        open_positions : int    — number of currently open F&O positions
        max_drawdown_hit: bool  — True if daily loss limit has been breached

symbol     : str   — instrument being evaluated (for logging)
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from agents.base_agent import BaseAgent

logger = structlog.get_logger(__name__)

# ------------------------------------------------------------------ #
# Configuration constants                                             #
# ------------------------------------------------------------------ #

# Minimum ensemble confidence for the arbiter to say "should_propose = True"
DEFAULT_CONFIDENCE_THRESHOLD: float = 0.65

# Agent name → weight mapping used in the ensemble calculation.
# These MUST sum to 1.0 and must match the weight attributes defined on
# each concrete agent class.  Keeping them here centralises override logic.
ENSEMBLE_AGENT_WEIGHTS: dict[str, float] = {
    "regime_agent"    : 0.20,
    "technical_agent" : 0.35,
    "options_agent"   : 0.25,
    "news_agent"      : 0.15,
    # risk_arbiter itself (weight=1.0) is the consumer, not a contributor
}


class RiskArbiter(BaseAgent):
    """
    Weighted-ensemble gatekeeper for trade proposals.

    Responsibilities:
        1. Compute a weighted average of all contributing agent scores.
        2. Apply hard blocks (news blackout, daily-loss limit).
        3. Enforce the confidence threshold before proposing a trade.
        4. Return a structured verdict consumed by the Orchestrator.

    The arbiter's own ``weight = 1.0`` marks it as the final authority —
    it does not contribute to the ensemble; it consumes it.
    """

    agent_name: str = "risk_arbiter"
    weight: float = 1.0  # final arbiter, not an ensemble contributor

    def __init__(
        self,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    ) -> None:
        super().__init__()
        self.confidence_threshold = confidence_threshold
        self._log.info(
            "arbiter_configured",
            confidence_threshold=confidence_threshold,
        )

    # ------------------------------------------------------------------ #
    # Public interface                                                     #
    # ------------------------------------------------------------------ #

    async def analyze(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        Evaluate all agent outputs and return a trade-proposal verdict.

        Returns
        -------
        dict
            {
              "agent"            : "risk_arbiter",
              "should_propose"   : bool,
              "final_confidence" : float  (0–1),
              "ensemble_scores"  : dict[str, float]  — per-agent contributions,
              "block_reason"     : str | None,
              "summary"          : str   — one-line human-readable verdict,
              "confidence"       : float (alias for final_confidence),
              "reasons"          : list[str],
              "latency_ms"       : float,
            }
        """
        start = time.monotonic()
        symbol: str = context.get("symbol", "UNKNOWN")
        self._log.info("risk_arbiter_start", symbol=symbol)

        agent_results: dict[str, dict] = context.get("agent_results", {})
        risk_state: dict[str, Any]     = context.get("risk_state", {})

        reasons: list[str] = []
        block_reason: str | None = None
        should_propose: bool = False

        # ----------------------------------------------------------------
        # Step 1: Hard blocks — checked before computing ensemble score
        # ----------------------------------------------------------------

        # 1a. News blackout window
        news_result = agent_results.get("news_agent", {})
        if news_result.get("is_blocked_window", False):
            block_reason = (
                f"News block active: "
                f"{news_result.get('block_reason', 'CRITICAL event')}"
            )
            reasons.append(f"⛔ HARD BLOCK — {block_reason}")
            self._log.warning("arbiter_news_block", reason=block_reason)
            return self._build_result(
                should_propose=False,
                final_confidence=0.0,
                ensemble_scores={},
                block_reason=block_reason,
                summary=f"Trade blocked: {block_reason}",
                reasons=reasons,
                start=start,
            )

        # 1b. Portfolio daily-loss limit breached
        if risk_state.get("max_drawdown_hit", False):
            block_reason = "Daily max drawdown limit has been reached — no new positions"
            reasons.append(f"⛔ HARD BLOCK — {block_reason}")
            self._log.warning("arbiter_drawdown_block")
            return self._build_result(
                should_propose=False,
                final_confidence=0.0,
                ensemble_scores={},
                block_reason=block_reason,
                summary=block_reason,
                reasons=reasons,
                start=start,
            )

        # ----------------------------------------------------------------
        # Step 2: Compute weighted ensemble score
        # ----------------------------------------------------------------
        ensemble_scores, final_confidence, score_reasons = (
            self._compute_ensemble(agent_results)
        )
        reasons.extend(score_reasons)

        # ----------------------------------------------------------------
        # Step 3: Regime check — VOLATILE_BREAKOUT / NEWS_DRIVEN are risky
        # ----------------------------------------------------------------
        regime_result = agent_results.get("regime_agent", {})
        regime = regime_result.get("regime", "UNDEFINED")
        if regime in ("VOLATILE_BREAKOUT", "NEWS_DRIVEN", "UNDEFINED"):
            reasons.append(
                f"⚠️  Caution: regime is '{regime}' — "
                "confidence penalised by 10%"
            )
            final_confidence = self._clamp(final_confidence - 0.10)

        # ----------------------------------------------------------------
        # Step 4: Confidence threshold gate
        # ----------------------------------------------------------------
        if final_confidence >= self.confidence_threshold:
            should_propose = True
            reasons.append(
                f"✅ Confidence {final_confidence:.1%} ≥ threshold "
                f"{self.confidence_threshold:.1%} → signal approved"
            )
            summary = (
                f"TRADE PROPOSED for {symbol}: "
                f"ensemble confidence {final_confidence:.1%} "
                f"(threshold {self.confidence_threshold:.1%})"
            )
        else:
            should_propose = False
            gap = self.confidence_threshold - final_confidence
            reasons.append(
                f"❌ Confidence {final_confidence:.1%} < threshold "
                f"{self.confidence_threshold:.1%} "
                f"(gap {gap:.1%}) → no signal"
            )
            summary = (
                f"NO TRADE for {symbol}: "
                f"insufficient ensemble confidence {final_confidence:.1%} "
                f"(required {self.confidence_threshold:.1%})"
            )

        self._log.info(
            "arbiter_verdict",
            symbol=symbol,
            should_propose=should_propose,
            final_confidence=round(final_confidence, 3),
            regime=regime,
        )

        return self._build_result(
            should_propose=should_propose,
            final_confidence=final_confidence,
            ensemble_scores=ensemble_scores,
            block_reason=block_reason,
            summary=summary,
            reasons=reasons,
            start=start,
        )

    async def health_check(self) -> bool:
        """Pure computation — always healthy."""
        return True

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _compute_ensemble(
        self, agent_results: dict[str, dict]
    ) -> tuple[dict[str, float], float, list[str]]:
        """
        Compute the weighted average confidence score from contributing agents.

        For the technical agent, the score is extracted from `technical_score`.
        For the options agent, from `options_score`.
        For news agent, the score is derived from the absolute sentiment score
        mapped to [0,1]: abs(sentiment_score) × confidence.
        For regime agent, we use `confidence` directly.

        Returns
        -------
        (ensemble_scores, final_confidence, reasons)
        """
        reasons: list[str] = []
        ensemble_scores: dict[str, float] = {}

        total_weight:  float = 0.0
        weighted_sum:  float = 0.0

        # ---- regime agent -------------------------------------------
        regime_conf = self._extract_score(agent_results, "regime_agent", "confidence")
        w_regime = ENSEMBLE_AGENT_WEIGHTS.get("regime_agent", 0.0)
        ensemble_scores["regime_agent"] = regime_conf
        weighted_sum += regime_conf * w_regime
        total_weight += w_regime
        reasons.append(
            f"RegimeAgent: confidence={regime_conf:.3f} "
            f"× weight={w_regime} → {regime_conf * w_regime:.4f}"
        )

        # ---- technical agent ----------------------------------------
        tech_score = self._extract_score(
            agent_results, "technical_agent", "technical_score"
        )
        w_tech = ENSEMBLE_AGENT_WEIGHTS.get("technical_agent", 0.0)
        ensemble_scores["technical_agent"] = tech_score
        weighted_sum += tech_score * w_tech
        total_weight += w_tech
        reasons.append(
            f"TechnicalAgent: technical_score={tech_score:.3f} "
            f"× weight={w_tech} → {tech_score * w_tech:.4f}"
        )

        # ---- options agent ------------------------------------------
        opt_score = self._extract_score(
            agent_results, "options_agent", "options_score"
        )
        w_opt = ENSEMBLE_AGENT_WEIGHTS.get("options_agent", 0.0)
        ensemble_scores["options_agent"] = opt_score
        weighted_sum += opt_score * w_opt
        total_weight += w_opt
        reasons.append(
            f"OptionsAgent: options_score={opt_score:.3f} "
            f"× weight={w_opt} → {opt_score * w_opt:.4f}"
        )

        # ---- news agent ---------------------------------------------
        # Map sentiment_score in [-1, 1] to confidence in [0, 1]:
        # use |sentiment_score| × agent_confidence to get an unsigned
        # measure of "how strongly does news support a directional view".
        news_res = agent_results.get("news_agent", {})
        raw_sentiment = abs(float(news_res.get("sentiment_score", 0.0)))
        news_conf = float(news_res.get("confidence", 0.0))
        news_score = self._clamp(raw_sentiment * news_conf)
        w_news = ENSEMBLE_AGENT_WEIGHTS.get("news_agent", 0.0)
        ensemble_scores["news_agent"] = news_score
        weighted_sum += news_score * w_news
        total_weight += w_news
        reasons.append(
            f"NewsAgent: |sentiment|={raw_sentiment:.3f} × conf={news_conf:.3f} "
            f"→ score={news_score:.3f} × weight={w_news} → {news_score * w_news:.4f}"
        )

        # ---- final weighted average ---------------------------------
        final = self._clamp(weighted_sum / total_weight) if total_weight > 0 else 0.0
        reasons.append(
            f"Ensemble weighted average: "
            f"{weighted_sum:.4f} / {total_weight:.2f} = {final:.4f}"
        )

        return ensemble_scores, final, reasons

    @staticmethod
    def _extract_score(
        agent_results: dict[str, dict],
        agent_name: str,
        key: str,
        default: float = 0.0,
    ) -> float:
        """Safely extract a float score from an agent result dict."""
        result = agent_results.get(agent_name, {})
        try:
            return float(result.get(key, default))
        except (TypeError, ValueError):
            return default

    def _build_result(
        self,
        should_propose: bool,
        final_confidence: float,
        ensemble_scores: dict[str, float],
        block_reason: str | None,
        summary: str,
        reasons: list[str],
        start: float,
    ) -> dict[str, Any]:
        """Assemble the final result dict."""
        result: dict[str, Any] = {
            "should_propose"   : should_propose,
            "final_confidence" : round(final_confidence, 4),
            "ensemble_scores"  : {
                k: round(v, 4) for k, v in ensemble_scores.items()
            },
            "block_reason"     : block_reason,
            "summary"          : summary,
            "confidence"       : round(final_confidence, 4),
            "reasons"          : reasons,
        }
        return self._timed_result(result, start)
