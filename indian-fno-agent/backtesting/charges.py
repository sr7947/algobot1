"""
NSE realistic trading charges calculator for F&O.
Based on 2024 SEBI/NSE charge schedules.
"""
from __future__ import annotations

import math
from typing import Optional


def calculate_charges(
    trade_type: str,
    buy_value: float,
    sell_value: float,
    quantity: int,
    is_futures: bool = False,
    is_options: bool = False,
    brokerage_per_order: float = 20.0,
) -> dict:
    """
    Calculate realistic NSE F&O trading charges.

    Args:
        trade_type: 'futures' or 'options'
        buy_value: Total buy-side value (price * qty)
        sell_value: Total sell-side value (price * qty)
        quantity: Number of units traded
        is_futures: True for futures trades
        is_options: True for options trades
        brokerage_per_order: Flat brokerage per leg (default ₹20 discount broker)

    Returns:
        dict with breakdown: brokerage, stt, exchange_txn, sebi, stamp_duty, gst, total_charges
    """
    turnover = buy_value + sell_value

    # ── Brokerage ──
    # Discount broker model: flat ₹20 per executed order, 2 legs
    brokerage = brokerage_per_order * 2

    # ── STT (Securities Transaction Tax) ──
    if is_futures:
        # Futures: 0.0125% on sell side
        stt = sell_value * 0.000125
    elif is_options:
        # Options: 0.0625% on sell-side premium (for selling) or on exercise
        stt = sell_value * 0.000625
    else:
        stt = sell_value * 0.000125

    # ── Exchange Transaction Charges ──
    if is_futures:
        exchange_txn = turnover * 0.00002  # 0.002%
    elif is_options:
        exchange_txn = turnover * 0.00053  # 0.053%
    else:
        exchange_txn = turnover * 0.00002

    # ── SEBI Charges ──
    # ₹10 per crore of turnover
    sebi_charge = turnover * 10.0 / 10_000_000.0

    # ── Stamp Duty ──
    # 0.002% on buy side (varies by state, using Maharashtra rate)
    stamp_duty = buy_value * 0.00002

    # ── GST (18% on brokerage + exchange charges + SEBI charges) ──
    gst_base = brokerage + exchange_txn + sebi_charge
    gst = gst_base * 0.18

    # ── Total ──
    total = brokerage + stt + exchange_txn + sebi_charge + stamp_duty + gst

    return {
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "exchange_txn": round(exchange_txn, 2),
        "sebi_charge": round(sebi_charge, 2),
        "stamp_duty": round(stamp_duty, 2),
        "gst": round(gst, 2),
        "total_charges": round(total, 2),
        "turnover": round(turnover, 2),
    }


def annualize_return(total_return_pct: float, days: int) -> float:
    """
    Convert a total return over N days to annualised CAGR.

    Args:
        total_return_pct: Total return as percentage (e.g. 25.0 for 25%)
        days: Number of trading days

    Returns:
        Annualised return as percentage.
    """
    if days <= 0:
        return 0.0
    years = days / 252.0  # ~252 trading days/year
    total = 1 + total_return_pct / 100.0
    if total <= 0:
        return -100.0
    cagr = (total ** (1.0 / years) - 1) * 100.0
    return round(cagr, 2)


def calculate_sharpe(
    daily_returns: list[float],
    risk_free_rate: float = 0.065,
) -> float:
    """
    Calculate Sharpe ratio from daily returns.

    Args:
        daily_returns: List of daily return fractions (e.g. 0.01 for 1%)
        risk_free_rate: Annual risk-free rate (India: ~6.5%)

    Returns:
        Annualised Sharpe ratio.
    """
    if not daily_returns or len(daily_returns) < 2:
        return 0.0

    import numpy as np

    returns = np.array(daily_returns)
    daily_rf = (1 + risk_free_rate) ** (1 / 252) - 1
    excess = returns - daily_rf
    mean_excess = np.mean(excess)
    std_excess = np.std(excess, ddof=1)

    if std_excess == 0:
        return 0.0

    sharpe = (mean_excess / std_excess) * math.sqrt(252)
    return round(sharpe, 3)


def calculate_sortino(
    daily_returns: list[float],
    risk_free_rate: float = 0.065,
) -> float:
    """
    Calculate Sortino ratio (downside deviation only).
    """
    if not daily_returns or len(daily_returns) < 2:
        return 0.0

    import numpy as np

    returns = np.array(daily_returns)
    daily_rf = (1 + risk_free_rate) ** (1 / 252) - 1
    excess = returns - daily_rf
    downside = excess[excess < 0]

    if len(downside) == 0:
        return float("inf") if np.mean(excess) > 0 else 0.0

    mean_excess = np.mean(excess)
    downside_std = np.std(downside, ddof=1)

    if downside_std == 0:
        return 0.0

    sortino = (mean_excess / downside_std) * math.sqrt(252)
    return round(sortino, 3)


def estimate_margin_required(
    instrument_type: str,
    price: float,
    lot_size: int,
    quantity_lots: int = 1,
) -> float:
    """
    Estimate margin required for an F&O position.
    Very rough approximation — actual margins depend on SPAN + exposure.

    Args:
        instrument_type: 'FUT', 'CE', 'PE'
        price: Entry price
        lot_size: Contract lot size
        quantity_lots: Number of lots

    Returns:
        Estimated margin in INR.
    """
    total_value = price * lot_size * quantity_lots

    if instrument_type == "FUT":
        # Futures: ~12-15% SPAN + ~3-5% exposure margin
        return total_value * 0.18  # ~18% total
    elif instrument_type in ("CE", "PE"):
        # Options buying: full premium
        return total_value
    else:
        return total_value * 0.20  # Default 20%
