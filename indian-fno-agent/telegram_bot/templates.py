"""
telegram_bot/templates.py
=========================
Formatting helper functions for constructing Telegram MarkdownV2 messages.

All functions return plain Python strings.  None of them make network calls.
Import freely from notifier.py, handlers.py, or any other module.

Design notes
------------
- ``escape_md`` is the most critical helper: every user-supplied or dynamic
  value MUST be passed through it before inclusion in a MarkdownV2 message,
  or Telegram will return a 400 Bad Request.
- Indian number formatting (e.g. ₹1,24,500.00) follows the Indian numbering
  system: the rightmost group has 3 digits, every subsequent group has 2.
- The confidence bar uses Unicode block characters for a smooth fill effect.
- ``format_time_ist`` always converts to IST (UTC+5:30) regardless of the
  input datetime's timezone.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from uuid import UUID

from core.enums import MarketRegime, TradeDirection


# ---------------------------------------------------------------------------
# IST timezone constant
# ---------------------------------------------------------------------------

_IST = timezone(timedelta(hours=5, minutes=30))

# Block characters for the confidence bar, from empty to full.
# Index 0 = empty cell, index 8 = full block.
_BAR_CHARS: list[str] = ["░", "▏", "▎", "▍", "▌", "▋", "▊", "▉", "█"]


# ---------------------------------------------------------------------------
# MarkdownV2 escaping
# ---------------------------------------------------------------------------

# Characters that must be escaped in Telegram MarkdownV2 (outside code spans).
# Source: https://core.telegram.org/bots/api#markdownv2-style
_MD_SPECIAL = r"\_*[]()~`>#+-=|{}.!"


def escape_md(text: str) -> str:
    """
    Escape all Telegram MarkdownV2 special characters in *text*.

    Call this on every dynamic value (prices, names, reasons, etc.) before
    embedding it in a formatted message.  Safe to call on already-escaped
    text only if done once — do NOT double-escape.

    Example
    -------
    >>> escape_md("P&L: +₹1,500.50 (2.5%)")
    'P&L: \\+₹1,500\\.50 \\(2\\.5%\\)'
    """
    return re.sub(r"([" + re.escape(_MD_SPECIAL) + r"])", r"\\\1", str(text))


# ---------------------------------------------------------------------------
# Confidence bar
# ---------------------------------------------------------------------------

def format_confidence_bar(score: float, width: int = 10) -> str:
    """
    Render a Unicode block-character progress bar for the given *score*.

    Parameters
    ----------
    score:
        Confidence value in the range [0.0, 1.0].
    width:
        Total number of cells in the bar.  Default is 10.

    Returns
    -------
    str
        A string of exactly *width* Unicode characters representing the fill.

    Examples
    --------
    >>> format_confidence_bar(0.78, 10)
    '███████▊░░'
    >>> format_confidence_bar(0.0, 10)
    '░░░░░░░░░░'
    >>> format_confidence_bar(1.0, 10)
    '██████████'
    """
    score = max(0.0, min(1.0, score))  # clamp to [0, 1]
    total_eighths = round(score * width * 8)  # each cell = 8 sub-units

    full_cells, remainder = divmod(total_eighths, 8)
    full_cells = min(full_cells, width)  # safety clamp

    bar = "█" * full_cells

    # Add a partial block if there is a remainder and space allows
    if len(bar) < width and remainder > 0:
        bar += _BAR_CHARS[remainder]

    # Pad the rest with empty blocks
    bar = bar.ljust(width, "░")

    return bar


# ---------------------------------------------------------------------------
# Currency formatting (Indian numbering system)
# ---------------------------------------------------------------------------

def format_currency(amount: float) -> str:
    """
    Format *amount* as Indian Rupees with the ₹ prefix.

    Applies the Indian numbering system: groups of 2 digits after the first
    group of 3 from the right (e.g. ₹12,34,567.89).

    Parameters
    ----------
    amount:
        Numeric value in INR.  May be negative.

    Returns
    -------
    str
        Formatted string like '₹1,24,500.00' or '-₹2,500.50'.

    Examples
    --------
    >>> format_currency(124500.0)
    '₹1,24,500.00'
    >>> format_currency(-2500.50)
    '-₹2,500.50'
    """
    negative = amount < 0
    abs_amount = abs(amount)

    # Split integer and fractional parts
    integer_part = int(abs_amount)
    fractional_part = round((abs_amount - integer_part) * 100)

    # Apply Indian grouping to the integer part
    int_str = str(integer_part)
    if len(int_str) <= 3:
        grouped = int_str
    else:
        # Last 3 digits stay together; the rest are grouped in pairs
        grouped = int_str[-3:]
        remaining = int_str[:-3]
        while remaining:
            grouped = remaining[-2:] + "," + grouped
            remaining = remaining[:-2]

    formatted = f"₹{grouped}.{fractional_part:02d}"
    return f"-{formatted}" if negative else formatted


# ---------------------------------------------------------------------------
# Percentage change formatting
# ---------------------------------------------------------------------------

def format_pct_change(pct: float) -> str:
    """
    Format a percentage change with a directional colour emoji.

    Parameters
    ----------
    pct:
        Percentage value (e.g. 2.34 means +2.34 %, -1.23 means -1.23 %).

    Returns
    -------
    str
        Human-readable string like '🟢 +2.34%' or '🔴 -1.23%' or '⚪ 0.00%'.

    Examples
    --------
    >>> format_pct_change(2.34)
    '🟢 +2.34%'
    >>> format_pct_change(-1.23)
    '🔴 -1.23%'
    >>> format_pct_change(0.0)
    '⚪ 0.00%'
    """
    if pct > 0:
        return f"🟢 +{pct:.2f}%"
    elif pct < 0:
        return f"🔴 {pct:.2f}%"
    else:
        return "⚪ 0.00%"


# ---------------------------------------------------------------------------
# Direction label
# ---------------------------------------------------------------------------

def format_direction(direction: TradeDirection | str) -> str:
    """
    Return a human-readable direction string with an emoji.

    Parameters
    ----------
    direction:
        A ``TradeDirection`` enum member or its string value ('BUY'/'SELL').

    Returns
    -------
    str
        '🔺 BUY' or '🔻 SELL'.

    Raises
    ------
    ValueError
        If *direction* is not a recognised value.

    Examples
    --------
    >>> format_direction(TradeDirection.BUY)
    '🔺 BUY'
    >>> format_direction('SELL')
    '🔻 SELL'
    """
    val = direction.value if isinstance(direction, TradeDirection) else str(direction).upper()
    if val == TradeDirection.BUY:
        return "🔺 BUY"
    elif val == TradeDirection.SELL:
        return "🔻 SELL"
    else:
        raise ValueError(f"Unknown direction: {direction!r}")


# ---------------------------------------------------------------------------
# Market regime label
# ---------------------------------------------------------------------------

# Mapping from MarketRegime value → display string
_REGIME_LABELS: dict[str, str] = {
    MarketRegime.TRENDING_BULL:     "📈 Trending Bull",
    MarketRegime.TRENDING_BEAR:     "📉 Trending Bear",
    MarketRegime.RANGE_BOUND:       "↔️ Range Bound",
    MarketRegime.VOLATILE_BREAKOUT: "⚡ Volatile Breakout",
    MarketRegime.REVERSAL:          "🔄 Reversal",
    MarketRegime.NEWS_DRIVEN:       "📰 News Driven",
    MarketRegime.UNKNOWN:           "❓ Unknown",
}


def format_regime(regime: MarketRegime | str) -> str:
    """
    Return a human-readable market regime label with an emoji.

    Parameters
    ----------
    regime:
        A ``MarketRegime`` enum member or its string value.

    Returns
    -------
    str
        Formatted regime string, e.g. '📈 Trending Bull'.

    Examples
    --------
    >>> format_regime(MarketRegime.TRENDING_BULL)
    '📈 Trending Bull'
    >>> format_regime('RANGE_BOUND')
    '↔️ Range Bound'
    """
    val = regime if isinstance(regime, str) else regime.value
    return _REGIME_LABELS.get(val, f"❓ {val}")


# ---------------------------------------------------------------------------
# Trade ID shortener
# ---------------------------------------------------------------------------

def format_trade_id(signal_id: UUID | str) -> str:
    """
    Return a short, human-friendly trade ID from a UUID.

    Takes the last 6 hex characters of the UUID and uppercases them.

    Parameters
    ----------
    signal_id:
        A ``UUID`` object or its string representation.

    Returns
    -------
    str
        A string like '#TRD-A3F9B2'.

    Examples
    --------
    >>> format_trade_id(UUID('12345678-1234-5678-1234-567812345678'))
    '#TRD-345678'
    """
    uid_str = str(signal_id).replace("-", "")
    return f"#TRD-{uid_str[-6:].upper()}"


# ---------------------------------------------------------------------------
# IST time formatting
# ---------------------------------------------------------------------------

def format_time_ist(dt: datetime) -> str:
    """
    Format a datetime as a human-readable IST time string.

    Converts *dt* to IST (UTC+5:30) regardless of its original timezone.
    Naive datetimes are assumed to be UTC.

    Parameters
    ----------
    dt:
        Any ``datetime`` object (tz-aware or naive/UTC).

    Returns
    -------
    str
        Formatted string like '2:45 PM IST'.

    Examples
    --------
    >>> from datetime import datetime, timezone
    >>> format_time_ist(datetime(2026, 7, 30, 9, 15, tzinfo=timezone.utc))
    '2:45 PM IST'
    """
    # Treat naive datetimes as UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ist_dt = dt.astimezone(_IST)
    hour = ist_dt.hour % 12 or 12
    minute = ist_dt.minute
    am_pm = "AM" if ist_dt.hour < 12 else "PM"
    return f"{hour}:{minute:02d} {am_pm} IST"


# ---------------------------------------------------------------------------
# Risk/Reward formatter
# ---------------------------------------------------------------------------

def format_risk_reward(rr: float) -> str:
    """
    Format a risk/reward ratio as '1 : X.XX'.

    Parameters
    ----------
    rr:
        Reward-to-risk ratio (e.g. 1.67 means reward is 1.67x the risk).

    Returns
    -------
    str
        A string like '1 : 1.67'.
    """
    return f"1 : {rr:.2f}"


# ---------------------------------------------------------------------------
# Stop-loss / target percentage formatters
# ---------------------------------------------------------------------------

def format_sl_pct(entry: float, sl: float) -> str:
    """
    Return the stop-loss as a percentage distance from entry, always negative.

    Example: entry=145, sl=101.5 -> '-30.00%'
    """
    if entry <= 0:
        return "N/A"
    pct = ((sl - entry) / entry) * 100
    return f"{pct:+.2f}%"


def format_target_pct(entry: float, target: float) -> str:
    """
    Return the target as a percentage distance from entry, positive for BUY.

    Example: entry=145, target=217.5 -> '+50.00%'
    """
    if entry <= 0:
        return "N/A"
    pct = ((target - entry) / entry) * 100
    return f"{pct:+.2f}%"
