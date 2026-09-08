import pytest
from datetime import date
from quant_engine.data.calendar import TradingCalendar
from quant_engine.backtest.context import StrategyContext
from quant_engine.backtest.strategy import Strategy
from quant_engine.backtest.types import Trade, OrderSide, AccountState, Position


# 具体策略子类用于测试（以 _ 开头避免被 pytest 收集为测试类）
class _TestStrategy(Strategy):
    def initialize(self):
        self.signal_date = None
        self.rebalance_log = []
        self.fill_log = []
        self.teardown_called = False

    def generate_signals(self, dt: date) -> dict[str, float]:
        self.signal_date = dt
        return {"000001.SZ": 0.5, "000002.SZ": 0.5}

    def on_rebalance(self, dt, old_weights, new_weights):
        self.rebalance_log.append((dt, old_weights, new_weights))

    def on_order_filled(self, trade: Trade):
        self.fill_log.append(trade)

    def teardown(self):
        self.teardown_called = True


class TestStrategyContext:
    @pytest.fixture
    def context(self):
        cal = TradingCalendar()
        return StrategyContext(cal)

    def test_initial_state(self, context):
        assert context.current_date is None
        assert context.portfolio is None

    def test_set_date(self, context):
        d = date(2024, 6, 14)
        context.set_date(d)
        assert context.current_date == d

    def test_log(self, context):
        context.set_date(date(2024, 6, 14))
        context.log("test message")
        assert len(context.get_logs()) == 1
        assert "test message" in context.get_logs()[0]

    def test_clear_logs(self, context):
        context.log("msg1")
        context.log("msg2")
        context.clear_logs()
        assert len(context.get_logs()) == 0

    def test_calendar_access(self, context):
        assert isinstance(context.calendar, TradingCalendar)

    def test_portfolio_set_and_get(self, context):
        pos = Position(
            code="000001.SZ", shares=1000,
            avg_cost=10.0, market_value=10500.0,
            unlock_date=date(2024, 6, 10)
        )
        acct = AccountState(cash=50000.0, positions={"000001.SZ": pos})
        context.set_portfolio(acct)
        assert context.portfolio is not None
        assert context.portfolio.cash == 50000.0

    def test_get_factor_requires_bound_data_portal(self, context):
        with pytest.raises(RuntimeError, match="not bound"):
            context.get_factor("momentum", date(2024, 6, 14))


class TestStrategyBase:
    @pytest.fixture
    def strategy(self):
        cal = TradingCalendar()
        ctx = StrategyContext(cal)
        s = _TestStrategy(ctx)
        s.initialize()
        return s

    def test_abstract_cannot_instantiate_directly(self):
        """Strategy 不能直接实例化"""
        with pytest.raises(TypeError):
            Strategy(None)  # type: ignore

    def test_concrete_strategy_has_context(self, strategy):
        assert strategy.ctx is not None

    def test_initialize_called(self):
        """初始化后 signal_date 应为 None（generate_signals 尚未调用）"""
        cal = TradingCalendar()
        ctx = StrategyContext(cal)
        s = _TestStrategy(ctx)
        s.initialize()
        assert s.signal_date is None

    def test_generate_signals(self, strategy):
        signals = strategy.generate_signals(date(2024, 6, 14))
        assert len(signals) == 2
        assert signals["000001.SZ"] == 0.5
        assert signals["000002.SZ"] == 0.5

    def test_on_rebalance(self, strategy):
        old = {"000001.SZ": 1.0}
        new = {"000001.SZ": 0.5, "000002.SZ": 0.5}
        strategy.on_rebalance(date(2024, 6, 14), old, new)
        assert len(strategy.rebalance_log) == 1

    def test_on_order_filled(self, strategy):
        trade = Trade(
            trade_id="t1", order_id="o1",
            code="000001.SZ", date=date(2024, 6, 14),
            side=OrderSide.BUY, shares=1000,
            price=10.0, amount=10000.0,
        )
        strategy.on_order_filled(trade)
        assert len(strategy.fill_log) == 1
        assert strategy.fill_log[0].code == "000001.SZ"

    def test_teardown(self, strategy):
        strategy.teardown()
        assert strategy.teardown_called

    def test_use_factor(self, strategy):
        strategy.use_factor("momentum_20")
        strategy.use_factor("roe_ttm")
        assert "momentum_20" in strategy._registered_factors
        assert "roe_ttm" in strategy._registered_factors

    def test_log_delegates_to_context(self, strategy):
        strategy.ctx.set_date(date(2024, 6, 14))
        strategy.log("hello")
        assert "hello" in strategy.ctx.get_logs()[-1]
