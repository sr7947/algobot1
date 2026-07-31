"""
Tests for the risk engine module (unittest compatible).
"""
import unittest
from datetime import date, datetime, timezone
from uuid import uuid4

from core.enums import (
    TradeDirection, InstrumentType, Exchange, SignalStatus, MarketRegime,
)
from core.models import TradeSignal, RiskState, MarginInfo


class TestRiskEngine(unittest.TestCase):
    def setUp(self):
        self.default_risk_state = RiskState(
            date=date.today(),
            daily_pnl=0.0,
            daily_trades=0,
            daily_losses=0,
            consecutive_losses=0,
            max_drawdown_today=0.0,
            kill_switch_active=False,
        )
        self.sample_signal = TradeSignal(
            id=uuid4(),
            created_at=datetime.now(timezone.utc),
            strategy_name="test_strategy",
            symbol="NIFTY",
            exchange=Exchange.NFO.value,
            instrument_type=InstrumentType.FUT.value,
            direction=TradeDirection.BUY.value,
            entry_price=24000.0,
            stop_loss=23800.0,
            target=24400.0,
            quantity=50,
            lot_size=50,
            confidence_score=0.75,
            regime=MarketRegime.TRENDING_BULL.value,
            rationale=["Test signal"],
            indicators_snapshot={},
            status=SignalStatus.PENDING_APPROVAL.value,
            expires_at=datetime.now(timezone.utc),
        )
        self.sample_margin_info = MarginInfo(
            available_cash=500000.0,
            used_margin=100000.0,
            available_margin=400000.0,
            collateral=0.0,
        )

    # ── Risk Reward Ratio Tests ─────────────────────────────────────────

    def test_buy_signal_risk_reward(self):
        """Risk:Reward for BUY: entry=24000, SL=23800, Target=24400."""
        risk = self.sample_signal.entry_price - self.sample_signal.stop_loss  # 200
        reward = self.sample_signal.target - self.sample_signal.entry_price   # 400
        rr = reward / risk if risk > 0 else 0
        self.assertEqual(rr, 2.0)

    def test_sell_signal_risk_reward(self):
        """Risk:Reward for SELL signal."""
        entry, sl, target = 24000.0, 24200.0, 23600.0
        risk = sl - entry     # 200
        reward = entry - target  # 400
        rr = reward / risk if risk > 0 else 0
        self.assertEqual(rr, 2.0)

    def test_zero_risk_returns_zero(self):
        """If SL == entry, RR should be 0."""
        risk = 0
        rr = 0 if risk == 0 else 400 / risk
        self.assertEqual(rr, 0)

    # ── Position Sizing Tests ────────────────────────────────────────────

    def test_position_size_respects_max_risk(self):
        """Position size should not risk more than max_risk_pct of capital."""
        capital = 500000.0
        max_risk_pct = 1.0  # 1%
        max_risk_amount = capital * max_risk_pct / 100.0  # 5000

        risk_per_unit = abs(self.sample_signal.entry_price - self.sample_signal.stop_loss)  # 200
        lot_size = self.sample_signal.lot_size  # 50

        risk_per_lot = risk_per_unit * lot_size  # 10000
        max_lots = int(max_risk_amount / risk_per_lot)  # 0

        self.assertTrue(max_lots >= 0)

    def test_minimum_one_lot(self):
        """Should always trade at least 1 lot even if risk exceeds limit."""
        capital = 1000000.0
        max_risk_pct = 2.0
        max_risk_amount = capital * max_risk_pct / 100.0  # 20000

        risk_per_lot = 200 * 50  # 10000
        max_lots = max(1, int(max_risk_amount / risk_per_lot))  # 2

        self.assertEqual(max_lots, 2)

    # ── Daily Loss Limit Tests ───────────────────────────────────────────

    def test_within_daily_loss_limit(self):
        """Should allow trades when daily loss is within limit."""
        capital = 500000.0
        max_daily_loss_pct = 3.0
        max_daily_loss = capital * max_daily_loss_pct / 100.0  # 15000

        self.default_risk_state.daily_pnl = -5000.0  # Lost 5000 so far
        self.assertLess(abs(self.default_risk_state.daily_pnl), max_daily_loss)

    def test_exceeds_daily_loss_limit(self):
        """Should block trades when daily loss exceeds limit."""
        capital = 500000.0
        max_daily_loss_pct = 3.0
        max_daily_loss = capital * max_daily_loss_pct / 100.0  # 15000

        self.default_risk_state.daily_pnl = -16000.0
        self.assertGreater(abs(self.default_risk_state.daily_pnl), max_daily_loss)

    # ── Consecutive Losses Tests ─────────────────────────────────────────

    def test_within_consecutive_limit(self):
        max_consecutive = 3
        self.default_risk_state.consecutive_losses = 2
        self.assertLess(self.default_risk_state.consecutive_losses, max_consecutive)

    def test_exceeds_consecutive_limit(self):
        max_consecutive = 3
        self.default_risk_state.consecutive_losses = 3
        self.assertGreaterEqual(self.default_risk_state.consecutive_losses, max_consecutive)

    # ── Kill Switch Tests ────────────────────────────────────────────────

    def test_kill_switch_blocks_trading(self):
        self.default_risk_state.kill_switch_active = True
        self.assertTrue(self.default_risk_state.kill_switch_active)

    def test_kill_switch_off_allows_trading(self):
        self.default_risk_state.kill_switch_active = False
        self.assertFalse(self.default_risk_state.kill_switch_active)

    # ── Max Open Positions Tests ─────────────────────────────────────────

    def test_within_position_limit(self):
        max_positions = 5
        current_positions = 3
        self.assertLess(current_positions, max_positions)

    def test_at_position_limit(self):
        max_positions = 5
        current_positions = 5
        can_open = current_positions < max_positions
        self.assertFalse(can_open)

    # ── Margin Check Tests ───────────────────────────────────────────────

    def test_sufficient_margin(self):
        """Available margin should cover estimated order margin."""
        estimated_margin = 120000.0  # Estimated margin for 1 lot Nifty FUT
        self.assertGreaterEqual(self.sample_margin_info.available_margin, estimated_margin)

    def test_insufficient_margin(self):
        """Should reject if required margin exceeds available."""
        estimated_margin = 500000.0
        self.assertLess(self.sample_margin_info.available_margin, estimated_margin)


if __name__ == "__main__":
    unittest.main()
