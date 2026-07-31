"""
agents/orchestrator.py
-----------------------
Central coordinator that runs the full trading cycle for all watchlist
symbols.  The orchestrator wires together:

  - Data fetching (multi-timeframe OHLCV + option chain + news)
  - All analysis agents (regime, technical, options, news → risk arbiter)
  - Strategy selection and signal generation
  - Risk engine hard checks
  - Telegram bot approval workflow
  - Position / approval lifecycle management

Design principles
-----------------
- All I/O and strategy execution is async (asyncio.gather for parallelism).
- Each component is injected via constructor — no global singletons.
- The orchestrator never blocks the event loop; heavy work is dispatched
  with asyncio.create_task.
- Pending approvals time out automatically (default 5 minutes).
- Graceful shutdown via asyncio.Event.

Dependency interfaces (expected duck-typed protocols)
-----------------------------------------------------
DataFetcher:
    async def fetch_multiframe(symbol, timeframes) -> dict[str, list[dict]]
    async def fetch_option_chain(symbol) -> list[dict]
    async def fetch_news(symbol) -> list[dict]
    async def fetch_indicators(symbol, candles) -> dict

BaseStrategy:
    strategy_name: str
    async def generate_signal(context) -> dict | None

RiskEngine:
    async def hard_check(signal) -> tuple[bool, str | None]
        Returns (passed: bool, rejection_reason: Optional[str])

TelegramBot:
    async def send_signal_for_approval(signal) -> str   (returns signal_id)
    async def send_message(text)

Each of these is injected; this module does NOT import concrete
implementations to avoid circular dependencies.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Optional

import structlog

from agents.news_agent import NewsAgent
from agents.options_agent import OptionsAgent
from agents.regime_agent import RegimeAgent
from agents.risk_arbiter import RiskArbiter
from agents.technical_agent import TechnicalAgent

logger = structlog.get_logger(__name__)

# ------------------------------------------------------------------ #
# Configuration constants                                             #
# ------------------------------------------------------------------ #
DEFAULT_TIMEFRAMES: list[str] = ["5m", "15m", "1h", "1d"]
APPROVAL_TIMEOUT_SECONDS: int = 300   # 5 minutes before auto-rejection
CYCLE_INTERVAL_SECONDS:   int = 60    # run one cycle per minute


# ------------------------------------------------------------------ #
# Data structures                                                     #
# ------------------------------------------------------------------ #

@dataclass
class PendingApproval:
    """Tracks a signal waiting for Telegram user approval."""
    signal_id:    str
    symbol:       str
    signal:       dict[str, Any]
    created_at:   datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    timeout_task: Optional[asyncio.Task] = field(default=None, repr=False)


# ------------------------------------------------------------------ #
# Orchestrator                                                        #
# ------------------------------------------------------------------ #

class TradingOrchestrator:
    """
    Coordinates the full trading cycle for an Indian F&O agent.

    Constructor parameters
    ----------------------
    data_fetcher    : duck-typed DataFetcher
    strategies      : list of duck-typed BaseStrategy objects
    risk_engine     : duck-typed RiskEngine
    telegram_bot    : duck-typed TelegramBot
    watchlist       : list[str]  — symbols to process each cycle
    timeframes      : list[str]  — timeframes to fetch (default 5m/15m/1h/1d)
    confidence_threshold : float — passed to RiskArbiter (default 0.65)
    approval_timeout     : int   — seconds before an unapproved signal expires
    cycle_interval       : int   — seconds between full cycle runs
    """

    def __init__(
        self,
        data_fetcher: Any,
        strategies: list[Any],
        risk_engine: Any,
        telegram_bot: Any,
        watchlist: list[str],
        timeframes: list[str] = DEFAULT_TIMEFRAMES,
        confidence_threshold: float = 0.65,
        approval_timeout: int = APPROVAL_TIMEOUT_SECONDS,
        cycle_interval: int = CYCLE_INTERVAL_SECONDS,
    ) -> None:
        # ---- injected dependencies ----------------------------------
        self._data_fetcher   = data_fetcher
        self._strategies     = strategies
        self._risk_engine    = risk_engine
        self._telegram_bot   = telegram_bot

        # ---- configuration -----------------------------------------
        self._watchlist      = watchlist
        self._timeframes     = timeframes
        self._approval_timeout = approval_timeout
        self._cycle_interval   = cycle_interval

        # ---- agent instances (stateless, re-used across cycles) -----
        self._regime_agent    = RegimeAgent()
        self._technical_agent = TechnicalAgent()
        self._options_agent   = OptionsAgent()
        self._news_agent      = NewsAgent()
        self._risk_arbiter    = RiskArbiter(
            confidence_threshold=confidence_threshold
        )

        # ---- state -------------------------------------------------
        self._pending_approvals: dict[str, PendingApproval] = {}
        self._stop_event: asyncio.Event = asyncio.Event()
        self._cycle_count: int = 0

        self._log = logger.bind(component="orchestrator")
        self._log.info(
            "orchestrator_initialised",
            watchlist=watchlist,
            strategy_count=len(strategies),
            timeframes=timeframes,
        )

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """
        Start the orchestrator's main loop.

        This coroutine blocks until ``stop()`` is called.
        Run it with ``asyncio.run(orchestrator.start())`` or as a task.
        """
        self._stop_event.clear()
        self._log.info("orchestrator_started")

        # Health check all agents before entering the loop
        await self._health_check_all_agents()

        while not self._stop_event.is_set():
            cycle_start = time.monotonic()
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                self._log.info("orchestrator_cancelled")
                break
            except Exception as exc:  # pylint: disable=broad-except
                self._log.exception("cycle_unhandled_exception", error=str(exc))
                await self._notify_error(str(exc))

            elapsed = time.monotonic() - cycle_start
            sleep_seconds = max(0.0, self._cycle_interval - elapsed)
            self._log.info(
                "cycle_sleep",
                elapsed_s=round(elapsed, 2),
                sleep_s=round(sleep_seconds, 2),
            )

            try:
                await asyncio.wait_for(
                    asyncio.shield(self._stop_event.wait()),
                    timeout=sleep_seconds,
                )
            except asyncio.TimeoutError:
                pass  # normal — sleep expired, run next cycle

        self._log.info("orchestrator_stopped")

    async def stop(self) -> None:
        """Signal the main loop to stop after the current cycle completes."""
        self._log.info("orchestrator_stop_requested")
        self._stop_event.set()

        # Cancel all pending approval timeout tasks
        for pending in list(self._pending_approvals.values()):
            if pending.timeout_task and not pending.timeout_task.done():
                pending.timeout_task.cancel()

    # ------------------------------------------------------------------ #
    # Main cycle                                                           #
    # ------------------------------------------------------------------ #

    async def run_cycle(self) -> None:
        """
        Execute one full analysis-and-signal cycle across all watchlist symbols.

        Sequence per symbol:
          1. Fetch multi-timeframe data, option chain, news, indicators
          2. Run all agents in parallel (asyncio.gather)
          3. Pass results to RiskArbiter
          4. If approved, run each strategy (parallel tasks)
          5. Pass any generated signals through Risk Engine hard checks
          6. Send approved signals to Telegram for human approval
        """
        self._cycle_count += 1
        self._log.info(
            "cycle_start",
            cycle=self._cycle_count,
            symbols=self._watchlist,
        )

        # Process each symbol concurrently
        symbol_tasks = [
            asyncio.create_task(
                self._process_symbol(symbol),
                name=f"symbol_{symbol}_{self._cycle_count}",
            )
            for symbol in self._watchlist
        ]

        results = await asyncio.gather(*symbol_tasks, return_exceptions=True)

        for symbol, result in zip(self._watchlist, results):
            if isinstance(result, Exception):
                self._log.error(
                    "symbol_processing_failed",
                    symbol=symbol,
                    error=str(result),
                )

        self._log.info("cycle_complete", cycle=self._cycle_count)

    # ------------------------------------------------------------------ #
    # Per-symbol processing                                                #
    # ------------------------------------------------------------------ #

    async def _process_symbol(self, symbol: str) -> None:
        """Full pipeline for a single symbol."""
        sym_log = self._log.bind(symbol=symbol)
        sym_log.info("symbol_processing_start")

        # ---- Step 1: Fetch data ------------------------------------
        fetch_start = time.monotonic()
        candles, option_chain, news_events, indicators = await asyncio.gather(
            self._data_fetcher.fetch_multiframe(symbol, self._timeframes),
            self._data_fetcher.fetch_option_chain(symbol),
            self._data_fetcher.fetch_news(symbol),
            self._data_fetcher.fetch_indicators(symbol, {}),  # indicators derived server-side
        )
        sym_log.debug(
            "data_fetched",
            timeframes=len(candles),
            chain_strikes=len(option_chain),
            news_count=len(news_events),
            fetch_ms=round((time.monotonic() - fetch_start) * 1_000, 1),
        )

        # Enrich indicators with spot price (last close)
        spot_price: float = self._extract_spot(candles)
        indicators["price"] = spot_price

        # ---- Step 2: Build shared context ---------------------------
        base_context: dict[str, Any] = {
            "symbol"       : symbol,
            "candles"      : candles,
            "indicators"   : indicators,
            "option_chain" : option_chain,
            "spot_price"   : spot_price,
            "news_events"  : news_events,
        }

        # ---- Step 3: Run all agents in parallel ---------------------
        agent_results = await self._run_all_agents(base_context)

        # ---- Step 4: Risk Arbiter verdict ---------------------------
        arbiter_context = {
            **base_context,
            "agent_results": agent_results,
            "risk_state"   : await self._get_risk_state(),
        }
        arbiter_result = await self._risk_arbiter.analyze(arbiter_context)

        sym_log.info(
            "arbiter_verdict",
            should_propose=arbiter_result.get("should_propose"),
            confidence=arbiter_result.get("final_confidence"),
        )

        if not arbiter_result.get("should_propose", False):
            sym_log.info(
                "no_signal",
                reason=arbiter_result.get("summary", ""),
            )
            return

        # ---- Step 5: Run strategies to generate signals -------------
        strategy_context = {
            **base_context,
            "agent_results" : agent_results,
            "arbiter_result": arbiter_result,
        }
        signals = await self._run_strategies(strategy_context, symbol)

        # ---- Step 6: Risk engine checks + Telegram approval ---------
        for signal in signals:
            await asyncio.create_task(
                self._validate_and_dispatch(signal, symbol),
                name=f"dispatch_{signal.get('signal_id', 'unknown')}",
            )

    # ------------------------------------------------------------------ #
    # Agent orchestration                                                  #
    # ------------------------------------------------------------------ #

    async def _run_all_agents(
        self, context: dict[str, Any]
    ) -> dict[str, dict]:
        """
        Run regime, technical, options, and news agents concurrently.
        Returns a dict keyed by agent_name.
        """
        self._log.debug("running_all_agents", symbol=context.get("symbol"))

        (
            regime_result,
            technical_result,
            options_result,
            news_result,
        ) = await asyncio.gather(
            self._regime_agent.analyze(context),
            self._technical_agent.analyze(context),
            self._options_agent.analyze(context),
            self._news_agent.analyze(context),
            return_exceptions=False,
        )

        return {
            "regime_agent"    : regime_result,
            "technical_agent" : technical_result,
            "options_agent"   : options_result,
            "news_agent"      : news_result,
        }

    # ------------------------------------------------------------------ #
    # Strategy execution                                                   #
    # ------------------------------------------------------------------ #

    async def _run_strategies(
        self, context: dict[str, Any], symbol: str
    ) -> list[dict[str, Any]]:
        """
        Run all registered strategies concurrently.
        Collect non-None signals and stamp each with a unique signal_id.
        """
        strategy_tasks: list[Coroutine] = [
            s.generate_signal(context) for s in self._strategies
        ]
        raw_results = await asyncio.gather(*strategy_tasks, return_exceptions=True)

        signals: list[dict[str, Any]] = []
        for strategy, raw in zip(self._strategies, raw_results):
            if isinstance(raw, Exception):
                self._log.error(
                    "strategy_exception",
                    strategy=strategy.strategy_name,
                    symbol=symbol,
                    error=str(raw),
                )
                continue
            if raw is None:
                self._log.debug(
                    "strategy_no_signal",
                    strategy=strategy.strategy_name,
                    symbol=symbol,
                )
                continue

            # Stamp with a unique ID and metadata
            raw["signal_id"]    = str(uuid.uuid4())
            raw["symbol"]       = symbol
            raw["strategy"]     = strategy.strategy_name
            raw["generated_at"] = datetime.now(tz=timezone.utc).isoformat()
            signals.append(raw)

            self._log.info(
                "strategy_signal_generated",
                strategy=strategy.strategy_name,
                symbol=symbol,
                signal_id=raw["signal_id"],
            )

        return signals

    # ------------------------------------------------------------------ #
    # Risk check and Telegram dispatch                                      #
    # ------------------------------------------------------------------ #

    async def _validate_and_dispatch(
        self, signal: dict[str, Any], symbol: str
    ) -> None:
        """
        1. Run the risk engine hard check.
        2. If passed, send to Telegram and register a pending approval.
        """
        signal_id = signal.get("signal_id", "unknown")
        passed, rejection_reason = await self._risk_engine.hard_check(signal)

        if not passed:
            self._log.info(
                "signal_risk_rejected",
                signal_id=signal_id,
                symbol=symbol,
                reason=rejection_reason,
            )
            return

        # Send to Telegram bot for human approval
        try:
            await self._telegram_bot.send_signal_for_approval(signal)
        except Exception as exc:  # pylint: disable=broad-except
            self._log.error(
                "telegram_dispatch_failed",
                signal_id=signal_id,
                error=str(exc),
            )
            return

        # Register pending approval with a timeout
        timeout_task = asyncio.create_task(
            self._approval_timeout_handler(signal_id),
            name=f"timeout_{signal_id}",
        )
        pending = PendingApproval(
            signal_id=signal_id,
            symbol=symbol,
            signal=signal,
            timeout_task=timeout_task,
        )
        self._pending_approvals[signal_id] = pending

        self._log.info(
            "signal_pending_approval",
            signal_id=signal_id,
            symbol=symbol,
            timeout_s=self._approval_timeout,
        )

    async def _approval_timeout_handler(self, signal_id: str) -> None:
        """
        Auto-reject a pending signal if it is not actioned within the timeout.
        """
        await asyncio.sleep(self._approval_timeout)
        if signal_id in self._pending_approvals:
            pending = self._pending_approvals.pop(signal_id)
            self._log.warning(
                "signal_approval_timeout",
                signal_id=signal_id,
                symbol=pending.symbol,
                age_s=self._approval_timeout,
            )
            try:
                await self._telegram_bot.send_message(
                    f"⏰ Signal {signal_id[:8]}… for {pending.symbol} "
                    "expired without approval and has been cancelled."
                )
            except Exception:  # pylint: disable=broad-except
                pass  # best-effort notification

    # ------------------------------------------------------------------ #
    # Approval handler (called by Telegram callback route)                 #
    # ------------------------------------------------------------------ #

    async def handle_approval(
        self,
        signal_id: str,
        action: str,
        modified_qty: Optional[int] = None,
    ) -> None:
        """
        Route an approval/rejection decision arriving from a Telegram callback.

        Parameters
        ----------
        signal_id    : str  — the UUID of the signal being actioned
        action       : str  — "APPROVE" | "REJECT" | "MODIFY"
        modified_qty : int  — new quantity if action == "MODIFY" (optional)
        """
        self._log.info(
            "approval_received",
            signal_id=signal_id,
            action=action,
            modified_qty=modified_qty,
        )

        pending = self._pending_approvals.pop(signal_id, None)
        if pending is None:
            self._log.warning(
                "approval_unknown_signal_id",
                signal_id=signal_id,
                action=action,
            )
            await self._telegram_bot.send_message(
                f"⚠️ Signal {signal_id[:8]}… not found — it may have expired."
            )
            return

        # Cancel the timeout task
        if pending.timeout_task and not pending.timeout_task.done():
            pending.timeout_task.cancel()

        action_upper = action.upper()

        if action_upper == "APPROVE":
            await self._execute_approved_signal(pending.signal)

        elif action_upper == "MODIFY" and modified_qty is not None:
            modified_signal = {**pending.signal, "quantity": modified_qty}
            self._log.info(
                "signal_modified",
                signal_id=signal_id,
                new_qty=modified_qty,
            )
            await self._execute_approved_signal(modified_signal)

        elif action_upper == "REJECT":
            self._log.info(
                "signal_rejected_by_user",
                signal_id=signal_id,
                symbol=pending.symbol,
            )
            await self._telegram_bot.send_message(
                f"❌ Signal {signal_id[:8]}… for {pending.symbol} rejected."
            )

        else:
            self._log.error(
                "approval_unknown_action",
                action=action,
                signal_id=signal_id,
            )

    async def _execute_approved_signal(self, signal: dict[str, Any]) -> None:
        """
        Forward an approved signal to the execution engine.
        Errors are caught so they do not crash the orchestrator loop.
        """
        signal_id = signal.get("signal_id", "unknown")
        symbol    = signal.get("symbol", "UNKNOWN")
        self._log.info(
            "signal_executing",
            signal_id=signal_id,
            symbol=symbol,
        )
        try:
            # Execution engine is expected to have an async `execute(signal)` method
            await self._risk_engine.execute(signal)
            self._log.info(
                "signal_executed",
                signal_id=signal_id,
                symbol=symbol,
            )
            await self._telegram_bot.send_message(
                f"✅ Order placed for {symbol} — signal {signal_id[:8]}…"
            )
        except Exception as exc:  # pylint: disable=broad-except
            self._log.exception(
                "signal_execution_failed",
                signal_id=signal_id,
                error=str(exc),
            )
            await self._telegram_bot.send_message(
                f"🚨 Order execution FAILED for {symbol}: {exc}"
            )

    # ------------------------------------------------------------------ #
    # Utility helpers                                                       #
    # ------------------------------------------------------------------ #

    async def _health_check_all_agents(self) -> None:
        """Run health checks on all agents; log any failures."""
        agents = [
            self._regime_agent,
            self._technical_agent,
            self._options_agent,
            self._news_agent,
            self._risk_arbiter,
        ]
        checks = await asyncio.gather(
            *[a.health_check() for a in agents],
            return_exceptions=True,
        )
        for agent, status in zip(agents, checks):
            if isinstance(status, Exception) or not status:
                self._log.error(
                    "agent_health_check_failed",
                    agent=agent.agent_name,
                    error=str(status) if isinstance(status, Exception) else "returned False",
                )
            else:
                self._log.info(
                    "agent_health_ok",
                    agent=agent.agent_name,
                )

    async def _get_risk_state(self) -> dict[str, Any]:
        """
        Fetch the current portfolio risk state from the risk engine.
        Falls back to a safe default if the engine call fails.
        """
        try:
            if hasattr(self._risk_engine, "get_risk_state"):
                return await self._risk_engine.get_risk_state()
        except Exception as exc:  # pylint: disable=broad-except
            self._log.warning(
                "risk_state_fetch_failed",
                error=str(exc),
            )
        # Safe default: no blocks, no open positions
        return {
            "daily_pnl_pct"    : 0.0,
            "open_positions"   : 0,
            "max_drawdown_hit" : False,
        }

    @staticmethod
    def _extract_spot(candles: dict[str, list[dict]]) -> float:
        """
        Extract the latest closing price from the finest-grained timeframe
        available.  Returns 0.0 if candles are missing.
        """
        # Prefer 5-minute data; fall back to any timeframe
        for tf in ("5m", "15m", "1h", "1d"):
            bars = candles.get(tf, [])
            if bars:
                last_bar = bars[-1]
                return float(last_bar.get("close", last_bar.get("ltp", 0.0)))
        # Last resort: take any non-empty timeframe
        for bars in candles.values():
            if bars:
                last_bar = bars[-1]
                return float(last_bar.get("close", last_bar.get("ltp", 0.0)))
        return 0.0

    async def _notify_error(self, message: str) -> None:
        """Best-effort Telegram notification for unhandled cycle errors."""
        try:
            await self._telegram_bot.send_message(
                f"🚨 Orchestrator unhandled error:\n```\n{message[:300]}\n```"
            )
        except Exception:  # pylint: disable=broad-except
            pass

    # ------------------------------------------------------------------ #
    # Introspection / monitoring                                           #
    # ------------------------------------------------------------------ #

    @property
    def pending_approval_count(self) -> int:
        """Number of signals currently awaiting human approval."""
        return len(self._pending_approvals)

    @property
    def cycle_count(self) -> int:
        """Total number of completed cycles since start."""
        return self._cycle_count

    def pending_approval_summary(self) -> list[dict[str, Any]]:
        """Return a lightweight summary of all pending approvals."""
        now = datetime.now(tz=timezone.utc)
        return [
            {
                "signal_id" : p.signal_id,
                "symbol"    : p.symbol,
                "strategy"  : p.signal.get("strategy"),
                "age_s"     : round((now - p.created_at).total_seconds(), 1),
            }
            for p in self._pending_approvals.values()
        ]

    def __repr__(self) -> str:
        return (
            f"<TradingOrchestrator "
            f"symbols={self._watchlist} "
            f"strategies={len(self._strategies)} "
            f"cycle={self._cycle_count} "
            f"pending={self.pending_approval_count}>"
        )
