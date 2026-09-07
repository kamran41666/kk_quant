from datetime import date

import pandas as pd

from quant_engine.backtest.portfolio import Portfolio
from quant_engine.backtest.types import Position
from server.services.paper_engine import SimulationAccount


class _TradingDay:
    @staticmethod
    def is_trading_day(_dt):
        return True


class _UnverifiedTradingDay:
    @staticmethod
    def ensure_coverage(_start, _end):
        return {"source": "fallback:business-days", "verified": False, "complete": False}

    @staticmethod
    def is_trading_day(_dt):
        raise AssertionError("unverified calendar must be blocked before classification")


class _NoSnapshotAPI:
    def index_components(self, _index_code, _dt):
        raise RuntimeError("snapshot missing")


def test_paper_run_blocks_without_point_in_time_stock_pool():
    account = object.__new__(SimulationAccount)
    account._calendar = _TradingDay()
    account._portfolio = Portfolio(initial_capital=100_000)
    account._api = _NoSnapshotAPI()

    result = account.run_daily(date(2026, 9, 4))

    assert result["status"] == "blocked"
    assert result["code"] == "PIT_STOCK_POOL_UNAVAILABLE"
    assert "import a dated snapshot" in result["reason"]


def test_paper_run_fails_closed_when_calendar_is_unverified():
    account = object.__new__(SimulationAccount)
    account._calendar = _UnverifiedTradingDay()
    result = account.run_daily(date(2026, 9, 4))
    assert result["status"] == "blocked"
    assert result["code"] == "TRADING_CALENDAR_UNAVAILABLE"


class _PartialPriceAPI:
    def daily(self, **_kwargs):
        return pd.DataFrame(
            {"close": [12.0]},
            index=pd.MultiIndex.from_tuples(
                [("600000.SH", date(2026, 9, 4))],
                names=["code", "date"],
            ),
        )


def test_paper_run_blocks_when_a_held_security_has_no_valid_close():
    account = object.__new__(SimulationAccount)
    account._calendar = _TradingDay()
    account._portfolio = Portfolio(initial_capital=100_000)
    account._portfolio._positions["000001.SZ"] = Position(
        code="000001.SZ", shares=100, avg_cost=10.0,
        market_value=1_000.0, unlock_date=date(2026, 9, 5),
    )
    account._api = _PartialPriceAPI()

    result = account.run_daily(date(2026, 9, 4))

    assert result["status"] == "blocked"
    assert result["code"] == "PAPER_POSITION_PRICES_INCOMPLETE"
    assert result["missing_codes"] == ["000001.SZ"]


class _PartialPoolPriceAPI:
    def index_components(self, _index_code, _dt):
        return ["000001.SZ", "600000.SH"]

    def daily(self, **_kwargs):
        return pd.DataFrame(
            {"close": [12.0]},
            index=pd.MultiIndex.from_tuples(
                [("000001.SZ", date(2026, 9, 4))],
                names=["code", "date"],
            ),
        )


def test_first_paper_run_blocks_when_stock_pool_prices_are_partial():
    account = object.__new__(SimulationAccount)
    account._calendar = _TradingDay()
    account._portfolio = Portfolio(initial_capital=100_000)
    account._api = _PartialPoolPriceAPI()

    result = account.run_daily(date(2026, 9, 4))

    assert result["status"] == "blocked"
    assert result["code"] == "PAPER_PRICE_DATA_INCOMPLETE"
    assert result["missing_codes"] == ["600000.SH"]


class _FailingPriceAPI:
    def index_components(self, _index_code, _dt):
        return ["000001.SZ"]

    def daily(self, **_kwargs):
        raise RuntimeError("local price store unavailable")


def test_paper_run_blocks_when_price_read_raises():
    account = object.__new__(SimulationAccount)
    account._calendar = _TradingDay()
    account._portfolio = Portfolio(initial_capital=100_000)
    account._api = _FailingPriceAPI()

    result = account.run_daily(date(2026, 9, 4))

    assert result["status"] == "blocked"
    assert result["code"] == "PAPER_PRICE_DATA_UNAVAILABLE"
    assert "local price store unavailable" in result["reason"]
