"""
risk/delta_margin.py
====================
Delta Exchange India margin & leverage calculations.

Mirrors the Isolated Margin formulas published by Delta Exchange India:

  Order / Initial Margin
  ----------------------
  IM = (Initial Margin% / 100) × #Contracts × Multiplier × Price

  where:
    Initial Margin% = max(100 / OrderLeverage, RiskLimitIM%)
    RiskLimitIM%    = IM%_MIN                         if size ≤ threshold
                    = IM%_MIN + Slope_IM × (size − threshold)  otherwise

  Support-centre shorthand (Contract Value = Multiplier × Price):
    Order Margin = Order Size × Contract Value × Initial Margin%

  Position (effective) Leverage
  -----------------------------
  Position Leverage = Position Value / (Position Margin + Unrealised PnL)
  Position Value    = #Contracts × Multiplier × Mark Price   (vanilla)

  Available Balance
  -----------------
  Available Balance = Wallet Balance − (Position Margin + Order Margin)

Product fields (from GET /v2/products):
  contract_value                 → Multiplier (e.g. BTCUSD = 0.001)
  initial_margin                 → IM%_MIN as a percent (e.g. 0.5 → 0.5%)
  initial_margin_scaling_factor  → Slope_IM
  default_leverage               → exchange default order leverage
  notional_type                  → "vanilla" | "inverse"
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


# Fallback product specs when live catalogue is unavailable (Delta India / testnet).
_DEFAULT_PRODUCTS: dict[str, dict[str, float | str]] = {
    "BTCUSD": {
        "contract_value": 0.001,
        "initial_margin": 0.5,          # percent
        "maintenance_margin": 0.25,     # percent
        "initial_margin_scaling_factor": 0.0,
        "default_leverage": 10.0,
        "notional_type": "vanilla",
    },
    "ETHUSD": {
        "contract_value": 0.01,
        "initial_margin": 1.0,
        "maintenance_margin": 0.5,
        "initial_margin_scaling_factor": 0.0,
        "default_leverage": 5.0,
        "notional_type": "vanilla",
    },
}


@dataclass(frozen=True)
class DeltaProductSpec:
    """Margin-relevant product parameters from Delta's product catalogue."""

    symbol: str
    contract_value: float          # Multiplier
    initial_margin_pct: float      # IM%_MIN (percent units, e.g. 0.5)
    maintenance_margin_pct: float  # MM%_MIN (percent units)
    im_scaling_factor: float      # Slope_IM
    default_leverage: float
    notional_type: str = "vanilla"
    position_threshold: float = 0.0  # contracts; 0 → scaling applies from size 0


def product_spec_from_dict(symbol: str, raw: Mapping[str, Any]) -> DeltaProductSpec:
    """Build a ``DeltaProductSpec`` from a Delta ``/v2/products`` payload."""
    return DeltaProductSpec(
        symbol=symbol.upper(),
        contract_value=float(
            raw.get("contract_value")
            or _DEFAULT_PRODUCTS.get(symbol.upper(), {}).get("contract_value", 0.001)
        ),
        initial_margin_pct=float(raw.get("initial_margin") or 0.5),
        maintenance_margin_pct=float(raw.get("maintenance_margin") or 0.25),
        im_scaling_factor=float(raw.get("initial_margin_scaling_factor") or 0.0),
        default_leverage=float(raw.get("default_leverage") or 10.0),
        notional_type=str(raw.get("notional_type") or "vanilla").lower(),
        position_threshold=float(raw.get("position_threshold") or 0.0),
    )


def get_default_product_spec(symbol: str = "BTCUSD") -> DeltaProductSpec:
    """Return baked-in defaults for common India/testnet perpetual symbols."""
    sym = symbol.upper()
    raw = _DEFAULT_PRODUCTS.get(sym, _DEFAULT_PRODUCTS["BTCUSD"])
    return product_spec_from_dict(sym, raw)


def initial_margin_pct_for_leverage(
    leverage: float,
    product: Optional[DeltaProductSpec] = None,
    size: float = 0.0,
) -> float:
    """
    Effective Initial Margin % for a chosen order leverage.

    Delta India rule:
        selected_im%  = 100 / order_leverage
        risk_limit_im% = IM%_MIN (+ slope × excess size above threshold)
        effective_im%  = max(selected_im%, risk_limit_im%)
    """
    lev = max(float(leverage), 1.0)
    selected = 100.0 / lev

    prod = product or get_default_product_spec()
    excess = max(0.0, abs(float(size)) - prod.position_threshold)
    risk_limit = prod.initial_margin_pct + prod.im_scaling_factor * excess
    return max(selected, risk_limit)


def position_notional(
    size: float,
    price: float,
    product: Optional[DeltaProductSpec] = None,
) -> float:
    """
    Position / order notional (Position Value) in settlement currency.

    Vanilla (USD-settled, e.g. India BTCUSD):
        Value = |size| × contract_value × price
    Inverse:
        Value = |size| × contract_value / price   (coin-margined)
    """
    prod = product or get_default_product_spec()
    abs_size = abs(float(size))
    px = float(price)
    if px <= 0 or abs_size <= 0:
        return 0.0

    if prod.notional_type == "inverse":
        return abs_size * prod.contract_value / px
    return abs_size * prod.contract_value * px


def order_margin(
    size: float,
    price: float,
    leverage: float,
    product: Optional[DeltaProductSpec] = None,
) -> float:
    """
    Initial / order margin reserved for a standalone order (Delta India).

    Equivalent forms:
        IM = (IM% / 100) × size × multiplier × price          # vanilla
        IM = size × (multiplier × price) × (IM% / 100)        # support shorthand
    """
    prod = product or get_default_product_spec()
    im_pct = initial_margin_pct_for_leverage(leverage, prod, size=size)
    notional = position_notional(size, price, prod)
    return notional * (im_pct / 100.0)


def position_leverage(
    size: float,
    mark_price: float,
    position_margin: float,
    unrealised_pnl: float = 0.0,
    product: Optional[DeltaProductSpec] = None,
) -> float:
    """
    Effective / Position Leverage (Delta India):

        Position Leverage = Position Value / (Position Margin + Unrealised PnL)

    When UPNL is 0 (fresh fill), this equals the order leverage used to open.
    """
    value = position_notional(size, mark_price, product)
    denominator = float(position_margin) + float(unrealised_pnl)
    if denominator <= 0 or value <= 0:
        return 0.0
    return value / denominator


def available_balance(
    wallet_balance: float,
    position_margin: float,
    order_margin_total: float = 0.0,
) -> float:
    """Available Balance = Wallet Balance − (Position Margin + Order Margin)."""
    return max(0.0, float(wallet_balance) - float(position_margin) - float(order_margin_total))


def estimate_position_margin(
    size: float,
    entry_price: float,
    leverage: float,
    product: Optional[DeltaProductSpec] = None,
    symbol: Optional[str] = None,
) -> float:
    """
    Estimate position margin for a filled order (UPNL ≈ 0 at entry).

    Same numeric result as ``order_margin`` for a market buy at the entry price.
    """
    prod = product
    if prod is None and symbol:
        prod = get_default_product_spec(symbol)
    return order_margin(size, entry_price, leverage, prod)
