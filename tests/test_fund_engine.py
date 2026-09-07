import json
from datetime import date

import pandas as pd
import pytest

from strategies.fund_nav_momentum import FundNavMomentumStrategy
from quant_engine.backtest.fund_engine import FundNavBacktestEngine
from quant_engine.backtest.strategy import Strategy


def test_fund_nav_engine_uses_next_valid_nav_and_writes_audit_files(tmp_path):
    rows = {
        "110022": [
                {"date": "2024-01-01", "nav": 1.00}, {"date": "2024-01-02", "nav": 1.01},
                {"date": "2024-01-03", "nav": 1.05}, {"date": "2024-01-04", "nav": 1.08},
                {"date": "2024-01-05", "nav": 1.10},
        ],
        "161725": [
                {"date": "2024-01-01", "nav": 1.00}, {"date": "2024-01-02", "nav": 0.99},
                {"date": "2024-01-03", "nav": 0.98}, {"date": "2024-01-04", "nav": 0.97},
                {"date": "2024-01-05", "nav": 0.96},
        ],
    }
    result_dir = FundNavBacktestEngine(FundNavMomentumStrategy, lookback=1, top_n=1).run(
            rows=rows, start=date(2024, 1, 1), end=date(2024, 1, 5), initial_capital=1_000,
        output_dir=str(tmp_path), rebalance_frequency="daily",
    )
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert result_dir == str(tmp_path.resolve())
    assert summary["execution_lag"] == "next_valid_nav"
    assert summary["execution_model"] == "next_valid_nav-v1"
    assert summary["n_trades"] >= 1
    assert (tmp_path / "daily_portfolio.parquet").exists()
    assert (tmp_path / "trades.parquet").exists()


def test_fund_rotation_executes_all_sells_before_buys(tmp_path):
    class RotationStrategy(Strategy):
        def initialize(self):
            pass

        def generate_signals(self, dt):
            return {"000002": 1.0} if dt <= date(2024, 1, 2) else {"000001": 1.0}

    rows = {
        "000001": [{"date": f"2024-01-0{day}", "nav": 1.0} for day in range(2, 5)],
        "000002": [{"date": f"2024-01-0{day}", "nav": 1.0} for day in range(2, 5)],
    }
    FundNavBacktestEngine(RotationStrategy).run(
        rows=rows, start=date(2024, 1, 2), end=date(2024, 1, 4), initial_capital=1_000,
        output_dir=str(tmp_path), rebalance_frequency="daily",
    )
    trades = pd.read_parquet(tmp_path / "trades.parquet")
    day_four = trades[trades["date"].astype(str) == "2024-01-04"]
    assert day_four["side"].tolist() == ["sell", "buy"]
    assert day_four["status"].tolist() == ["filled", "filled"]
    assert day_four["reason"].isna().all()


def test_fund_engine_rejects_non_finite_signal_weights(tmp_path):
    class InvalidSignalStrategy(Strategy):
        def initialize(self):
            pass

        def generate_signals(self, dt):
            return {"000001": float("nan")}

    rows = {"000001": [
        {"date": "2024-01-01", "nav": 1.0},
        {"date": "2024-01-02", "nav": 1.0},
    ]}
    with pytest.raises(ValueError, match="invalid target weights"):
        FundNavBacktestEngine(InvalidSignalStrategy).run(
            rows=rows, start=date(2024, 1, 1), end=date(2024, 1, 2),
            initial_capital=1000, output_dir=str(tmp_path), rebalance_frequency="daily",
        )


def test_fund_engine_can_gate_nav_rows_to_supplied_calendar():
    class CalendarStub:
        def get_trading_days(self, start, end):
            return [date(2024, 1, 2)]

    rows = {
        "000001": [
            {"date": "2024-01-01", "nav": 1.0},
            {"date": "2024-01-02", "nav": 1.1},
        ]
    }
    days, navs = FundNavBacktestEngine._rows_by_date(
        rows, date(2024, 1, 1), date(2024, 1, 2), CalendarStub()
    )
    assert days == [date(2024, 1, 2)]
    assert navs["000001"]["2024-01-01"] == 1.0


def test_fund_engine_direct_call_requires_verified_calendar(monkeypatch, tmp_path):
    class UnverifiedCalendar:
        def __init__(self, start_year, end_year):
            pass

        def ensure_coverage(self, start, end):
            return {
                "calendar_version": "trading-calendar-v1",
                "source": "fallback:business-days",
                "content_hash": "a" * 64,
                "coverage_start": start.isoformat(),
                "coverage_end": end.isoformat(),
                "verified": False,
                "complete": False,
            }

    monkeypatch.setattr("quant_engine.backtest.fund_engine.TradingCalendar", UnverifiedCalendar)
    with pytest.raises(ValueError, match="trading_calendar_coverage_insufficient"):
        FundNavBacktestEngine(FundNavMomentumStrategy).run(
            rows={"000001": [{"date": "2024-01-02", "nav": 1.0}]},
            start=date(2024, 1, 2), end=date(2024, 1, 2),
            initial_capital=1000, output_dir=str(tmp_path),
        )


def test_fund_engine_default_calendar_filters_non_trading_nav_rows(tmp_path):
    class NoopStrategy(Strategy):
        def initialize(self):
            pass

        def generate_signals(self, dt):
            return {}

    rows = {
        "000001": [
            {"date": "2024-01-01", "nav": 1.0},
            {"date": "2024-01-02", "nav": 1.1},
        ]
    }
    FundNavBacktestEngine(NoopStrategy).run(
        rows=rows,
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        initial_capital=1000,
        output_dir=str(tmp_path),
    )
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    portfolio = pd.read_parquet(tmp_path / "daily_portfolio.parquet")
    assert summary["start_date"] == "2024-01-02"
    assert summary["end_date"] == "2024-01-02"
    assert summary["n_trading_days"] == 1
    assert portfolio["date"].astype(str).tolist() == ["2024-01-02"]


def test_fund_strategy_history_uses_canonical_trading_rows(tmp_path):
    seen = []

    class HistoryStrategy(Strategy):
        def initialize(self):
            pass

        def generate_signals(self, dt):
            seen.append([str(row["date"]) for row in self._fund_history["000001"]])
            return {}

    FundNavBacktestEngine(HistoryStrategy).run(
        rows={"000001": [
            {"date": "2024-01-01", "nav": 1.0},
            {"date": "2024-01-02", "nav": 1.1},
        ]},
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        initial_capital=1000,
        output_dir=str(tmp_path),
    )
    assert seen == [["2024-01-02"]]


def test_fund_engine_records_explicit_paper_fee_rate(tmp_path):
    class HalfWeightStrategy(Strategy):
        def initialize(self):
            pass

        def generate_signals(self, dt):
            return {"000001": 0.5}

    rows = {"000001": [
        {"date": "2024-01-01", "nav": 1.0},
        {"date": "2024-01-02", "nav": 1.0},
        {"date": "2024-01-03", "nav": 1.0},
    ]}
    FundNavBacktestEngine(HalfWeightStrategy, paper_fee_rate=0.01).run(
        rows=rows, start=date(2024, 1, 1), end=date(2024, 1, 3),
        initial_capital=1000, output_dir=str(tmp_path),
    )
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    trades = pd.read_parquet(tmp_path / "trades.parquet")
    assert summary["fee_model"] == "fund_nav_configurable_rate-v1"
    assert summary["fee_rate"] == 0.01
    assert float(trades.iloc[0]["fee"]) == 5.0
    assert summary["final_value"] == 995.0


def test_fund_engine_preserves_strategy_fee_rate_parameter(tmp_path):
    received = {}

    class StrategyWithFeeParameter(Strategy):
        def initialize(self):
            received["fee_rate"] = self._strategy_kwargs["fee_rate"]

        def generate_signals(self, dt):
            return {"000001": 0.5}

    rows = {"000001": [
        {"date": "2024-01-01", "nav": 1.0},
        {"date": "2024-01-02", "nav": 1.0},
    ]}
    FundNavBacktestEngine(
        StrategyWithFeeParameter, paper_fee_rate=0.01, fee_rate=0.02,
    ).run(
        rows=rows, start=date(2024, 1, 1), end=date(2024, 1, 2),
        initial_capital=1000, output_dir=str(tmp_path),
    )
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert received["fee_rate"] == 0.02
    assert summary["fee_rate"] == 0.01


def test_fund_engine_cash_limited_buy_is_audited_as_partial(tmp_path):
    class FullWeightStrategy(Strategy):
        def initialize(self):
            pass

        def generate_signals(self, dt):
            return {"000001": 1.0}

    rows = {"000001": [
        {"date": "2024-01-01", "nav": 1.0},
        {"date": "2024-01-02", "nav": 1.0},
        {"date": "2024-01-03", "nav": 1.0},
    ]}
    FundNavBacktestEngine(FullWeightStrategy, paper_fee_rate=0.01).run(
        rows=rows, start=date(2024, 1, 1), end=date(2024, 1, 3),
        initial_capital=1000, output_dir=str(tmp_path),
    )
    trades = pd.read_parquet(tmp_path / "trades.parquet")
    assert trades.iloc[0]["status"] == "partial"
    assert trades.iloc[0]["reason"] == "cash_limited"
    assert trades.iloc[0]["requested_units"] == 1000.0
    assert trades.iloc[0]["executed_units"] == pytest.approx(990.09)


def test_fund_engine_runs_common_strategy_lifecycle(tmp_path):
    events = []

    class LifecycleStrategy(Strategy):
        def initialize(self):
            events.append("initialize")

        def before_trading(self):
            events.append(("before", self.ctx.current_date, self.ctx.portfolio is not None))

        def generate_signals(self, dt):
            events.append(("signal", dt))
            return {"000001": 1.0}

        def on_rebalance(self, dt, old_weights, new_weights):
            events.append(("rebalance", dt, dict(old_weights), dict(new_weights)))

        def on_order_filled(self, trade):
            events.append(("fill", trade.code, trade.side.value, trade.date))

        def teardown(self):
            events.append("teardown")

    rows = {"000001": [
        {"date": "2024-01-02", "nav": 1.0},
        {"date": "2024-01-03", "nav": 1.0},
        {"date": "2024-01-04", "nav": 1.0},
    ]}
    FundNavBacktestEngine(LifecycleStrategy).run(
        rows=rows, start=date(2024, 1, 2), end=date(2024, 1, 4),
        initial_capital=1000, output_dir=str(tmp_path), rebalance_frequency="daily",
    )
    assert events[0] == "initialize"
    assert sum(1 for event in events if isinstance(event, tuple) and event[0] == "before") == 3
    assert sum(1 for event in events if isinstance(event, tuple) and event[0] == "rebalance") == 3
    assert sum(1 for event in events if isinstance(event, tuple) and event[0] == "fill") == 1
    assert events[-1] == "teardown"


def test_fund_engine_tears_down_strategy_when_cancelled(tmp_path):
    events = []

    class CancelledStrategy(Strategy):
        def initialize(self):
            events.append("initialize")

        def generate_signals(self, dt):
            return {"000001": 1.0}

        def teardown(self):
            events.append("teardown")

    rows = {"000001": [
        {"date": "2024-01-01", "nav": 1.0},
        {"date": "2024-01-02", "nav": 1.0},
    ]}
    with pytest.raises(RuntimeError, match="cancelled"):
        FundNavBacktestEngine(CancelledStrategy).run(
            rows=rows, start=date(2024, 1, 1), end=date(2024, 1, 2),
            initial_capital=1000, output_dir=str(tmp_path),
            cancel_check=lambda: True,
        )
    assert events == ["initialize", "teardown"]


def test_fund_engine_tears_down_strategy_when_initialization_fails(tmp_path):
    events = []

    class FailingStrategy(Strategy):
        def initialize(self):
            events.append("initialize")
            raise RuntimeError("init failed")

        def generate_signals(self, dt):
            return {}

        def teardown(self):
            events.append("teardown")

    rows = {"000001": [{"date": "2024-01-02", "nav": 1.0}]}
    with pytest.raises(RuntimeError, match="init failed"):
        FundNavBacktestEngine(FailingStrategy).run(
            rows=rows, start=date(2024, 1, 2), end=date(2024, 1, 2),
            initial_capital=1000, output_dir=str(tmp_path),
        )
    assert events == ["initialize", "teardown"]
