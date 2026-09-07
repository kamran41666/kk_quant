from datetime import date
import json

import pandas as pd
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from server.api.analytics import compare_runs, get_metrics
from server.models.database import Base
from server.models.schema import Run


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _run(db, tmp_path, run_id, *, market="a-share", fund=False):
    result_dir = tmp_path / run_id
    result_dir.mkdir()
    pd.DataFrame([
        {"date": date(2024, 1, 2), "daily_return": 0.01, "total_value": 101.0},
        {"date": date(2024, 1, 3), "daily_return": -0.005, "total_value": 100.5},
    ]).to_parquet(result_dir / "daily_portfolio.parquet", index=False)
    manifest = {
        "daily_data_content_hash": f"{run_id}content",
        "daily_data_coverage": {"complete": True, "dataset_hash": f"{run_id}dataset"},
    }
    if fund:
        manifest.pop("daily_data_content_hash")
        manifest.pop("daily_data_coverage")
        manifest["fund_fee_rate"] = 0.001
        manifest["dataset_manifests"] = [{
            "dataset_id": f"dataset-{run_id}",
            "content_hash": f"{run_id}navhash",
            "row_count": 2,
        }]
    row = Run(
        id=run_id, strategy_id=f"strategy-{run_id}", run_type="backtest", status="completed",
        market=market, result_dir=str(result_dir), data_manifest=json.dumps(manifest),
        strategy_fingerprint=f"{run_id}fingerprint", data_end="2024-01-03",
        calendar_version="trading-calendar-v1", execution_model="next_trading_day_open-v1",
        created_at="2024-01-03T00:00:00",
    )
    db.add(row); db.commit()
    return row


def test_compare_runs_returns_metrics_and_evidence(tmp_path):
    db = _db()
    _run(db, tmp_path, "run-a")
    _run(db, tmp_path, "run-b")

    result = compare_runs("run-b,run-a", db)

    assert [item["run_id"] for item in result["data"]] == ["run-b", "run-a"]
    assert result["meta"] == {"research_only": True, "comparison_count": 2, "same_market": True}
    assert result["data"][0]["evidence"]["coverage_complete"] is True
    assert "sharpe_ratio" in result["data"][0]["metrics"]


def test_compare_runs_rejects_invalid_cardinality(tmp_path):
    db = _db()
    _run(db, tmp_path, "run-a")
    with pytest.raises(HTTPException) as raised:
        compare_runs("run-a", db)
    assert raised.value.status_code == 422


def test_compare_runs_exposes_fund_nav_dataset_evidence(tmp_path):
    db = _db()
    _run(db, tmp_path, "fund-a", market="cn-fund", fund=True)
    _run(db, tmp_path, "fund-b", market="cn-fund", fund=True)

    result = compare_runs("fund-a,fund-b", db)

    evidence = result["data"][0]["evidence"]
    assert evidence["coverage_complete"] is True
    assert evidence["dataset_hashes"] == ["fund-anavhash"]
    assert evidence["data_content_hash"] == "fund-anavhash"
    assert evidence["cost_scenario"] is None
    assert evidence["fund_fee_rate"] == 0.001


def test_metrics_handles_legacy_fund_trade_schema(tmp_path):
    db = _db()
    row = _run(db, tmp_path, "fund-legacy", market="cn-fund", fund=True)
    pd.DataFrame([{
        "date": date(2024, 1, 3), "code": "110022", "side": "sell",
        "units": 10.0, "price": 3.5, "amount": 35.0, "fee": 0.0,
        "status": "filled",
    }]).to_parquet(row.result_dir + "/trades.parquet", index=False)
    metrics = get_metrics(row.id, db)
    assert metrics["win_rate"] is None
    assert metrics["profit_loss_ratio"] is None
