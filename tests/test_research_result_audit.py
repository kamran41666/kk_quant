import json
from pathlib import Path

import pandas as pd

from scripts.audit_ashare_results import audit_matrix, main


def _write_fixture(root: Path) -> Path:
    run = root / "run-1"
    run.mkdir(parents=True)
    initial = 10_000.0
    cash = [8_995.0, 8_995.0, 9_989.01]
    receivable = [0.0, 10.0, 0.0]
    market_value = [1_000.0, 990.0, 0.0]
    total = [sum(values) for values in zip(cash, receivable, market_value, strict=True)]
    returns = [total[0] / initial - 1.0]
    returns.extend(total[index] / total[index - 1] - 1.0 for index in range(1, len(total)))
    days = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]).date
    pd.DataFrame({
        "date": days,
        "cash": cash,
        "receivable_cash": receivable,
        "market_value": market_value,
        "total_value": total,
        "daily_return": returns,
    }).to_parquet(run / "daily_portfolio.parquet", index=False)
    pd.DataFrame([
        {"date": days[0], "code": "600000.SH", "shares": 100, "market_value": 1_000.0},
        {"date": days[1], "code": "600000.SH", "shares": 110, "market_value": 990.0},
    ]).to_parquet(run / "daily_positions.parquet", index=False)
    pd.DataFrame([
        {"date": days[0], "code": "600000.SH", "side": "buy", "shares": 100,
         "price": 10.0, "amount": 1_000.0, "commission": 5.0,
         "stamp_duty": 0.0, "slippage": 0.0},
        {"date": days[2], "code": "600000.SH", "side": "sell", "shares": 110,
         "price": 9.0, "amount": 990.0, "commission": 5.0,
         "stamp_duty": 0.99, "slippage": 0.0},
    ]).to_parquet(run / "trades.parquet", index=False)
    (run / "summary.json").write_text(json.dumps({"initial_capital": initial}))
    (run / "corporate_actions.json").write_text(json.dumps([
        {"action_id": "dividend-1", "date": "2024-01-02", "code": "600000.SH",
         "stage": "record", "shares": 100},
        {"action_id": "dividend-1", "date": "2024-01-03", "code": "600000.SH",
         "stage": "ex", "entitled_shares": 100, "cash_receivable": 10.0, "bonus_shares": 10,
         "bonus_shares_booked": 10, "bonus_allocation_verified": True},
        {"action_id": "dividend-1", "date": "2024-01-04", "code": "600000.SH",
         "stage": "pay", "cash": 10.0},
        {"action_id": "dividend-1", "date": "2024-01-04", "code": "600000.SH",
         "stage": "stock_listing", "shares": 10},
    ]))
    (run / "audit.json").write_text("[]")
    matrix = root / "matrix.json"
    matrix.write_text(json.dumps({"runs": [
        {"run_id": "run-1", "label": "fixture", "status": "completed", "result_dir": str(run)},
        {"run_id": "pending", "status": "running", "result_dir": "ignored"},
    ]}))
    return matrix


def test_independent_replay_passes_balanced_cash_actions_and_shares(tmp_path):
    matrix = _write_fixture(tmp_path)
    result = audit_matrix(matrix)
    assert result["completed_run_count"] == 1
    assert result["passed_run_count"] == 1
    assert result["failed_run_count"] == 0
    assert result["runs"][0]["passed"] is True
    assert result["runs"][0]["max_diff"]["cash"] == 0.0
    assert (tmp_path / "accounting-audit.json").exists()


def test_cash_tampering_fails_independent_replay(tmp_path):
    matrix = _write_fixture(tmp_path)
    path = tmp_path / "run-1" / "daily_portfolio.parquet"
    portfolio = pd.read_parquet(path)
    portfolio.loc[1, "cash"] += 1.0
    portfolio.to_parquet(path, index=False)

    result = audit_matrix(matrix)
    run = result["runs"][0]
    assert run["passed"] is False
    assert run["checks"]["cash_replay"] is False
    assert run["max_diff"]["cash"] == 1.0


def test_daily_return_tampering_fails_equity_compounding(tmp_path):
    matrix = _write_fixture(tmp_path)
    path = tmp_path / "run-1" / "daily_portfolio.parquet"
    portfolio = pd.read_parquet(path)
    portfolio.loc[1, "daily_return"] = 0.01
    portfolio.to_parquet(path, index=False)

    output = tmp_path / "custom-audit.json"
    result = audit_matrix(matrix, output_path=output)
    run = result["runs"][0]
    assert run["passed"] is False
    assert run["checks"]["daily_return_reconcile"] is False
    assert run["max_diff"]["compounded_equity"] > 1.0
    assert output.exists()
    assert main(["--matrix", str(matrix), "--output", str(output)]) == 1
