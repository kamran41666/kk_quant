import pytest
from datetime import date, timedelta
from quant_engine.backtest.types import (
    Order, OrderSide, OrderStatus, Trade, Position, AccountState
)


class TestOrderTypes:
    def test_create_buy_order(self):
        o = Order(
            order_id="o1", code="000001.SZ",
            date=date(2024, 6, 14), side=OrderSide.BUY,
            shares=1000, price_limit=None
        )
        assert o.side == OrderSide.BUY
        assert o.status == OrderStatus.PENDING
        assert o.fill_shares == 0
        assert o.reject_reason is None

    def test_create_sell_order(self):
        o = Order(
            order_id="o2", code="000002.SZ",
            date=date(2024, 6, 14), side=OrderSide.SELL,
            shares=500, price_limit=15.0
        )
        assert o.side == OrderSide.SELL
        assert o.price_limit == 15.0

    def test_order_fill_partial(self):
        o = Order(
            order_id="o3", code="000001.SZ",
            date=date(2024, 6, 14), side=OrderSide.BUY,
            shares=1000
        )
        o.fill(600)
        assert o.status == OrderStatus.PARTIAL
        assert o.fill_shares == 600

    def test_order_fill_full(self):
        o = Order(
            order_id="o4", code="000001.SZ",
            date=date(2024, 6, 14), side=OrderSide.BUY,
            shares=1000
        )
        o.fill(1000)
        assert o.status == OrderStatus.FILLED

    def test_order_reject(self):
        o = Order(
            order_id="o5", code="000001.SZ",
            date=date(2024, 6, 14), side=OrderSide.BUY,
            shares=1000
        )
        o.reject("limit_up")
        assert o.status == OrderStatus.REJECTED
        assert o.reject_reason == "limit_up"

    def test_trade_from_order(self):
        o = Order(
            order_id="o6", code="000001.SZ",
            date=date(2024, 6, 14), side=OrderSide.BUY,
            shares=1000
        )
        o.fill(1000)
        t = Trade.from_order(o, price=10.50, commission=2.63,
                             stamp_duty=0, slippage=1.05)
        assert t.code == "000001.SZ"
        assert t.price == 10.50
        assert t.amount == 10500.0
        assert t.commission == 2.63

    def test_position_unlock(self):
        """T+1: 今日买入 → 明日才可卖出"""
        today = date(2024, 6, 14)
        pos = Position(
            code="000001.SZ", shares=1000,
            avg_cost=10.0, market_value=10500.0,
            unlock_date=today + timedelta(days=1)
        )
        assert pos.is_locked(today)
        assert not pos.is_locked(today + timedelta(days=1))

    def test_position_sellable(self):
        """已有持仓（非今日买入）可以直接卖"""
        today = date(2024, 6, 14)
        pos = Position(
            code="000001.SZ", shares=1000,
            avg_cost=10.0, market_value=10500.0,
            unlock_date=date(2024, 6, 10)
        )
        assert not pos.is_locked(today)

    def test_account_state_total_value(self):
        pos = Position(
            code="000001.SZ", shares=1000,
            avg_cost=10.0, market_value=10500.0,
            unlock_date=date(2024, 6, 10)
        )
        acct = AccountState(
            cash=50000.0,
            positions={"000001.SZ": pos}
        )
        assert acct.total_value == pytest.approx(60500.0)
        assert acct.market_value == pytest.approx(10500.0)
