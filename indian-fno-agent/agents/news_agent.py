"""
agents/news_agent.py
--------------------
Evaluates recent news events for a given symbol and returns a sentiment
assessment, a blocked-window flag, and risk factors.

Expected context keys
---------------------
news_events : list[dict]
    Each event dict must contain at minimum:
        headline        : str
        summary         : str  (may be empty)
        symbol_tags     : list[str]  — symbols/tickers this news relates to
        sentiment       : str  — "POSITIVE" | "NEGATIVE" | "NEUTRAL"
                                 (pre-scored by the data pipeline or NLP model)
        sentiment_score : float  — raw score in [-1.0, 1.0]
        severity        : str  — "LOW" | "MEDIUM" | "HIGH" | "CRITICAL"
        published_at    : str  — ISO-8601 timestamp

symbol : str
    The F&O instrument being analyzed (e.g., "NIFTY", "BANKNIFTY", "RELIANCE").

Notes
-----
The news_agent does NOT run any NLP internally — it relies on pre-computed
sentiment fields produced by the data pipeline (which may use an LLM or
lexicon-based model). This keeps latency low and the agent stateless.

If the data pipeline does not pre-score sentiment, you can inject a simple
lexical scorer in the `_score_event()` method below.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import structlog

from agents.base_agent import BaseAgent

logger = structlog.get_logger(__name__)

# ------------------------------------------------------------------ #
# Constants                                                           #
# ------------------------------------------------------------------ #

# Severities that trigger a hard trading block
BLOCKING_SEVERITIES: frozenset[str] = frozenset({"CRITICAL"})

# Severity → weight used when aggregating the composite sentiment score
SEVERITY_WEIGHTS: dict[str, float] = {
    "LOW"     : 0.5,
    "MEDIUM"  : 1.0,
    "HIGH"    : 2.0,
    "CRITICAL": 3.0,
}

# Maximum number of top headlines to include in result
TOP_HEADLINES_COUNT: int = 3

# Generic market-index tags — news containing these tags is always relevant
MARKET_INDEX_TAGS: frozenset[str] = frozenset(
    {"NIFTY", "BANKNIFTY", "SENSEX", "NIFTY50", "INDIA", "RBI", "SEBI", "FII", "FED"}
)

# How many minutes old a CRITICAL event must be before it stops blocking
CRITICAL_EVENT_TTL_MINUTES: int = 60


class NewsAgent(BaseAgent):
    """
    Filters news relevant to the requested symbol and returns:

    - overall_sentiment   : "POSITIVE" | "NEGATIVE" | "NEUTRAL"
    - sentiment_score     : float in [-1.0, 1.0]
    - is_blocked_window   : True if any CRITICAL event is recent and active
    - top_headlines       : list[str] — top 3 most relevant headlines
    - risk_factors        : list[str] — human-readable risk descriptions
    - reasons             : list[str] — explanation for the assessment
    """

    agent_name: str = "news_agent"
    weight: float = 0.15

    # ------------------------------------------------------------------ #
    # Public interface                                                     #
    # ------------------------------------------------------------------ #

    async def analyze(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        Evaluate news sentiment for the given symbol.

        Returns
        -------
        dict
            {
              "agent"              : "news_agent",
              "overall_sentiment"  : "POSITIVE" | "NEGATIVE" | "NEUTRAL",
              "sentiment_score"    : float  (-1.0 to 1.0),
              "confidence"         : float  (0–1, derived from event count & severity),
              "is_blocked_window"  : bool,
              "block_reason"       : str | None,
              "top_headlines"      : list[str],
              "risk_factors"       : list[str],
              "reasons"            : list[str],
              "latency_ms"         : float,
            }
        """
        start = time.monotonic()
        self._log.info("news_analysis_start")

        all_events: list[dict] = context.get("news_events", [])
        symbol: str = context.get("symbol", "").upper().strip()

        # ---- Filter relevant events ----------------------------------
        relevant = self._filter_relevant(all_events, symbol)
        self._log.debug(
            "news_filtered",
            total=len(all_events),
            relevant=len(relevant),
            symbol=symbol,
        )

        reasons: list[str] = [
            f"Analyzed {len(all_events)} news event(s); "
            f"{len(relevant)} relevant to '{symbol}'"
        ]

        # ---- Blocked window check (CRITICAL severity) ----------------
        is_blocked, block_reason, block_events = self._check_blocked_window(relevant)
        if is_blocked:
            reasons.append(block_reason or "Blocked window active — CRITICAL event")

        # ---- Sentiment aggregation ----------------------------------
        sentiment_score, sentiment_label, sentiment_reasons = (
            self._aggregate_sentiment(relevant)
        )
        reasons.extend(sentiment_reasons)

        # ---- Top headlines ------------------------------------------
        top_headlines = self._pick_top_headlines(relevant)

        # ---- Risk factor extraction ---------------------------------
        risk_factors = self._extract_risk_factors(relevant, is_blocked)

        # ---- Confidence (based on event count and severity) ----------
        confidence = self._compute_confidence(relevant)

        self._log.info(
            "news_scored",
            sentiment=sentiment_label,
            score=round(sentiment_score, 3),
            is_blocked=is_blocked,
            confidence=round(confidence, 3),
        )

        result: dict[str, Any] = {
            "overall_sentiment" : sentiment_label,
            "sentiment_score"   : round(sentiment_score, 4),
            "confidence"        : round(confidence, 4),
            "is_blocked_window" : is_blocked,
            "block_reason"      : block_reason,
            "top_headlines"     : top_headlines,
            "risk_factors"      : risk_factors,
            "reasons"           : reasons,
        }
        return self._timed_result(result, start)

    async def health_check(self) -> bool:
        """Pure logic — always healthy."""
        return True

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _filter_relevant(
        self, events: list[dict], symbol: str
    ) -> list[dict]:
        """
        Retain events that mention the target symbol or a market index tag.
        If symbol is empty, all events are returned as-is.
        """
        if not symbol:
            return list(events)

        relevant: list[dict] = []
        symbol_upper = symbol.upper()

        for event in events:
            tags: list[str] = [
                t.upper() for t in event.get("symbol_tags", [])
            ]
            headline: str = event.get("headline", "").upper()
            summary:  str = event.get("summary",  "").upper()

            tag_match    = symbol_upper in tags
            index_match  = bool(MARKET_INDEX_TAGS.intersection(tags))
            text_match   = symbol_upper in headline or symbol_upper in summary

            if tag_match or index_match or text_match:
                relevant.append(event)

        return relevant

    def _check_blocked_window(
        self, events: list[dict]
    ) -> tuple[bool, str | None, list[dict]]:
        """
        Returns (is_blocked, reason_str, blocking_events).

        A trading block is triggered when a CRITICAL-severity event was
        published within the last CRITICAL_EVENT_TTL_MINUTES minutes.
        """
        now_utc = datetime.now(tz=timezone.utc)
        blocking: list[dict] = []

        for event in events:
            severity = event.get("severity", "LOW").upper()
            if severity not in BLOCKING_SEVERITIES:
                continue

            published_at_str = event.get("published_at", "")
            try:
                published_dt = datetime.fromisoformat(
                    published_at_str.replace("Z", "+00:00")
                )
                age_minutes = (now_utc - published_dt).total_seconds() / 60.0
                if age_minutes <= CRITICAL_EVENT_TTL_MINUTES:
                    blocking.append(event)
            except (ValueError, TypeError):
                # If we can't parse the timestamp, treat it as recent
                blocking.append(event)

        if blocking:
            headlines = [e.get("headline", "N/A")[:80] for e in blocking[:2]]
            reason = (
                f"CRITICAL news event within last {CRITICAL_EVENT_TTL_MINUTES} min: "
                + " | ".join(headlines)
            )
            return True, reason, blocking

        return False, None, []

    def _aggregate_sentiment(
        self, events: list[dict]
    ) -> tuple[float, str, list[str]]:
        """
        Compute a weighted average sentiment score.

        Each event's sentiment_score is weighted by its severity.
        Returns (score, label, reasons).
        """
        reasons: list[str] = []

        if not events:
            reasons.append("No relevant news — defaulting to NEUTRAL sentiment")
            return 0.0, "NEUTRAL", reasons

        total_weight: float = 0.0
        weighted_sum: float = 0.0

        for event in events:
            raw_score: float = float(event.get("sentiment_score", 0.0))
            severity: str    = event.get("severity", "LOW").upper()
            w = SEVERITY_WEIGHTS.get(severity, 1.0)
            weighted_sum += raw_score * w
            total_weight += w

        if total_weight == 0:
            return 0.0, "NEUTRAL", reasons

        composite: float = self._clamp(weighted_sum / total_weight, -1.0, 1.0)

        if composite > 0.15:
            label = "POSITIVE"
            reasons.append(
                f"Weighted sentiment score = {composite:.3f} → POSITIVE "
                f"(across {len(events)} event(s))"
            )
        elif composite < -0.15:
            label = "NEGATIVE"
            reasons.append(
                f"Weighted sentiment score = {composite:.3f} → NEGATIVE "
                f"(across {len(events)} event(s))"
            )
        else:
            label = "NEUTRAL"
            reasons.append(
                f"Weighted sentiment score = {composite:.3f} → NEUTRAL "
                f"(across {len(events)} event(s))"
            )

        # Add detail about high-severity events
        high_sev = [
            e for e in events
            if e.get("severity", "").upper() in ("HIGH", "CRITICAL")
        ]
        if high_sev:
            for e in high_sev[:2]:
                reasons.append(
                    f"[{e.get('severity')}] {e.get('headline', 'N/A')[:80]}"
                )

        return composite, label, reasons

    def _pick_top_headlines(self, events: list[dict]) -> list[str]:
        """
        Return the top N headlines sorted by severity (CRITICAL first) then
        by recency (newest first). Truncates each to 120 characters.
        """
        severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        sorted_events = sorted(
            events,
            key=lambda e: (
                severity_order.get(e.get("severity", "LOW").upper(), 3),
                # Negate to sort newest first (string comparison on ISO date)
                -(hash(e.get("published_at", ""))),
            ),
        )
        headlines: list[str] = []
        for e in sorted_events[:TOP_HEADLINES_COUNT]:
            headline = e.get("headline", "")[:120]
            sev = e.get("severity", "LOW")
            headlines.append(f"[{sev}] {headline}")
        return headlines

    def _extract_risk_factors(
        self, events: list[dict], is_blocked: bool
    ) -> list[str]:
        """
        Build a list of human-readable risk factors from HIGH/CRITICAL events.
        Also adds a blanket risk if the window is blocked.
        """
        risk_factors: list[str] = []

        if is_blocked:
            risk_factors.append(
                "⛔ TRADING BLOCKED: Critical news event active — "
                "await resolution before initiating new positions"
            )

        # Collect distinct risk descriptions from high-severity events
        seen: set[str] = set()
        for event in events:
            severity = event.get("severity", "LOW").upper()
            if severity in ("HIGH", "CRITICAL"):
                summary = event.get("summary", event.get("headline", ""))[:160]
                if summary and summary not in seen:
                    risk_factors.append(f"[{severity}] {summary}")
                    seen.add(summary)

        # Add generic systemic risk mentions from medium events
        medium_events = [
            e for e in events if e.get("severity", "").upper() == "MEDIUM"
        ]
        if medium_events:
            risk_factors.append(
                f"{len(medium_events)} MEDIUM-severity event(s) detected — "
                "monitor for escalation"
            )

        return risk_factors

    def _compute_confidence(self, events: list[dict]) -> float:
        """
        Confidence in the news assessment scales with:
        - Number of relevant events (more data = more confidence)
        - Presence of HIGH/CRITICAL events (clear signal = more confident)

        Max confidence = 0.90 (news alone is never 100% reliable).
        """
        if not events:
            return 0.20  # low confidence when no data

        base = 0.40
        count_boost = min(len(events) * 0.05, 0.25)  # up to +0.25 for 5+ events

        high_count = sum(
            1 for e in events
            if e.get("severity", "").upper() in ("HIGH", "CRITICAL")
        )
        severity_boost = min(high_count * 0.10, 0.25)  # up to +0.25

        return self._clamp(base + count_boost + severity_boost, 0.0, 0.90)
