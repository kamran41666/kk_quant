import pytest
from datetime import date
from unittest.mock import Mock
from quant_engine.backtest.types import Order, OrderSide, Trade
from quant_engine.backtest.cost_model import CostModel
from quant_engine.backtest.matcher import Matcher


class TestMatcherVWAP:
    def test_est_vwap(self):
        vwap = Matcher.est_vwap(10.0, 10.5, 9.8, 10.0)
        # (10.0 + 10.5 + 9.8 + 2*10.0) / 5 = 50.3 / 5 = 10.06
        assert vwap == pytest.approx(10.06, rel=0.001)

    def test_est_vwap_flat_day(self):
        vwap = Matcher.est_vwap(10.0, 10.0, 10.0, 10.0)
        assert vwap == 10.0


class TestMatcher:
    @pytest.fixture
    def matcher(self):
        return Matcher(CostModel(slippage_rate=0.0))  # 无滑点方便测试

    @pytest.fixture
    def data_handler(self):
        dh = Mock()
        dh.current_date = date(2024, 6, 14)
        # 默认不涨停不停牌
        dh.is_suspended.return_value = False
        dh.get_price.return_value = 10.0
        dh.get_limit_prices.return_value = (8.0, 12.0)  # 跌停8, 涨停12
        return dh

    def test_normal_buy_fills(self, matcher, data_handler):
        order = Order(
            order_id="o1", code="000001.SZ",
            date=date(2024, 6, 14), side=OrderSide.BUY,
            shares=1000,
        )
        trade = matcher.match(order, data_handler)
        assert trade is not None
        assert trade.shares == 1000
        assert trade.side == OrderSide.BUY
        assert trade.price == 10.0  # VWAP, 无滑点

    def test_normal_sell_fills(self, matcher, data_handler):
        order = Order(
            order_id="o2", code="000002.SZ",
            date=date(2024, 6, 14), side=OrderSide.SELL,
            shares=500,
        )
        trade = matcher.match(order, data_handler)
        assert trade is not None
        assert trade.side == OrderSide.SELL

    def test_limit_up_no_buy(self, matcher, data_handler):
        """涨停时买入无法成交"""
        data_handler.get_limit_prices.return_value = (8.0, 10.0)  # 涨停=10
        data_handler.get_price.return_value = 10.0  # 当前价=涨停价

        order = Order(
            order_id="o3", code="000001.SZ",
            date=date(2024, 6, 14), side=OrderSide.BUY,
            shares=1000,
        )
        trade = matcher.match(order, data_handler)
        assert trade is None

    def test_limit_down_no_sell(self, matcher, data_handler):
        """跌停时卖出无法成交"""
        data_handler.get_limit_prices.return_value = (10.0, 12.0)  # 跌停=10
        data_handler.get_price.return_value = 10.0  # 当前价=跌停价

        order = Order(
            order_id="o4", code="000001.SZ",
            date=date(2024, 6, 14), side=OrderSide.SELL,
            shares=1000,
        )
        trade = matcher.match(order, data_handler)
        assert trade is None

    def test_suspended_no_fill(self, matcher, data_handler):
        """停牌无法成交"""
        data_handler.is_suspended.return_value = True

        order = Order(
            order_id="o5", code="000001.SZ",
            date=date(2024, 6, 14), side=OrderSide.BUY,
            shares=1000,
        )
        trade = matcher.match(order, data_handler)
        assert trade is None

    def test_slippage_applied(self, data_handler):
        """滑点影响成交价，但不重复计算到 slippage 字段"""
        matcher_with_slip = Matcher(CostModel(slippage_rate=0.001))
        data_handler.is_suspended.return_value = False
        data_handler.get_price.return_value = 10.0

        order = Order(
            order_id="o6", code="000001.SZ",
            date=date(2024, 6, 14), side=OrderSide.BUY,
            shares=1000,
        )
        trade = matcher_with_slip.match(order, data_handler)
        # 买入: price > VWAP (滑点反映在价格中)
        assert trade.price > 10.0
        # 滑点已在价格中体现，slippage 字段应为 0 以避免重复扣减
        assert trade.slippage == 0.0

    def test_trade_includes_costs(self, matcher, data_handler):
        order = Order(
            order_id="o7", code="000001.SZ",
            date=date(2024, 6, 14), side=OrderSide.BUY,
            shares=1000,
        )
        trade = matcher.match(order, data_handler)
        assert trade.commission > 0  # 最低5元佣金
        assert trade.slippage == 0  # 无滑点设定

    def test_partial_fill_when_order_already_partial(self, matcher, data_handler):
        """部分成交订单继续撮合"""
        order = Order(
            order_id="o8", code="000001.SZ",
            date=date(2024, 6, 14), side=OrderSide.BUY,
            shares=1000,
        )
        order.fill_shares = 400  # 已成交400
        trade = matcher.match(order, data_handler)
        assert trade is not None
        assert trade.shares == 600  # 撮合剩余的600
