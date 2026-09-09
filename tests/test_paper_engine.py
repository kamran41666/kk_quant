from datetime import date
import json

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from quant_engine.backtest.portfolio import Portfolio
from quant_engine.backtest.types import Position
from server.models.database import Base
from server.models.schema import PaperPosition, PaperSnapshot
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


def _legacy_session_factory(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr("server.services.paper_engine.SessionLocal", factory)
    return factory


def test_legacy_snapshot_restore_preserves_t_plus_one_lots_and_is_idempotent(monkeypatch):
    factory = _legacy_session_factory(monkeypatch)
    day = date(2024, 6, 14)
    monkeypatch.setattr("server.services.paper_engine._trade_date", lambda: day)
    account = object.__new__(SimulationAccount)
    account._initial_capital = 100_000.0
    account._portfolio = Portfolio(initial_capital=100_000.0)
    account._portfolio._cash = 98_000.0
    account._portfolio._positions["000001.SZ"] = Position(
        code="000001.SZ",
        shares=200,
        avg_cost=10.0,
        market_value=2_000.0,
        unlock_date=date(2024, 6, 15),
    )
    account._portfolio._locked_lots["000001.SZ"] = [
        (date(2024, 6, 14), 100),
        (date(2024, 6, 15), 100),
    ]

    with factory() as db:
        account._save_snapshot(day, db)
        account._save_snapshot(day, db)
        db.commit()
        assert db.query(PaperSnapshot).count() == 1
        assert db.query(PaperPosition).count() == 1
        saved = db.query(PaperPosition).one()
        assert json.loads(saved.locked_lots) == [
            {"shares": 100, "unlock_date": "2024-06-15"}
        ]

    restored = object.__new__(SimulationAccount)
    restored._initial_capital = 100_000.0
    restored._portfolio = Portfolio(initial_capital=100_000.0)
    restored._load_state()
    assert restored._portfolio.get_sellable_shares("000001.SZ", day) == 100
    assert restored._portfolio.get_sellable_shares(
        "000001.SZ", date(2024, 6, 15)
    ) == 200


def test_legacy_snapshot_without_lot_metadata_stays_locked_on_same_day(monkeypatch):
    factory = _legacy_session_factory(monkeypatch)
    day = date(2024, 6, 14)
    monkeypatch.setattr("server.services.paper_engine._trade_date", lambda: day)
    with factory() as db:
        db.add(PaperSnapshot(
            date=day.isoformat(), cash=99_000.0, market_value=1_000.0,
            total_value=100_000.0, daily_return=0.0, n_positions=1,
        ))
        db.add(PaperPosition(
            snapshot_date=day.isoformat(), code="000001.SZ", shares=100,
            avg_cost=10.0, market_value=1_000.0, weight=1.0,
            unlock_date=None, locked_lots="[]",
        ))
        db.commit()

    restored = object.__new__(SimulationAccount)
    restored._initial_capital = 100_000.0
    restored._portfolio = Portfolio(initial_capital=100_000.0)
    restored._load_state()
    assert restored._portfolio.get_sellable_shares("000001.SZ", day) == 0
    assert restored._portfolio.get_sellable_shares(
        "000001.SZ", date(2024, 6, 15)
    ) == 100


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
