import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from server.api.backtest import _import_strategy, BacktestRunRequest, run_backtest
from server.models.database import Base
from server.models.schema import Run, Strategy
from strategies.small_cap_value import SmallCapValueStrategy


def test_import_strategy_allows_local_strategy_subclass():
    loaded = _import_strategy(
        "strategies.small_cap_value.SmallCapValueStrategy"
    )
    assert loaded is SmallCapValueStrategy


@pytest.mark.parametrize("path", [
    "os.system",
    "server.main.app",
    "quant_engine.backtest.engine.BacktestEngine",
])
def test_import_strategy_rejects_non_strategy_modules(path):
    with pytest.raises(ValueError, match="local strategies package"):
        _import_strategy(path)


@pytest.mark.parametrize("payload", [
    {"strategy_id": "s", "start_date": "2024-01-01", "end_date": "2024-01-02", "initial_capital": 0},
    {"strategy_id": "s", "start_date": "2024-02-30", "end_date": "2024-03-01"},
    {"strategy_id": "s", "start_date": "2024-02-01", "end_date": "2024-01-01"},
    {"strategy_id": "s", "start_date": "2024-01-01", "end_date": "2024-01-02", "rebalance_frequency": "hourly"},
])
def test_backtest_request_rejects_unsafe_parameters(payload):
    with pytest.raises(Exception):
        BacktestRunRequest.model_validate(payload)


def test_backtest_request_restricts_fund_fee_rate_to_domestic_funds():
    with pytest.raises(Exception, match="fund_fee_rate applies only"):
        BacktestRunRequest.model_validate({
            "strategy_id": "s",
            "market": "a-share",
            "fund_fee_rate": 0.001,
            "start_date": "2024-01-01",
            "end_date": "2024-01-02",
        })
    request = BacktestRunRequest.model_validate({
        "strategy_id": "s",
        "market": "cn-fund",
        "symbols": ["110022"],
        "fund_fee_rate": 0.001,
        "start_date": "2024-01-01",
        "end_date": "2024-01-02",
    })
    assert request.fund_fee_rate == 0.001


def test_backtest_request_restricts_cost_scenarios_to_a_share():
    request = BacktestRunRequest.model_validate({
        "strategy_id": "s",
        "market": "a-share",
        "cost_scenario": "paper_high_impact_v1",
        "start_date": "2024-01-01",
        "end_date": "2024-01-02",
    })
    assert request.cost_scenario == "paper_high_impact_v1"
    with pytest.raises(Exception, match="cost_scenario applies only"):
        BacktestRunRequest.model_validate({
            "strategy_id": "s",
            "market": "cn-fund",
            "symbols": ["110022"],
            "cost_scenario": "paper_high_impact_v1",
            "start_date": "2024-01-01",
            "end_date": "2024-01-02",
        })


def test_cost_scenario_catalog_is_read_only_and_explicit():
    from server.api.backtest import list_cost_scenarios
    result = list_cost_scenarios()
    assert result["meta"]["research_only"] is True
    assert {item["id"] for item in result["data"]} == {
        "paper_baseline_v1", "paper_low_impact_v1", "paper_high_impact_v1",
    }


def test_fund_backtest_http_submission_fails_before_pending_without_calendar(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    strategy = Strategy(
        name="fund strategy",
        description="test",
        strategy_class="strategies.fund_nav_momentum.FundNavMomentumStrategy",
        params="{}",
        market="cn-fund",
    )
    db.add(strategy)
    db.commit()
    db.refresh(strategy)

    class UnverifiedCalendar:
        def __init__(self, start_year, end_year):
            pass

        def ensure_coverage(self, start, end):
            return {"complete": False, "verified": False, "source": "fallback:business-days"}

    monkeypatch.setattr("quant_engine.data.calendar.TradingCalendar", UnverifiedCalendar)
    request = BacktestRunRequest.model_validate({
        "strategy_id": strategy.id,
        "market": "cn-fund",
        "symbols": ["110022"],
        "start_date": "2024-01-02",
        "end_date": "2024-01-03",
    })
    with pytest.raises(HTTPException) as exc:
        run_backtest(request, BackgroundTasks(), db)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "FUND_CALENDAR_COVERAGE_INSUFFICIENT"
    assert db.query(Run).count() == 0
    db.close()
