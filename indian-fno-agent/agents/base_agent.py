"""
agents/base_agent.py
--------------------
Abstract base class for all trading agents in the Indian F&O system.

Every concrete agent must implement:
  - `analyze(context)` — the core logic that returns a structured result dict
  - `health_check()`    — lightweight liveness probe used by the orchestrator

The `weight` attribute controls how much this agent's score contributes
to the RiskArbiter's ensemble vote (sum of all weights should ideally = 1.0
excluding RiskArbiter itself which has weight 1.0 as the final arbiter).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class BaseAgent(ABC):
    """
    Abstract base for all trading agents.

    Subclasses MUST set:
        agent_name : str   — unique identifier used in logs and ensemble maps
        weight     : float — contribution weight for ensemble scoring (0.0–1.0)

    and MUST implement:
        analyze(context)   — async, returns a structured result dict
        health_check()     — async, returns True if the agent is operational
    """

    # ------------------------------------------------------------------ #
    # Class-level attributes — override in subclasses                     #
    # ------------------------------------------------------------------ #
    agent_name: str = "base_agent"
    weight: float = 0.0  # contribution weight in ensemble vote (0–1)

    def __init__(self) -> None:
        # Bind a logger enriched with the agent name so every log line
        # automatically carries the agent identity.
        self._log = logger.bind(agent=self.agent_name)
        self._log.info("agent_initialised", weight=self.weight)

    # ------------------------------------------------------------------ #
    # Abstract interface                                                   #
    # ------------------------------------------------------------------ #

    @abstractmethod
    async def analyze(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        Run the agent's core analysis against the supplied context.

        Parameters
        ----------
        context : dict
            Arbitrary payload assembled by the orchestrator. Each agent
            documents the keys it expects in its own docstring.

        Returns
        -------
        dict
            Agent-specific result dictionary. Every concrete implementation
            MUST include at minimum:
                - "agent"      : str   — self.agent_name
                - "confidence" : float — primary confidence score (0–1)
                - "reasons"    : list[str] — human-readable rationale
        """

    @abstractmethod
    async def health_check(self) -> bool:
        """
        Lightweight liveness probe.

        Returns
        -------
        bool
            True  — agent is healthy and ready to analyze
            False — agent has a dependency issue (e.g. data feed down)
        """

    # ------------------------------------------------------------------ #
    # Shared helpers available to all subclasses                          #
    # ------------------------------------------------------------------ #

    def _clamp(self, value: float, lo: float = 0.0, hi: float = 1.0) -> float:
        """Clamp *value* into [lo, hi]."""
        return max(lo, min(hi, value))

    def _timed_result(
        self, result: dict[str, Any], start_ts: float
    ) -> dict[str, Any]:
        """
        Enrich *result* with latency metadata before returning from analyze().

        Parameters
        ----------
        result    : the dict to enrich
        start_ts  : ``time.monotonic()`` captured at the start of analyze()

        Returns
        -------
        dict — the same result dict, mutated in-place and returned
        """
        elapsed_ms = round((time.monotonic() - start_ts) * 1_000, 2)
        result["agent"] = self.agent_name
        result["latency_ms"] = elapsed_ms
        self._log.debug(
            "analyze_complete",
            confidence=result.get("confidence"),
            latency_ms=elapsed_ms,
        )
        return result

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} "
            f"name={self.agent_name!r} weight={self.weight}>"
        )
