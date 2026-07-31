"""
tests/unit/test_delta_leverage.py
=================================
Unit tests for Delta Exchange default leverage (25x).
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch, AsyncMock


class TestDeltaDefaultLeverage(unittest.TestCase):
    """Default crypto order leverage should be 25x."""

    def test_settings_default_leverage_is_25(self):
        from config.settings import Settings

        settings = Settings(
            _env_file=None,
            DELTA_API_KEY=None,
            DELTA_API_SECRET=None,
        )
        self.assertEqual(settings.DELTA_DEFAULT_LEVERAGE, 25.0)

    def test_margin_fraction_at_25x(self):
        leverage = 25.0
        margin_fraction = 1.0 / leverage
        self.assertAlmostEqual(margin_fraction, 0.04)

        # 1 contract BTCUSD @ $65,200 with contract size 0.001 BTC
        entry = 65200.0
        qty = 1
        contract_size = 0.001
        used_margin = entry * qty * contract_size * margin_fraction
        self.assertAlmostEqual(used_margin, 2.608)

    def test_margin_fraction_uses_position_leverage(self):
        default_leverage = 25.0
        positions = [
            {"entry": 65200.0, "qty": 1, "leverage": 25.0},
            {"entry": 65200.0, "qty": 2, "leverage": 10.0},  # legacy 10x position
        ]
        used = sum(
            float(p["entry"])
            * int(p["qty"])
            * 0.001
            * (1.0 / max(1.0, float(p.get("leverage", default_leverage))))
            for p in positions
        )
        # 25x: 65200*1*0.001*0.04 = 2.608
        # 10x: 65200*2*0.001*0.1 = 13.04
        self.assertAlmostEqual(used, 2.608 + 13.04)

    def test_broker_default_leverage_helper(self):
        from broker.delta_exchange import DeltaExchangeBroker

        settings = MagicMock()
        settings.DELTA_API_KEY = "k"
        settings.DELTA_API_SECRET = "s"
        settings.DELTA_ENV = "paper"
        settings.DELTA_DEFAULT_LEVERAGE = 25.0

        broker = DeltaExchangeBroker(settings)
        self.assertEqual(broker._default_leverage(), 25.0)

    def test_broker_default_leverage_fallback(self):
        from broker.delta_exchange import DeltaExchangeBroker

        settings = MagicMock(spec=[])
        # No DELTA_DEFAULT_LEVERAGE attribute → fallback 25
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
        self.assertEqual(cfg["broker"], "delta_exchange")
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
            return_value={
                "success": True,
                "result": {"id": 99, "state": "open"},
            }
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
        broker._request.assert_awaited_once()
        call_kwargs = broker._request.await_args
        self.assertEqual(call_kwargs.args[0], "POST")
        self.assertIn("/v2/products/27/orders/leverage", call_kwargs.args[1])
        self.assertEqual(call_kwargs.kwargs["payload"]["leverage"], "25.0")
        self.assertEqual(result.get("leverage"), "25")


if __name__ == "__main__":
    unittest.main()
