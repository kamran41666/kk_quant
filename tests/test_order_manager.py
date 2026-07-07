import pytest
from datetime import date
from quant_engine.backtest.types import OrderSide, OrderStatus
from quant_engine.backtest.cost_model import CostModel
from quant_engine.backtest.order_manager import OrderManager


class TestOrderManager:
    @pytest.fixture
    def om(self):
        model = CostModel()
        return OrderManager(model)

    def test_submit_buy_order(self, om):
        order = om.submit("000001.SZ", OrderSide.BUY, 1500, date(2024, 6, 14))
        assert order.code == "000001.SZ"
        assert order.side == OrderSide.BUY
        assert order.status == OrderStatus.PENDING
        assert order.shares == 1500  # 已是100的倍数

    def test_submit_buy_rounds_down(self, om):
        """买入150股 → 向下取整到100股"""
        order = om.submit("000001.SZ", OrderSide.BUY, 150, date(2024, 6, 14))
        assert order.shares == 100

    def test_submit_sell_no_rounding(self, om):
        """卖出不自动取整"""
        order = om.submit("000001.SZ", OrderSide.SELL, 150, date(2024, 6, 14))
        assert order.shares == 150

    def test_round_lot_buy(self):
        assert OrderManager.round_lot(250) == 200
        assert OrderManager.round_lot(100) == 100
        assert OrderManager.round_lot(99) == 0

    def test_round_lot_up(self):
        assert OrderManager.round_lot(250, round_up=True) == 300
        assert OrderManager.round_lot(1, round_up=True) == 100

    def test_cancel_order(self, om):
        order = om.submit("000001.SZ", OrderSide.BUY, 1000, date(2024, 6, 14))
        assert om.cancel(order.order_id) is True
        assert order.status == OrderStatus.CANCELLED

    def test_cancel_nonexistent(self, om):
        assert om.cancel("nonexistent") is False

    def test_get_pending_orders(self, om):
        om.submit("000001.SZ", OrderSide.BUY, 1000, date(2024, 6, 14))
        om.submit("000002.SZ", OrderSide.SELL, 500, date(2024, 6, 14))
        pending = om.get_pending_orders()
        assert len(pending) == 2

    def test_get_pending_excludes_cancelled(self, om):
        o = om.submit("000001.SZ", OrderSide.BUY, 1000, date(2024, 6, 14))
        om.cancel(o.order_id)
        pending = om.get_pending_orders()
        assert len(pending) == 0

    def test_get_orders_by_code(self, om):
        om.submit("000001.SZ", OrderSide.BUY, 1000, date(2024, 6, 14))
        om.submit("000002.SZ", OrderSide.BUY, 500, date(2024, 6, 14))
        om.submit("000001.SZ", OrderSide.SELL, 500, date(2024, 6, 15))

        orders_000001 = om.get_orders("000001.SZ")
        assert len(orders_000001) == 2

    def test_all_orders(self, om):
        om.submit("000001.SZ", OrderSide.BUY, 1000, date(2024, 6, 14))
        assert len(om.all_orders) == 1

    def test_cost_model_access(self, om):
        assert isinstance(om.cost_model, CostModel)

    def test_submit_with_price_limit(self, om):
        order = om.submit(
            "000001.SZ", OrderSide.BUY, 1000, date(2024, 6, 14),
            price_limit=12.5
        )
        assert order.price_limit == 12.5
