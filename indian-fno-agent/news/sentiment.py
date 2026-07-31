"""
Gemini-powered news sentiment analysis with keyword fallback.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from core.enums import EventSeverity, NewsSentiment
from core.models import NewsEvent
from config.settings import get_settings

logger = logging.getLogger(__name__)

# ── Keyword-based fallback lists ────────────────────────────────────

NEGATIVE_KEYWORDS = [
    "crash", "fall", "decline", "loss", "cut", "slump", "weak", "drop", "bear",
    "recession", "downgrade", "scam", "fraud", "default", "ban", "penalty",
    "war", "crisis", "shutdown", "collapse", "sell-off", "selloff", "fear",
    "inflation", "rate hike", "hawkish", "outflow", "fii selling",
]

POSITIVE_KEYWORDS = [
    "rally", "surge", "gain", "rise", "bull", "upgrade", "record high",
    "growth", "recovery", "profit", "earnings beat", "dovish", "rate cut",
    "inflow", "fii buying", "breakout", "expansion", "boom", "strong",
    "robust", "optimism", "upbeat", "green",
]

CRITICAL_KEYWORDS = [
    "rbi policy", "rbi mpc", "monetary policy", "budget", "election result",
    "war", "nuclear", "crash", "circuit breaker", "trading halt", "sebi ban",
]


class SentimentAnalyzer:
    """
    Analyse Indian financial news for sentiment using Gemini or keyword fallback.
    """

    def __init__(self):
        self.settings = get_settings()
        self._gemini_model = None
        self._rate_limiter_tokens = 10
        self._rate_limiter_last_refill = time.monotonic()
        self._rate_limit_per_minute = 10

    async def _get_gemini_model(self):
        """Lazy-init the Gemini generative model."""
        if self._gemini_model is None and self.settings.GEMINI_API_KEY:
            try:
                import google.generativeai as genai

                genai.configure(api_key=self.settings.GEMINI_API_KEY)
                self._gemini_model = genai.GenerativeModel("gemini-1.5-flash")
                logger.info("Gemini model initialised for sentiment analysis.")
            except Exception as e:
                logger.warning(f"Failed to init Gemini model: {e}. Using keyword fallback.")
                self._gemini_model = None
        return self._gemini_model

    def _check_rate_limit(self) -> bool:
        """Simple token-bucket rate limiter: 10 requests/minute."""
        now = time.monotonic()
        elapsed = now - self._rate_limiter_last_refill
        self._rate_limiter_tokens = min(
            self._rate_limit_per_minute,
            self._rate_limiter_tokens + elapsed * (self._rate_limit_per_minute / 60.0),
        )
        self._rate_limiter_last_refill = now
        if self._rate_limiter_tokens >= 1:
            self._rate_limiter_tokens -= 1
            return True
        return False

    # ── Core Analysis ────────────────────────────────────────────────

    async def analyze(
        self, headline: str, content: str = ""
    ) -> tuple[NewsSentiment, float, str, EventSeverity]:
        """
        Analyse a single headline for sentiment.

        Returns:
            (sentiment, score -1.0 to 1.0, one_line_summary, severity)
        """
        model = await self._get_gemini_model()
        if model is not None and self._check_rate_limit():
            return await self._analyze_with_gemini(model, headline, content)
        return self._analyze_with_keywords(headline, content)

    async def _analyze_with_gemini(
        self, model, headline: str, content: str
    ) -> tuple[NewsSentiment, float, str, EventSeverity]:
        """Use Gemini to analyze sentiment."""
        prompt = f"""Analyze this Indian financial news headline for market sentiment.

Headline: {headline}
{f'Content snippet: {content[:500]}' if content else ''}

Return ONLY valid JSON (no markdown):
{{
  "sentiment": "POSITIVE" | "NEGATIVE" | "NEUTRAL" | "VERY_POSITIVE" | "VERY_NEGATIVE",
  "score": <float from -1.0 to 1.0>,
  "summary": "<one-line summary of market impact>",
  "severity": "LOW" | "MEDIUM" | "HIGH" | "CRITICAL"
}}"""
        try:
            response = await asyncio.to_thread(
                model.generate_content, prompt
            )
            text = response.text.strip()
            # Strip markdown code fences if present
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

            data = json.loads(text)
            return (
                NewsSentiment(data.get("sentiment", "NEUTRAL")),
                float(data.get("score", 0.0)),
                str(data.get("summary", headline)),
                EventSeverity(data.get("severity", "LOW")),
            )
        except Exception as e:
            logger.warning(f"Gemini sentiment error: {e}. Falling back to keywords.")
            return self._analyze_with_keywords(headline, content)

    def _analyze_with_keywords(
        self, headline: str, content: str = ""
    ) -> tuple[NewsSentiment, float, str, EventSeverity]:
        """Keyword-based sentiment fallback."""
        text = (headline + " " + content).lower()
        pos_count = sum(1 for kw in POSITIVE_KEYWORDS if kw in text)
        neg_count = sum(1 for kw in NEGATIVE_KEYWORDS if kw in text)
        crit_count = sum(1 for kw in CRITICAL_KEYWORDS if kw in text)

        # Score
        total = pos_count + neg_count
        if total == 0:
            score = 0.0
            sentiment = NewsSentiment.NEUTRAL
        else:
            score = (pos_count - neg_count) / total
            if score > 0.5:
                sentiment = NewsSentiment.VERY_POSITIVE
            elif score > 0.1:
                sentiment = NewsSentiment.POSITIVE
            elif score < -0.5:
                sentiment = NewsSentiment.VERY_NEGATIVE
            elif score < -0.1:
                sentiment = NewsSentiment.NEGATIVE
            else:
                sentiment = NewsSentiment.NEUTRAL

        # Severity
        if crit_count > 0:
            severity = EventSeverity.CRITICAL
        elif neg_count >= 3:
            severity = EventSeverity.HIGH
        elif neg_count >= 1 or pos_count >= 2:
            severity = EventSeverity.MEDIUM
        else:
            severity = EventSeverity.LOW

        return sentiment, round(score, 3), headline[:120], severity

    # ── Batch Processing ─────────────────────────────────────────────

    async def analyze_batch(self, events: list[NewsEvent]) -> list[NewsEvent]:
        """
        Analyse sentiment for a batch of news events.
        Enriches each event in-place and returns the list.
        """
        for event in events:
            if event.sentiment is not None:
                continue  # Already analysed

            sentiment, score, summary, severity = await self.analyze(
                event.headline, event.summary or ""
            )
            event.sentiment = sentiment.value
            event.sentiment_score = score
            event.summary = summary
            event.severity = severity.value

            # Mark blocked window for critical events
            if severity in (EventSeverity.CRITICAL, EventSeverity.HIGH.value, "CRITICAL", "HIGH"):
                event.is_blocked_window = True

            # Small delay between API calls
            await asyncio.sleep(0.5)

        return events
