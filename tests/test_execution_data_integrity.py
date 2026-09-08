"""Exercise execution constraints through the real Parquet projection path."""
from datetime import date

import pandas as pd
import pytest

from quant_engine.backtest.cost_model import CostModel
from quant_engine.backtest.data_handler import DataHandler
from quant_engine.backtest.matcher import Matcher
from quant_engine.backtest.types import Order, OrderSide
from quant_engine.data.adjust import AdjustHandler
from quant_engine.data.api import DataAPI
from quant_engine.data.store import PriceStore


DAY = date(2024, 6, 14)
CODE = "000001.SZ"


def stored_handler(tmp_path, monkeypatch, extra=None, factor=1.0):
    store = PriceStore(str(tmp_path))
    row = dict(date=DAY, open=10., high=10., low=10., close=10.,
               volume=1000., amount=10000., turnover_rate=0.1)
    row.update(extra or {})
    store.write(CODE, pd.DataFrame([row]))
    api = object.__new__(DataAPI)
    api._price_store = store
    api._adjust = AdjustHandler(None)
    api._adjust._factor_cache[CODE] = pd.DataFrame({"date": [DAY], "factor": [factor]})
    # Isolate unrelated calendar/coverage checks; pricing, projection and
    # adjustment below all use the production storage and DataAPI methods.
    monkeypatch.setattr(api, "daily_coverage", lambda **kwargs: None)
    monkeypatch.setattr("quant_engine.backtest.data_handler.DataAPI", lambda: api)
    handler = DataHandler([CODE], DAY, DAY)
    assert handler.load_error is None
    handler.push_day(DAY)
    return handler


def match(handler, side=OrderSide.BUY):
    order = Order(order_id="test", code=CODE, date=DAY, side=side, shares=100)
    return Matcher(CostModel(slippage_rate=0.)).match(order, handler)


@pytest.mark.parametrize("extra,side", [
    ({"is_suspended": True}, OrderSide.BUY),
    ({"up_limit": 10.}, OrderSide.BUY),
    ({"down_limit": 10.}, OrderSide.SELL),
    ({"volume": 0.}, OrderSide.BUY),
])
def test_stored_execution_constraints_block_fills(tmp_path, monkeypatch, extra, side):
    handler = stored_handler(tmp_path, monkeypatch, extra)
    assert match(handler, side) is None


def test_optional_rule_columns_remain_optional(tmp_path, monkeypatch):
    handler = stored_handler(tmp_path, monkeypatch)
    assert not handler.is_suspended(CODE)
    assert handler.get_limit_prices(CODE) == (0., float("inf"))
    assert match(handler) is not None


def test_limit_prices_use_same_adjustment_as_execution(tmp_path, monkeypatch):
    handler = stored_handler(tmp_path, monkeypatch, {"up_limit": 11., "down_limit": 9.}, factor=2.)
    assert handler.get_price(CODE, "open") == 20.
    assert handler.get_limit_prices(CODE) == (18., 22.)
    assert match(handler) is not None


@pytest.mark.parametrize("flag,can_trade", [
    (False, True), (0, True), ("False", True), ("0", True),
    (True, False), (1, False), ("True", False), ("1", False),
    ("unknown", False), ("", False), (2, False),
])
def test_stored_suspension_flags_are_parsed_explicitly(tmp_path, monkeypatch, flag, can_trade):
    handler = stored_handler(tmp_path, monkeypatch, {"is_suspended": flag})
    assert (match(handler) is not None) is can_trade
