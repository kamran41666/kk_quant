import pytest
from datetime import date, timedelta
from quant_engine.backtest.types import Trade, OrderSide, Position
from quant_engine.backtest.portfolio import Portfolio


class TestPortfolio:
    @pytest.fixture
    def portfolio(self):
        return Portfolio(initial_capital=1_000_000.0)

    def test_initial_state(self, portfolio):
        assert portfolio.cash == 1_000_000.0
        assert portfolio.total_value == 1_000_000.0
        assert portfolio.market_value == 0.0
        assert len(portfolio.positions) == 0

    def test_apply_buy_trade(self, portfolio):
        trade = Trade(
            trade_id="t1", order_id="o1",
            code="000001.SZ", date=date(2024, 6, 14),
            side=OrderSide.BUY, shares=1000,
            price=10.0, amount=10000.0,
            commission=5.0, stamp_duty=0.0, slippage=10.0,
        )
        portfolio.apply_trade(trade)

        # 现金减少: 10000 + 5 + 0 + 10 = 10015
        assert portfolio.cash == pytest.approx(989_985.0)
        # 持仓增加
        pos = portfolio.positions.get("000001.SZ")
        assert pos is not None
        assert pos.shares == 1000
        assert pos.avg_cost == pytest.approx(10.015)  # (10000+5+10)/1000

    def test_apply_sell_trade(self, portfolio):
        # 先买入
        buy_trade = Trade(
            trade_id="t1", order_id="o1",
            code="000001.SZ", date=date(2024, 6, 10),
            side=OrderSide.BUY, shares=1000,
            price=10.0, amount=10000.0,
            commission=5.0, stamp_duty=0.0, slippage=10.0,
        )
        portfolio.apply_trade(buy_trade)

        # 再卖出500股
        sell_trade = Trade(
            trade_id="t2", order_id="o2",
            code="000001.SZ", date=date(2024, 6, 14),
            side=OrderSide.SELL, shares=500,
            price=12.0, amount=6000.0,
            commission=5.0, stamp_duty=6.0, slippage=6.0,
        )
        portfolio.apply_trade(sell_trade)

        # 现金变化: 买入-10015, 卖出+(6000-5-6-6)=+5983
        expected_cash = 1_000_000 - 10015 + 5983
        assert portfolio.cash == pytest.approx(expected_cash)

        # 剩余持仓
        pos = portfolio.positions.get("000001.SZ")
        assert pos.shares == 500

    def test_update_market_values(self, portfolio):
        # 先建仓
        buy = Trade(
            trade_id="t1", order_id="o1",
            code="000001.SZ", date=date(2024, 6, 10),
            side=OrderSide.BUY, shares=1000,
            price=10.0, amount=10000.0,
            commission=5.0, stamp_duty=0.0, slippage=10.0,
        )
        portfolio.apply_trade(buy)

        # 更新市值
        portfolio.update_market_values(
            {"000001.SZ": 12.5}, dt=date(2024, 6, 14)
        )
        pos = portfolio.positions["000001.SZ"]
        assert pos.market_value == pytest.approx(12500.0)
        assert pos.unrealized_pnl > 0

    def test_t_plus_one_lock(self, portfolio):
        """买入当天不可卖 (T+1)"""
        today = date(2024, 6, 14)
        buy = Trade(
            trade_id="t1", order_id="o1",
            code="000001.SZ", date=today,
            side=OrderSide.BUY, shares=1000,
            price=10.0, amount=10000.0,
            commission=5.0, stamp_duty=0.0, slippage=10.0,
        )
        portfolio.apply_trade(buy)

        # 当天不可卖
        sellable = portfolio.get_sellable_shares("000001.SZ", today)
        assert sellable == 0

        # 第二天可卖
        tomorrow = today + timedelta(days=1)
        sellable_tomorrow = portfolio.get_sellable_shares("000001.SZ", tomorrow)
        assert sellable_tomorrow == 1000

    def test_sell_more_than_owned(self, portfolio):
        """Reject an inconsistent fill without creating cash or changing shares."""
        buy = Trade(
            trade_id="t1", order_id="o1",
            code="000001.SZ", date=date(2024, 6, 10),
            side=OrderSide.BUY, shares=500,
            price=10.0, amount=5000.0, commission=5.0,
        )
        portfolio.apply_trade(buy)

        sell = Trade(
            trade_id="t2", order_id="o2",
            code="000001.SZ", date=date(2024, 6, 14),
            side=OrderSide.SELL, shares=1000,  # 想卖1000但只有500
            price=12.0, amount=12000.0, commission=5.0, stamp_duty=12.0, slippage=12.0,
        )
        cash_before = portfolio.cash
        with pytest.raises(ValueError, match="Insufficient shares"):
            portfolio.apply_trade(sell)
        assert portfolio.cash == cash_before
        assert portfolio.positions["000001.SZ"].shares == 500

    def test_daily_return(self, portfolio):
        """验证日收益率计算"""
        # 买入后更新市值
        buy = Trade(
            trade_id="t1", order_id="o1",
            code="000001.SZ", date=date(2024, 6, 10),
            side=OrderSide.BUY, shares=1000,
            price=10.0, amount=10000.0, commission=5.0,
        )
        portfolio.apply_trade(buy)
        portfolio.update_market_values(
            {"000001.SZ": 10.5}, dt=date(2024, 6, 10)
        )

        prev = portfolio.total_value
        portfolio.update_market_values(
            {"000001.SZ": 11.0}, dt=date(2024, 6, 11)
        )

        ret = portfolio.daily_return
        assert ret == pytest.approx((portfolio.total_value - prev) / prev, rel=0.01)

    def test_custom_initial_capital(self):
        p = Portfolio(initial_capital=100_000.0)
        assert p.cash == 100_000.0

    def test_buy_cannot_make_cash_negative(self):
        p = Portfolio(initial_capital=1_000.0)
        trade = Trade(
            trade_id="t1", order_id="o1", code="000001.SZ",
            date=date(2024, 6, 14), side=OrderSide.BUY, shares=100,
            price=10.0, amount=1000.0, commission=5.0,
        )
        with pytest.raises(ValueError, match="Insufficient cash"):
            p.apply_trade(trade)
        assert p.cash == 1_000.0
        assert p.positions == {}


def fill(side, shares, day):
    return Trade(trade_id="test", order_id="test", code="000001.SZ",
                 date=day, side=side, shares=shares, price=10., amount=shares * 10.)


def test_top_up_preserves_equity_and_previously_sellable_shares():
    portfolio = Portfolio(10000.)
    portfolio.apply_trade(fill(OrderSide.BUY, 500, date(2024, 6, 13)))
    portfolio.update_market_values({"000001.SZ": 10.}, date(2024, 6, 13))
    portfolio.apply_trade(fill(OrderSide.BUY, 100, date(2024, 6, 14)))
    assert portfolio.total_value == 10000.
    assert portfolio.get_sellable_shares("000001.SZ", date(2024, 6, 14)) == 500
    portfolio.apply_trade(fill(OrderSide.SELL, 500, date(2024, 6, 14)))
    assert portfolio.total_value == 10000.
    assert portfolio.positions["000001.SZ"].shares == 100
    assert portfolio.get_sellable_shares("000001.SZ", date(2024, 6, 14)) == 0
    assert portfolio.get_sellable_shares("000001.SZ", date(2024, 6, 17)) == 100


def test_same_day_multiple_buys_unlock_by_lot():
    portfolio = Portfolio(10000.)
    portfolio.apply_trade(fill(OrderSide.BUY, 200, date(2024, 6, 13)))
    portfolio.apply_trade(fill(OrderSide.BUY, 100, date(2024, 6, 14)))
    portfolio.apply_trade(fill(OrderSide.BUY, 100, date(2024, 6, 14)))
    assert portfolio.get_sellable_shares("000001.SZ", date(2024, 6, 14)) == 200
    assert portfolio.get_sellable_shares("000001.SZ", date(2024, 6, 17)) == 400


def test_sell_without_position_is_atomic():
    portfolio = Portfolio(10000.)
    with pytest.raises(ValueError, match="Insufficient shares"):
        portfolio.apply_trade(fill(OrderSide.SELL, 100, date(2024, 6, 14)))
    assert portfolio.cash == 10000.
    assert portfolio.positions == {}


def test_partial_sell_updates_value_before_next_mark():
    portfolio = Portfolio(10000.)
    portfolio.apply_trade(fill(OrderSide.BUY, 500, date(2024, 6, 13)))
    portfolio.apply_trade(fill(OrderSide.SELL, 200, date(2024, 6, 14)))
    assert portfolio.positions["000001.SZ"].market_value == 3000.
    assert portfolio.total_value == 10000.


def test_locked_sell_does_not_mutate_portfolio():
    portfolio = Portfolio(10000.)
    portfolio.apply_trade(fill(OrderSide.BUY, 100, date(2024, 6, 14)))
    with pytest.raises(ValueError, match="Insufficient sellable shares"):
        portfolio.apply_trade(fill(OrderSide.SELL, 100, date(2024, 6, 14)))
    assert portfolio.cash == 9000.
    assert portfolio.positions["000001.SZ"].shares == 100


def test_top_up_of_legacy_restored_position_retains_original_unlock():
    portfolio = Portfolio(5000.)
    portfolio._positions["000001.SZ"] = Position(
        code="000001.SZ", shares=500, avg_cost=10., market_value=5000.,
        unlock_date=date(2024, 6, 14),
    )
    portfolio.apply_trade(fill(OrderSide.BUY, 100, date(2024, 6, 14)))
    assert portfolio.get_sellable_shares("000001.SZ", date(2024, 6, 14)) == 500
    assert portfolio.get_sellable_shares("000001.SZ", date(2024, 6, 17)) == 600
