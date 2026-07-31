"""
Indian market events calendar — RBI, Budget, earnings, expiry dates.
"""
from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, time, timedelta
from typing import Optional

import pytz

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# ── Known Major Recurring Events ────────────────────────────────────

# RBI MPC meeting months (approximate — exact dates announced yearly)
RBI_MPC_MONTHS = [2, 4, 6, 8, 10, 12]

# Earnings season windows (month ranges)
EARNINGS_SEASONS = [
    (4, 5),    # Q4 results: Apr–May
    (7, 8),    # Q1 results: Jul–Aug
    (10, 11),  # Q2 results: Oct–Nov
    (1, 2),    # Q3 results: Jan–Feb
]

# US Fed FOMC meeting months (8 per year approximately)
FED_FOMC_MONTHS = [1, 3, 5, 6, 7, 9, 11, 12]


def _last_thursday(year: int, month: int) -> date:
    """Get the last Thursday of a given month (monthly F&O expiry)."""
    # Find the last day of the month
    last_day = calendar.monthrange(year, month)[1]
    d = date(year, month, last_day)
    # Walk backward to find Thursday (weekday 3)
    while d.weekday() != 3:  # 3 = Thursday
        d -= timedelta(days=1)
    return d


def _all_thursdays(year: int, month: int) -> list[date]:
    """Get all Thursdays in a month (weekly expiry dates for NIFTY)."""
    d = date(year, month, 1)
    # Advance to first Thursday
    while d.weekday() != 3:
        d += timedelta(days=1)
    thursdays = []
    while d.month == month:
        thursdays.append(d)
        d += timedelta(days=7)
    return thursdays


def _all_weekday(year: int, month: int, weekday: int) -> list[date]:
    """Get all occurrences of a specific weekday in a month."""
    d = date(year, month, 1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    days = []
    while d.month == month:
        days.append(d)
        d += timedelta(days=7)
    return days


class EventsCalendar:
    """
    Manages the Indian market events calendar for blocking
    dangerous trading windows around major events.
    """

    def __init__(
        self,
        block_before_hours: int = 2,
        block_after_hours: int = 1,
    ):
        self.block_before_hours = block_before_hours
        self.block_after_hours = block_after_hours
        # Custom event overrides loaded from config
        self._custom_events: list[dict] = []

    def add_custom_event(
        self, dt: datetime, name: str, severity: str = "HIGH"
    ) -> None:
        """Add a custom event (e.g. specific RBI date, company earnings)."""
        self._custom_events.append({
            "datetime": dt,
            "name": name,
            "severity": severity,
        })

    # ── Blocked Window Check ─────────────────────────────────────────

    def is_blocked_window(
        self,
        dt: Optional[datetime] = None,
        hours_before: Optional[int] = None,
        hours_after: Optional[int] = None,
    ) -> tuple[bool, str]:
        """
        Check if the given time falls within a blocked trading window.

        Returns:
            (is_blocked, reason_string)
        """
        if dt is None:
            dt = datetime.now(IST)
        elif dt.tzinfo is None:
            dt = IST.localize(dt)

        hb = hours_before or self.block_before_hours
        ha = hours_after or self.block_after_hours

        # Check all upcoming events
        events = self.get_upcoming_events(days=3)
        for evt in events:
            evt_dt = evt["datetime"]
            if evt_dt.tzinfo is None:
                evt_dt = IST.localize(evt_dt)

            window_start = evt_dt - timedelta(hours=hb)
            window_end = evt_dt + timedelta(hours=ha)

            if window_start <= dt <= window_end:
                return True, f"Blocked: {evt['name']} at {evt_dt.strftime('%Y-%m-%d %H:%M IST')}"

        # Check if today is monthly expiry (high volatility)
        today = dt.date()
        monthly_expiry = self.get_monthly_expiry(today.year, today.month)
        if today == monthly_expiry and dt.time() >= time(14, 0):
            return True, "Monthly F&O expiry — high volatility after 2:00 PM"

        return False, ""

    # ── Upcoming Events ──────────────────────────────────────────────

    def get_upcoming_events(self, days: int = 7) -> list[dict]:
        """Get all major events in the next N days."""
        now = datetime.now(IST)
        cutoff = now + timedelta(days=days)
        events: list[dict] = []

        for delta in range(days + 1):
            d = (now + timedelta(days=delta)).date()

            # Budget day (Feb 1)
            if d.month == 2 and d.day == 1:
                events.append({
                    "date": d.isoformat(),
                    "datetime": IST.localize(datetime.combine(d, time(11, 0))),
                    "name": "Union Budget Presentation",
                    "type": "BUDGET",
                    "severity": "CRITICAL",
                })

            # RBI MPC meetings
            if d.month in RBI_MPC_MONTHS:
                # RBI typically announces on the first Friday of the month
                first_friday = d.replace(day=1)
                while first_friday.weekday() != 4:
                    first_friday += timedelta(days=1)
                if d == first_friday:
                    events.append({
                        "date": d.isoformat(),
                        "datetime": IST.localize(datetime.combine(d, time(10, 0))),
                        "name": "RBI MPC Policy Decision",
                        "type": "RBI_MPC",
                        "severity": "CRITICAL",
                    })

            # Monthly F&O expiry
            monthly_exp = self.get_monthly_expiry(d.year, d.month)
            if d == monthly_exp:
                events.append({
                    "date": d.isoformat(),
                    "datetime": IST.localize(datetime.combine(d, time(15, 30))),
                    "name": "Monthly F&O Expiry",
                    "type": "EXPIRY",
                    "severity": "HIGH",
                })

            # Weekly Nifty expiry (Thursdays)
            if d.weekday() == 3 and d != monthly_exp:
                events.append({
                    "date": d.isoformat(),
                    "datetime": IST.localize(datetime.combine(d, time(15, 30))),
                    "name": "Weekly NIFTY Expiry",
                    "type": "EXPIRY",
                    "severity": "MEDIUM",
                })

            # Earnings season
            for start_m, end_m in EARNINGS_SEASONS:
                if d.month == start_m and d.day == 1:
                    events.append({
                        "date": d.isoformat(),
                        "datetime": IST.localize(datetime.combine(d, time(9, 15))),
                        "name": f"Earnings Season Window ({calendar.month_abbr[start_m]}-{calendar.month_abbr[end_m]})",
                        "type": "EARNINGS_SEASON",
                        "severity": "MEDIUM",
                    })

        # Add custom events within window
        for evt in self._custom_events:
            evt_dt = evt["datetime"]
            if evt_dt.tzinfo is None:
                evt_dt = IST.localize(evt_dt)
            if now <= evt_dt <= cutoff:
                events.append({
                    "date": evt_dt.date().isoformat(),
                    "datetime": evt_dt,
                    "name": evt["name"],
                    "type": "CUSTOM",
                    "severity": evt["severity"],
                })

        events.sort(key=lambda e: e["datetime"])
        return events

    # ── Expiry Date Helpers ──────────────────────────────────────────

    @staticmethod
    def get_monthly_expiry(year: int, month: int) -> date:
        """Get the monthly F&O expiry date (last Thursday of month)."""
        return _last_thursday(year, month)

    @staticmethod
    def get_weekly_expiry_dates(year: int, month: int) -> list[date]:
        """Get all weekly expiry dates in a month (Thursdays for NIFTY)."""
        return _all_thursdays(year, month)

    @staticmethod
    def get_days_to_expiry(expiry_date: date, from_date: Optional[date] = None) -> int:
        """Calculate days to expiry from a given date."""
        if from_date is None:
            from_date = datetime.now(IST).date()
        return (expiry_date - from_date).days

    @staticmethod
    def get_next_monthly_expiry(from_date: Optional[date] = None) -> date:
        """Get the next monthly expiry from today."""
        if from_date is None:
            from_date = datetime.now(IST).date()
        exp = _last_thursday(from_date.year, from_date.month)
        if exp <= from_date:
            # Move to next month
            if from_date.month == 12:
                exp = _last_thursday(from_date.year + 1, 1)
            else:
                exp = _last_thursday(from_date.year, from_date.month + 1)
        return exp

    @staticmethod
    def get_next_weekly_expiry(from_date: Optional[date] = None) -> date:
        """Get the next weekly NIFTY expiry (Thursday)."""
        if from_date is None:
            from_date = datetime.now(IST).date()
        d = from_date
        while d.weekday() != 3:  # Thursday
            d += timedelta(days=1)
        if d == from_date:
            d += timedelta(days=7)  # Skip today if it's already Thursday
        return d
