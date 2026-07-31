"""
tests/unit/test_delta_leverage.py
=================================
Unit tests for Delta Exchange India leverage & margin formulas.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, AsyncMock


class TestDeltaIndiaMarginFormulas(unittest.TestCase):
    """Verify formulas against Delta Exchange India documentation."""

    def test_im_pct_equals_100_over_leverage(self):
        from risk.delta_margin import initial_margin_pct_for_leverage, get_default_product_spec

        btc = get_default_product_spec("BTCUSD")
        self.assertAlmostEqual(initial_margin_pct_for_leverage(10, btc), 10.0)
        self.assertAlmostEqual(initial_margin_pct_for_leverage(25, btc), 4.0)
        self.assertAlmostEqual(initial_margin_pct_for_leverage(4, btc), 25.0)

    def test_im_pct_floored_by_product_minimum(self):
        from risk.delta_margin import initial_margin_pct_for_leverage, get_default_product_spec

        btc = get_default_product_spec("BTCUSD")  # IM%_MIN = 0.5
        # 200x → 0.5%, equals product minimum
        self.assertAlmostEqual(initial_margin_pct_for_leverage(200, btc), 0.5)
        # Above max (would be < 0.5%) still floored at 0.5%
        self.assertAlmostEqual(initial_margin_pct_for_leverage(500, btc), 0.5)

    def test_order_margin_vanilla_btcusd_10x(self):
        """
        Delta India example path:
          1 BTCUSD contract @ $65,200, 10x leverage
          IM% = 10, Contract Value (notional) = 0.001 × 65200 = $65.20
          Order Margin = 65.20 × 0.10 = $6.52
        """
        from risk.delta_margin import order_margin, get_default_product_spec

        btc = get_default_product_spec("BTCUSD")
        margin = order_margin(size=1, price=65200.0, leverage=10, product=btc)
        self.assertAlmostEqual(margin, 6.52)

    def test_order_margin_vanilla_btcusd_25x(self):
        """At 25x: IM% = 4 → margin = 65.20 × 0.04 = $2.608"""
        from risk.delta_margin import order_margin, get_default_product_spec

        btc = get_default_product_spec("BTCUSD")
        margin = order_margin(size=1, price=65200.0, leverage=25, product=btc)
        self.assertAlmostEqual(margin, 2.608)

    def test_support_shorthand_matches_detailed_formula(self):
        """Order Size × (Multiplier × Price) × (IM%/100) == detailed IM formula."""
        from risk.delta_margin import (
            get_default_product_spec,
            initial_margin_pct_for_leverage,
            order_margin,
        )

        btc = get_default_product_spec("BTCUSD")
        size, price, lev = 3, 64000.0, 25
        im_pct = initial_margin_pct_for_leverage(lev, btc, size=size)
        shorthand = size * (btc.contract_value * price) * (im_pct / 100.0)
        self.assertAlmostEqual(order_margin(size, price, lev, btc), shorthand)

    def test_position_leverage_at_entry_equals_order_leverage(self):
        from risk.delta_margin import (
            get_default_product_spec,
            order_margin,
            position_leverage,
        )

        btc = get_default_product_spec("BTCUSD")
        size, price, lev = 2, 65000.0, 25
        margin = order_margin(size, price, lev, btc)
        # Fresh fill: UPNL = 0 → position leverage == order leverage
        self.assertAlmostEqual(
            position_leverage(size, price, margin, unrealised_pnl=0.0, product=btc),
            lev,
            places=6,
        )

    def test_position_leverage_falls_when_upnl_positive(self):
        from risk.delta_margin import (
            get_default_product_spec,
            order_margin,
            position_leverage,
        )

        btc = get_default_product_spec("BTCUSD")
        size, price, lev = 1, 65000.0, 25
        margin = order_margin(size, price, lev, btc)
        # Mark moved in favour → UPNL > 0 → effective leverage drops
        effective = position_leverage(size, price, margin, unrealised_pnl=1.0, product=btc)
        self.assertLess(effective, lev)

    def test_available_balance_formula(self):
        from risk.delta_margin import available_balance

        self.assertAlmostEqual(available_balance(200.0, 2.608, 0.0), 197.392)
        self.assertAlmostEqual(available_balance(200.0, 150.0, 60.0), 0.0)

    def test_ethusd_contract_value(self):
        from risk.delta_margin import get_default_product_spec, order_margin

        eth = get_default_product_spec("ETHUSD")
        self.assertEqual(eth.contract_value, 0.01)
        # 1 ETHUSD @ $3500, 5x → IM% = 20 → margin = 1 × 0.01 × 3500 × 0.20 = 7.0
        self.assertAlmostEqual(order_margin(1, 3500.0, 5, eth), 7.0)

    def test_im_scaling_increases_floor(self):
        from risk.delta_margin import (
            DeltaProductSpec,
            initial_margin_pct_for_leverage,
        )

        prod = DeltaProductSpec(
            symbol="BTCUSD",
            contract_value=0.001,
            initial_margin_pct=0.5,
            maintenance_margin_pct=0.25,
            im_scaling_factor=0.0000025,
            default_leverage=200.0,
            position_threshold=0.0,
        )
        # Huge size pushes risk-limit IM% above selected 25x (4%)
        huge = 2_000_000
        im = initial_margin_pct_for_leverage(25, prod, size=huge)
        self.assertGreater(im, 4.0)
        self.assertAlmostEqual(im, 0.5 + 0.0000025 * huge)


class TestDeltaDefaultLeverageSettings(unittest.TestCase):
    def test_settings_default_leverage_is_25(self):
        from config.settings import Settings

        settings = Settings(
            _env_file=None,
            DELTA_API_KEY=None,
            DELTA_API_SECRET=None,
        )
        self.assertEqual(settings.DELTA_DEFAULT_LEVERAGE, 25.0)

    def test_broker_default_leverage_helper(self):
        from broker.delta_exchange import DeltaExchangeBroker

        settings = MagicMock()
        settings.DELTA_API_KEY = "k"
        settings.DELTA_API_SECRET = "s"
        settings.DELTA_ENV = "paper"
        settings.DELTA_DEFAULT_LEVERAGE = 25.0

        broker = DeltaExchangeBroker(settings)
        self.assertEqual(broker._default_leverage(), 25.0)

    def test_get_broker_config_includes_leverage(self):
        from config.settings import Settings

        settings = Settings(
            _env_file=None,
            BROKER="delta_exchange",
            DELTA_API_KEY="key",
            DELTA_API_SECRET="secret",
            DELTA_ENV="paper",
        )
        cfg = settings.get_broker_config()
        self.assertEqual(cfg["default_leverage"], 25.0)


class TestDeltaSetLeverage(unittest.IsolatedAsyncioTestCase):
    async def test_place_order_sets_leverage_before_submit(self):
        from broker.delta_exchange import DeltaExchangeBroker

        settings = MagicMock()
        settings.DELTA_API_KEY = "k"
        settings.DELTA_API_SECRET = "s"
        settings.DELTA_ENV = "paper"
        settings.DELTA_DEFAULT_LEVERAGE = 25.0

        broker = DeltaExchangeBroker(settings)
        broker._product_id_map["BTCUSD"] = 27
        broker._authenticated = True

        set_lev = AsyncMock(return_value={"leverage": "25"})
        request = AsyncMock(
            return_value={"success": True, "result": {"id": 99, "state": "open"}}
        )
        broker.set_leverage = set_lev
        broker._request = request

        order = MagicMock()
        order.symbol = "BTCUSD"
        order.quantity = 1
        order.direction = "BUY"
        order.order_type = "MARKET"
        order.price = None
        order.trigger_price = None
        order.product_id = None
        order.leverage = None

        resp = await broker.place_order(order)
        set_lev.assert_awaited_once_with(27, leverage=None)
        self.assertEqual(str(resp.broker_order_id), "99")

    async def test_set_leverage_posts_default_25(self):
        from broker.delta_exchange import DeltaExchangeBroker

        settings = MagicMock()
        settings.DELTA_API_KEY = "k"
        settings.DELTA_API_SECRET = "s"
        settings.DELTA_ENV = "paper"
        settings.DELTA_DEFAULT_LEVERAGE = 25.0

        broker = DeltaExchangeBroker(settings)
        broker._request = AsyncMock(
            return_value={"success": True, "result": {"leverage": "25"}}
        )

        result = await broker.set_leverage(27)
        call_kwargs = broker._request.await_args
        self.assertEqual(call_kwargs.args[0], "POST")
        self.assertIn("/v2/products/27/orders/leverage", call_kwargs.args[1])
        self.assertEqual(call_kwargs.kwargs["payload"]["leverage"], "25.0")
        self.assertEqual(result.get("leverage"), "25")


if __name__ == "__main__":
    unittest.main()
