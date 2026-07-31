"""
Tests for the NSE F&O charges calculator (unittest compatible).
"""
import unittest
from backtesting.charges import (
    calculate_charges,
    calculate_sharpe,
    calculate_sortino,
    annualize_return,
    estimate_margin_required,
)


class TestCalculateCharges(unittest.TestCase):
    """Tests for NSE F&O charge calculation."""

    def test_futures_charges_basic(self):
        """Basic futures trade charge calculation."""
        result = calculate_charges(
            trade_type="futures",
            buy_value=1_200_000,  # 24000 * 50
            sell_value=1_220_000,  # 24400 * 50
            quantity=50,
            is_futures=True,
        )
        self.assertIn("total_charges", result)
        self.assertGreater(result["total_charges"], 0)
        self.assertEqual(result["brokerage"], 40.0)  # 20 * 2 legs
        self.assertGreater(result["stt"], 0)
        self.assertGreater(result["gst"], 0)
        self.assertEqual(result["turnover"], 2_420_000)

    def test_options_charges_basic(self):
        """Basic options trade charge calculation."""
        result = calculate_charges(
            trade_type="options",
            buy_value=7250,   # 145 * 50
            sell_value=10875,  # 217.5 * 50
            quantity=50,
            is_options=True,
        )
        self.assertGreater(result["total_charges"], 0)
        self.assertEqual(result["brokerage"], 40.0)

    def test_futures_stt_on_sell_side(self):
        """STT for futures is 0.0125% on sell side only."""
        result = calculate_charges(
            trade_type="futures",
            buy_value=1_000_000,
            sell_value=1_000_000,
            quantity=50,
            is_futures=True,
        )
        expected_stt = 1_000_000 * 0.000125  # 125
        self.assertAlmostEqual(result["stt"], expected_stt, places=2)

    def test_options_stt_on_sell_side(self):
        """STT for options is 0.0625% on sell side."""
        result = calculate_charges(
            trade_type="options",
            buy_value=50_000,
            sell_value=50_000,
            quantity=50,
            is_options=True,
        )
        expected_stt = 50_000 * 0.000625  # 31.25
        self.assertAlmostEqual(result["stt"], expected_stt, places=2)

    def test_gst_is_18_pct_on_base(self):
        """GST should be 18% of (brokerage + exchange txn + SEBI charge)."""
        result = calculate_charges(
            trade_type="futures",
            buy_value=1_000_000,
            sell_value=1_000_000,
            quantity=50,
            is_futures=True,
        )
        gst_base = result["brokerage"] + result["exchange_txn"] + result["sebi_charge"]
        expected_gst = gst_base * 0.18
        self.assertAlmostEqual(result["gst"], round(expected_gst, 2), places=2)

    def test_zero_trade_value(self):
        """Charges with zero values should still return structure."""
        result = calculate_charges("futures", 0, 0, 0, is_futures=True)
        self.assertGreaterEqual(result["total_charges"], 0)
        self.assertEqual(result["turnover"], 0)

    def test_custom_brokerage(self):
        """Custom brokerage per order."""
        result = calculate_charges(
            "futures", 100_000, 100_000, 50,
            is_futures=True, brokerage_per_order=0,
        )
        self.assertEqual(result["brokerage"], 0)


class TestSharpeRatio(unittest.TestCase):
    """Tests for Sharpe ratio calculation."""

    def test_positive_returns(self):
        """Consistently positive returns should give positive Sharpe."""
        returns = [0.01, 0.02, 0.015, 0.01, 0.025, 0.01, 0.02, 0.015]
        sharpe = calculate_sharpe(returns)
        self.assertGreater(sharpe, 0)

    def test_negative_returns(self):
        """Consistently negative returns should give negative Sharpe."""
        returns = [-0.01, -0.02, -0.015, -0.01, -0.025]
        sharpe = calculate_sharpe(returns)
        self.assertLess(sharpe, 0)

    def test_empty_returns(self):
        """Empty returns list should return 0."""
        self.assertEqual(calculate_sharpe([]), 0.0)

    def test_single_return(self):
        """Single return should return 0 (can't compute std)."""
        self.assertEqual(calculate_sharpe([0.01]), 0.0)

    def test_zero_std_returns_zero(self):
        """If all returns are identical, std=0, should return 0."""
        sharpe = calculate_sharpe([0.0, 0.0, 0.0, 0.0])
        self.assertEqual(sharpe, 0.0)


class TestSortinoRatio(unittest.TestCase):
    """Tests for Sortino ratio calculation."""

    def test_all_positive_returns(self):
        """All positive returns should give inf or very high Sortino."""
        returns = [0.01, 0.02, 0.015, 0.03]
        sortino = calculate_sortino(returns)
        self.assertTrue(sortino == float("inf") or sortino > 0)

    def test_mixed_returns(self):
        """Mixed returns should give finite Sortino."""
        returns = [0.02, -0.01, 0.03, -0.005, 0.01, -0.02]
        sortino = calculate_sortino(returns)
        self.assertIsInstance(sortino, float)

    def test_empty_returns(self):
        self.assertEqual(calculate_sortino([]), 0.0)


class TestAnnualizeReturn(unittest.TestCase):
    """Tests for return annualization."""

    def test_one_year_return(self):
        """25% over 252 trading days = 25% CAGR."""
        cagr = annualize_return(25.0, 252)
        self.assertLess(abs(cagr - 25.0), 0.5)

    def test_half_year_return(self):
        """10% over 126 days annualizes to ~21%."""
        cagr = annualize_return(10.0, 126)
        self.assertGreater(cagr, 10.0)

    def test_zero_days(self):
        self.assertEqual(annualize_return(10.0, 0), 0.0)

    def test_negative_return(self):
        cagr = annualize_return(-20.0, 252)
        self.assertLess(cagr, 0)


class TestMarginEstimation(unittest.TestCase):
    """Tests for margin estimation."""

    def test_futures_margin(self):
        """Futures margin should be ~18% of notional."""
        margin = estimate_margin_required("FUT", 24000, 50)
        expected = 24000 * 50 * 0.18  # 216000
        self.assertAlmostEqual(margin, expected, places=1)

    def test_options_buy_margin(self):
        """Options buy margin = full premium."""
        margin = estimate_margin_required("CE", 150, 50)
        self.assertEqual(margin, 150 * 50)  # 7500

    def test_multiple_lots(self):
        """Multiple lots should scale linearly."""
        margin_1 = estimate_margin_required("FUT", 24000, 50, 1)
        margin_3 = estimate_margin_required("FUT", 24000, 50, 3)
        self.assertAlmostEqual(margin_3, margin_1 * 3, places=1)


if __name__ == "__main__":
    unittest.main()
