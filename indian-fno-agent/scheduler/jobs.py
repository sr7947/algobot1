"""
Indian F&O Trading Agent — Main Scheduler
APScheduler-based job scheduler for market hours tasks.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import get_settings

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

settings = get_settings()


def _is_market_hours() -> bool:
    """Check if current IST time is within NSE market hours (9:15 AM – 3:30 PM)."""
    now_ist = datetime.now(IST).time()
    market_open = time(9, 15)
    market_close = time(15, 30)
    return market_open <= now_ist <= market_close


def _is_trading_day() -> bool:
    """Check if today is a weekday (Monday–Friday)."""
    return datetime.now(IST).weekday() < 5  # 0=Mon, 4=Fri


class TradingScheduler:
    """
    Central scheduler for all market-hours and maintenance jobs.

    Jobs:
    - signal_scan: Runs every 60s during market hours
    - position_monitor: Runs every 5s during market hours (SL/Target check)
    - news_fetch: Runs every 15 minutes all day
    - end_of_day_summary: Runs at 3:35 PM IST on trading days
    - broker_reconnect: Runs every 30 minutes to keep session alive
    - pre_market_scan: Runs at 9:00 AM to warm up data and check events
    - risk_state_reset: Runs at 9:00 AM to reset daily P&L counters
    """

    def __init__(
        self,
        orchestrator=None,
        news_service=None,
        telegram_notifier=None,
        kill_switch=None,
    ):
        self.orchestrator = orchestrator
        self.news_service = news_service
        self.telegram_notifier = telegram_notifier
        self.kill_switch = kill_switch
        self._scheduler = AsyncIOScheduler(timezone=IST)
        self._running = False

    def setup_jobs(self) -> None:
        """Register all scheduled jobs."""

        # ── Signal Scanner ──────────────────────────────────────────────
        self._scheduler.add_job(
            self._run_signal_scan,
            trigger=IntervalTrigger(seconds=settings.SIGNAL_SCAN_INTERVAL_SECONDS),
            id="signal_scan",
            name="Signal Scanner",
            replace_existing=True,
            misfire_grace_time=30,
        )

        # ── Position Monitor (SL/Target check every 5s) ─────────────────
        self._scheduler.add_job(
            self._run_position_monitor,
            trigger=IntervalTrigger(seconds=5),
            id="position_monitor",
            name="Position Monitor",
            replace_existing=True,
            misfire_grace_time=5,
        )

        # ── News Fetcher (every 15 minutes) ─────────────────────────────
        self._scheduler.add_job(
            self._run_news_fetch,
            trigger=IntervalTrigger(minutes=settings.NEWS_FETCH_INTERVAL_MINUTES),
            id="news_fetch",
            name="News Fetcher",
            replace_existing=True,
        )

        # ── Pre-market Warm-up (9:00 AM IST) ────────────────────────────
        self._scheduler.add_job(
            self._pre_market_setup,
            trigger=CronTrigger(hour=9, minute=0, day_of_week="mon-fri"),
            id="pre_market",
            name="Pre-Market Setup",
            replace_existing=True,
        )

        # ── Daily Risk State Reset (9:00 AM IST) ────────────────────────
        self._scheduler.add_job(
            self._reset_risk_state,
            trigger=CronTrigger(hour=9, minute=0, day_of_week="mon-fri"),
            id="risk_reset",
            name="Daily Risk Reset",
            replace_existing=True,
        )

        # ── End-of-Day Summary (3:35 PM IST) ────────────────────────────
        self._scheduler.add_job(
            self._end_of_day_summary,
            trigger=CronTrigger(hour=15, minute=35, day_of_week="mon-fri"),
            id="eod_summary",
            name="End-of-Day Summary",
            replace_existing=True,
        )

        # ── Broker Session Keep-Alive (every 30 min) ─────────────────────
        self._scheduler.add_job(
            self._broker_keepalive,
            trigger=IntervalTrigger(minutes=30),
            id="broker_keepalive",
            name="Broker Session Keep-Alive",
            replace_existing=True,
        )

        # ── Expired Signal Cleanup (every 5 min) ────────────────────────
        self._scheduler.add_job(
            self._cleanup_expired_signals,
            trigger=IntervalTrigger(minutes=5),
            id="signal_cleanup",
            name="Expired Signal Cleanup",
            replace_existing=True,
        )

        logger.info("All scheduler jobs registered.")

    async def start(self) -> None:
        """Start the scheduler."""
        self.setup_jobs()
        self._scheduler.start()
        self._running = True
        logger.info("TradingScheduler started.")

    async def stop(self) -> None:
        """Gracefully stop the scheduler."""
        if self._running:
            self._scheduler.shutdown(wait=False)
            self._running = False
            logger.info("TradingScheduler stopped.")

    # ── Job Implementations ───────────────────────────────────────────────

    async def _run_signal_scan(self) -> None:
        """Run the main signal scanning cycle during market hours."""
        if not _is_market_hours() or not _is_trading_day():
            return
        if self.orchestrator is None:
            return
        try:
            await self.orchestrator.run_cycle()
        except Exception as e:
            logger.error(f"Signal scan error: {e}", exc_info=True)

    async def _run_position_monitor(self) -> None:
        """Monitor open positions for SL/Target hits."""
        if not _is_market_hours():
            return
        if self.orchestrator is None:
            return
        try:
            await self.orchestrator.monitor_positions()
        except Exception as e:
            logger.error(f"Position monitor error: {e}", exc_info=True)

    async def _run_news_fetch(self) -> None:
        """Fetch latest news and update blocked windows."""
        if self.news_service is None:
            return
        try:
            await self.news_service.fetch_latest()
            logger.debug("News fetch completed.")
        except Exception as e:
            logger.error(f"News fetch error: {e}", exc_info=True)

    async def _pre_market_setup(self) -> None:
        """Pre-market warmup: load instruments, check events, prime data cache."""
        logger.info("Starting pre-market setup...")
        try:
            if self.orchestrator:
                await self.orchestrator.pre_market_setup()
            if self.telegram_notifier:
                await self.telegram_notifier.send_system_alert(
                    "🌅 Pre-market setup complete. Signal scanning will begin at 9:15 AM IST.",
                    severity="INFO",
                )
        except Exception as e:
            logger.error(f"Pre-market setup error: {e}", exc_info=True)

    async def _reset_risk_state(self) -> None:
        """Reset daily P&L counters and kill switch state at market open."""
        logger.info("Resetting daily risk state.")
        try:
            if self.orchestrator:
                await self.orchestrator.reset_daily_state()
        except Exception as e:
            logger.error(f"Risk state reset error: {e}", exc_info=True)

    async def _end_of_day_summary(self) -> None:
        """Send end-of-day P&L summary via Telegram."""
        logger.info("Generating end-of-day summary.")
        try:
            if self.orchestrator:
                await self.orchestrator.end_of_day()
        except Exception as e:
            logger.error(f"End-of-day summary error: {e}", exc_info=True)

    async def _broker_keepalive(self) -> None:
        """Refresh broker session token to prevent expiry."""
        try:
            if self.orchestrator and hasattr(self.orchestrator, "broker_adapter"):
                adapter = self.orchestrator.broker_adapter
                if not adapter.is_connected():
                    logger.warning("Broker disconnected, attempting reconnect...")
                    await adapter.login()
        except Exception as e:
            logger.error(f"Broker keepalive error: {e}", exc_info=True)

    async def _cleanup_expired_signals(self) -> None:
        """Mark expired pending signals as EXPIRED in DB."""
        try:
            if self.orchestrator:
                await self.orchestrator.cleanup_expired_signals()
        except Exception as e:
            logger.error(f"Signal cleanup error: {e}", exc_info=True)
