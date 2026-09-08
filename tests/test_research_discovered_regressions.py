from datetime import date
from types import SimpleNamespace

import pandas as pd

from quant_engine.backtest.cost_model import CostModel
from quant_engine.backtest.engine import BacktestEngine
from quant_engine.backtest.order_manager import OrderManager
from quant_engine.backtest.portfolio import Portfolio
from quant_engine.backtest.strategy import Strategy
from quant_engine.backtest.types import OrderSide, Trade
from quant_engine.data.fetcher.pipeline import DataPipeline
from quant_engine.data.store import MetaDB, PriceStore


def test_unchanged_full_weight_does_not_sell_on_overnight_gap():
    portfolio = Portfolio(initial_capital=10_000)
    portfolio.apply_trade(Trade(trade_id="t", order_id="o", code="600000.SH",
                                date=date(2024, 1, 2), side=OrderSide.BUY,
                                shares=1000, price=10, amount=10_000, commission=0))
    portfolio.update_market_values({"600000.SH": 10}, date(2024, 1, 2))
    manager = OrderManager(CostModel(commission_rate=0, min_commission=0, slippage_rate=0, stamp_duty_rate=0))
    handler = SimpleNamespace(get_price=lambda code, field: 11.)
    orders = BacktestEngine(Strategy)._signals_to_orders(
        {"600000.SH": 1.0}, portfolio, manager, handler, date(2024, 1, 3))
    assert orders == []
    assert portfolio.total_value == 10_000  # sizing must not advance EOD returns
    assert portfolio.daily_return == 0


def test_pipeline_keeps_source_execution_constraints(tmp_path):
    frame = pd.DataFrame([{"code": "600000.SH", "date": "2024-01-02", "open": 10,
                           "high": 10, "low": 10, "close": 10, "volume": 100,
                           "up_limit": 10, "down_limit": 9, "is_suspended": True}])
    source = SimpleNamespace(source_name="test:historical", fetch_daily=lambda *args, **kwargs: frame)
    store = PriceStore(str(tmp_path))
    pipeline = DataPipeline(source, store, MetaDB(str(tmp_path / "meta.db")))
    pipeline.sync_daily(["600000.SH"], date(2024, 1, 2), date(2024, 1, 2))
    actual = store.read_range(["600000.SH"], date(2024, 1, 2), date(2024, 1, 2),
                              ["up_limit", "down_limit", "is_suspended"]).iloc[0]
    assert actual.up_limit == 10
    assert actual.down_limit == 9
    assert bool(actual.is_suspended)
