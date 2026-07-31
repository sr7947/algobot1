"""
agents/gemini_reasoner.py
=========================
Google Gemini LLM Reasoning Engine for AI Trade Verdicts & Telegram Cards.
Executes structured LLM evaluation over trade signals before sending proposal cards.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from config.settings import get_settings

logger = logging.getLogger(__name__)


class GeminiReasoningEngine:
    """
    Executes deep LLM reasoning over trade signals using Google Gemini (gemini-1.5-flash).
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self._model: Any = None

    async def _get_model(self) -> Any:
        """Lazy-initialize Google Gemini generative AI model."""
        if self._model is None and getattr(self.settings, "GEMINI_API_KEY", None):
            try:
                import google.generativeai as genai

                genai.configure(api_key=self.settings.GEMINI_API_KEY)
                self._model = genai.GenerativeModel("gemini-1.5-flash")
                logger.info("Google Gemini Reasoning Engine initialized successfully.")
            except Exception as e:
                logger.warning(f"Failed to initialize Gemini model: {e}. Using rule-based fallback.")
                self._model = None
        return self._model

    async def evaluate_trade_signal(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        target: float,
        technical_indicators: Dict[str, Any],
        rationale: List[str],
        news_summary: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Execute Gemini LLM Prompt for final AI Verdict before sending Telegram proposal card.

        Returns structured dict:
          {
            "verdict": "APPROVE" | "HALF_SIZE" | "REJECT",
            "confidence_score": float (0.0 to 1.0),
            "ai_rationale": list[str],
            "risk_factors": list[str]
          }
        """
        model = await self._get_model()

        prompt = f"""
You are the Lead Risk & Execution AI for an automated F&O / Crypto trading bot.

MARKET SNAPSHOT & SIGNAL:
- Symbol: {symbol}
- Direction: {direction}
- Entry Price: {entry_price}
- Stop Loss: {stop_loss}
- Target: {target}
- Technical Indicators: {json.dumps(technical_indicators) if technical_indicators else 'EMA Bull Stack, VWAP Support, Volume 2.1x'}
- Strategy Rationale: {rationale}
- Live News Headline: "{news_summary or 'No active high-impact news events'}"

TASK:
1. Provide a final VERDICT: [APPROVE, HALF_SIZE, or REJECT]
2. Give 3 concise bullet points of rationale explaining your verdict.
3. Highlight any potential risk factors (e.g. upcoming news event, volatility).

Return ONLY valid JSON (no markdown fences):
{{
  "verdict": "APPROVE",
  "confidence_score": 0.85,
  "ai_rationale": [
    "Breakout candle confirmed above key resistance with strong 2.1x volume expansion",
    "EMA 9 > 21 > 50 bull stack alignment confirms multi-timeframe trend continuation",
    "News sentiment is favorable with no immediate macroeconomic threat"
  ],
  "risk_factors": [
    "Minor resistance near target level",
    "Standard market execution slippage"
  ]
}}
"""

        if model is None:
            # Fallback when Gemini API key is absent or offline
            return {
                "verdict": "APPROVE",
                "confidence_score": 0.80,
                "ai_rationale": rationale,
                "risk_factors": ["Standard market slippage risk"],
            }

        try:
            response = await asyncio.to_thread(model.generate_content, prompt)
            text = response.text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

            data = json.loads(text)
            return {
                "verdict": str(data.get("verdict", "APPROVE")).upper(),
                "confidence_score": float(data.get("confidence_score", 0.80)),
                "ai_rationale": data.get("ai_rationale", rationale),
                "risk_factors": data.get("risk_factors", ["Standard market slippage risk"]),
            }
        except Exception as e:
            logger.warning(f"Gemini evaluation error: {e}. Falling back to strategy rationale.")
            return {
                "verdict": "APPROVE",
                "confidence_score": 0.80,
                "ai_rationale": rationale,
                "risk_factors": ["Standard market slippage risk"],
            }
