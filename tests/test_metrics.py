import pytest
import pandas as pd
import numpy as np
from datetime import date
from quant_engine.analytics.metrics import (
    annual_return,
    annual_volatility,
    downside_volatility,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
    calmar_ratio,
    win_rate,
    profit_loss_ratio,
)


class TestMetrics:
    @pytest.fixture
    def sample_returns(self):
        np.random.seed(42)
        dates = pd.date_range("2024-01-01", "2024-12-31", freq="B")
        returns = pd.Series(np.random.randn(len(dates)) * 0.01, index=dates)
        return returns

    @pytest.fixture
    def positive_returns(self):
        np.random.seed(7)
        dates = pd.date_range("2024-01-01", "2024-12-31", freq="B")
        returns = pd.Series(0.001 + np.random.rand(len(dates)) * 0.005, index=dates)
        return returns

    @pytest.fixture
    def drawdown_returns(self):
        dates = pd.date_range("2024-01-01", "2024-06-30", freq="B")
        # 先涨后跌
        n = len(dates)
        rets = np.zeros(n)
        rets[: n // 2] = 0.005
        rets[n // 2 :] = -0.01
        return pd.Series(rets, index=dates)

    @pytest.fixture
    def trades_df(self):
        """模拟卖出成交记录"""
        return pd.DataFrame({
            "trade_id": [1, 2, 3, 4, 5],
            "code": ["000001"] * 5,
            "side": ["sell"] * 5,
            "shares": [100] * 5,
            "price": [10.0] * 5,
            "amount": [1000, 800, 1200, 900, 1100],
            "commission": [1.0] * 5,
            "stamp_duty": [1.0] * 5,
        })

    def test_annual_return_positive(self, positive_returns):
        ar = annual_return(positive_returns)
        assert ar > 0

    def test_annual_return_zero_on_empty(self):
        ar = annual_return(pd.Series([], dtype=float))
        assert ar == 0.0

    def test_annual_volatility(self, sample_returns):
        vol = annual_volatility(sample_returns)
        assert vol > 0

    def test_annual_volatility_on_empty(self):
        vol = annual_volatility(pd.Series([], dtype=float))
        assert vol == 0.0

    def test_downside_volatility(self, drawdown_returns):
        down_vol = downside_volatility(drawdown_returns)
        assert down_vol >= 0

    def test_max_drawdown(self, drawdown_returns):
        mdd, peak, trough, recovery = max_drawdown(drawdown_returns)
        assert mdd < 0  # 回撤为负
        assert mdd < -0.1  # 至少 10% 回撤

    def test_max_drawdown_no_drawdown(self, positive_returns):
        mdd, peak, trough, recovery = max_drawdown(positive_returns)
        assert mdd <= 0  # 可能接近 0 但不为正

    def test_max_drawdown_on_empty(self):
        mdd, peak, trough, recovery = max_drawdown(pd.Series([], dtype=float))
        assert mdd == 0.0
        assert peak is None

    def test_sharpe_positive(self, positive_returns):
        sr = sharpe_ratio(positive_returns, rf=0.02)
        assert sr > 0  # 正收益应该正夏普

    def test_sharpe_negative(self, sample_returns):
        # 加负偏
        bad = sample_returns - 0.001
        sr = sharpe_ratio(bad, rf=0.02)
        assert isinstance(sr, float)

    def test_sharpe_on_empty(self):
        sr = sharpe_ratio(pd.Series([], dtype=float))
        assert sr == 0.0

    def test_sortino_positive(self, positive_returns):
        sor = sortino_ratio(positive_returns, rf=0.02)
        # 全正收益 → 无下行波动 → 返回 +inf 或正值
        assert sor > 0 or sor == float("inf")

    def test_calmar_ratio(self, positive_returns):
        cr = calmar_ratio(positive_returns)
        assert isinstance(cr, float)

    def test_calmar_on_empty(self):
        cr = calmar_ratio(pd.Series([], dtype=float))
        assert cr == 0.0

    def test_win_rate(self, trades_df):
        wr = win_rate(trades_df)
        assert 0.0 <= wr <= 1.0

    def test_win_rate_on_empty(self):
        wr = win_rate(pd.DataFrame())
        assert wr == 0.0

    def test_profit_loss_ratio(self, trades_df):
        plr = profit_loss_ratio(trades_df)
        assert plr > 0

    def test_profit_loss_ratio_on_empty(self):
        plr = profit_loss_ratio(pd.DataFrame())
        assert plr == 0.0


class TestMaxDrawdownEdgeCases:
    """针对 max_drawdown 的边界场景测试"""

    def test_single_drawdown_at_end(self):
        """前一半涨, 后一半跌, 且谷底在末尾 → 恢复天数为直到末尾"""
        dates = pd.date_range("2024-01-01", periods=20, freq="B")
        rets = pd.Series([0.01] * 10 + [-0.02] * 10, index=dates)
        mdd, peak, trough, recovery = max_drawdown(rets)
        assert mdd < 0
        assert recovery > 0

    def test_sharp_v_recovery(self):
        """V 形反转: 跌后立即回到原值"""
        dates = pd.date_range("2024-01-01", periods=15, freq="B")
        # 先慢慢涨, 再暴跌一天, 再慢慢涨
        rets = pd.Series(
            [0.001] * 5 + [-0.10] + [0.02] * 9,
            index=dates,
        )
        mdd, peak, trough, recovery = max_drawdown(rets)
        assert mdd < -0.05
