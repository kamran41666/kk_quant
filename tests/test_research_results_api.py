import hashlib
import json
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import BackgroundTasks, HTTPException

from server.api.backtest import BacktestRunRequest, get_research_comparison, run_backtest
from server.api.analytics import get_metrics


class QueryDB:
    def __init__(self, row):
        self.row = row

    def query(self, *args):
        return self

    def filter(self, *args):
        return self

    def first(self):
        return self.row


def test_comparison_is_bound_to_archived_content(tmp_path):
    path = tmp_path / "comparison.json"
    payload = {"labels": ["2024-01-02"], "series": [], "validated": False}
    path.write_text(json.dumps(payload))
    run = SimpleNamespace(status="completed", result_dir=str(tmp_path), data_manifest=json.dumps({
        "research_experiment": True, "comparison_content_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
    }))
    assert get_research_comparison("run", QueryDB(run)) == payload
    path.write_text(json.dumps({**payload, "validated": True}))
    with pytest.raises(HTTPException) as error:
        get_research_comparison("run", QueryDB(run))
    assert error.value.status_code == 409


def test_research_strategy_cannot_use_legacy_adjusted_execution():
    strategy = SimpleNamespace(strategy_class="strategies.research_low_volatility.ResearchLowVolatilityStrategy",
                               market="a-share", params="{}")
    request = BacktestRunRequest(strategy_id="candidate", start_date="2020-01-02", end_date="2020-12-31")
    with pytest.raises(HTTPException) as error:
        run_backtest(request, BackgroundTasks(), QueryDB(strategy))
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "FROZEN_RESEARCH_ENGINE_REQUIRED"


def test_research_metrics_keep_frozen_calendar_year_convention(tmp_path):
    pd.DataFrame({"date": ["2020-01-02", "2020-01-03"], "daily_return": [0., .1]}).to_parquet(tmp_path / "daily_portfolio.parquet")
    metrics = {"annual_return": .12, "annual_volatility": .2, "sharpe": .6,
               "max_drawdown": -.1, "calmar": 1.2, "trading_days": 1000, "annualization": "calendar CAGR, rf=0"}
    run = SimpleNamespace(status="completed", result_dir=str(tmp_path), data_manifest=json.dumps({
        "research_experiment": True, "research_metrics": metrics,
    }))
    response = get_metrics("run", QueryDB(run))
    assert response["annual_return"] == .12
    assert response["sharpe_ratio"] == .6
    assert response["metric_convention"] == "calendar CAGR, rf=0"
    assert response["win_rate"] is None
