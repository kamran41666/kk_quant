"""H2b integrated v3 loop tests for cohort execution and evidence rows."""
from datetime import date, timedelta
from decimal import Decimal
import shutil

import pandas as pd
import pyarrow.parquet as pq
import pytest

from quant_engine.backtest.manual_daily_portfolio_v3 import (
    ManualPortfolioInputManifest,
    run_manual_daily_portfolio_v3,
)
from quant_engine.backtest.manual_research_ledger import ResearchCorporateAction
from quant_engine.backtest.manual_portfolio_evidence import verify_portfolio_evidence_directory
from tests.test_manual_daily_factor_research import _bundle


HASH = "1" * 64


class Calendar:
    def __init__(self, days):
        self.days = days

    def ensure_coverage(self, start, end):
        return {"complete": start >= self.days[0] and end <= self.days[-1], "content_hash": HASH}

    def get_trading_days(self, start, end):
        return [day for day in self.days if start <= day <= end]


def _inputs(days):
    daily = []
    eligibility = []
    benchmark = []
    for index, day in enumerate(days):
        for code, base in (("600000.SH", 10), ("000001.SZ", 20)):
            price = Decimal(base) + Decimal("0.1") * index
            daily.append({
                "date": day, "code": code, "open": price, "high": price,
                "low": price, "close": price,
                "preclose": price if index == 0 else Decimal(base) + Decimal("0.1") * (index - 1),
                "volume": 1_000_000, "amount": 10_000_000,
                "is_suspended": False, "is_st": False,
            })
            eligibility.append({"date": day, "code": code, "is_eligible": True})
        benchmark.append({"date": day, "close": 100 + index})
    return pd.DataFrame(daily), pd.DataFrame(eligibility), pd.DataFrame(benchmark)


def _manifest(*, unresolved=0):
    return ManualPortfolioInputManifest(
        dataset_id="dataset-v3", dataset_content_hash="5" * 64,
        calendar_content_hash=HASH, signal_content_hash="2" * 64,
        eligibility_content_hash="3" * 64, corporate_action_content_hash="4" * 64,
        benchmark_id="csi300-price", benchmark_content_hash="6" * 64,
        training_artifact_id="training-artifact", validation_artifact_id="validation-artifact",
        unresolved_adjustment_count=unresolved,
    )


def test_v3_runs_ordered_daily_cycle_and_preserves_action_entitlement_by_cohort():
    days = [date(2027, 1, 4) + timedelta(days=index) for index in range(8)]
    days = [day for day in days if day.weekday() < 5]
    daily, eligibility, benchmark = _inputs(days)
    action = ResearchCorporateAction(
        action_id="v3-cash-action", code="600000.SH", record_date=days[1],
        ex_date=days[2], pay_date=days[3], stock_listing_date=None,
        source_hash="4" * 64, cash_per_share="0.1", bonus_ratio="0",
    )
    result = run_manual_daily_portfolio_v3(
        daily=daily, eligibility=eligibility, benchmark=benchmark,
        signals={days[0]: {"600000.SH": 1}, days[1]: {"600000.SH": 2}},
        corporate_actions=[action], calendar=Calendar(days), bundle=_bundle(),
        input_manifest=_manifest(), start=days[0], end=days[-1], initial_capital=100_000,
    )
    assert [row["date"] for row in result.daily] == [day.isoformat() for day in days]
    assert len(result.benchmark) == len(days)
    assert any(row["phase"] == "open" and row["side"] == "buy" for row in result.trades)
    assert any(row["phase"] == "close" and row["side"] == "sell" for row in result.trades)
    records = [row for row in result.corporate_actions if row["stage"] == "record"]
    assert {row["cohort_id"] for row in records} == {f"cohort-{days[0].isoformat()}"}
    ex_cash = [row for row in result.corporate_actions if row["stage"] == "ex_cash"]
    assert len(ex_cash) == 1 and Decimal(ex_cash[0]["receivable_cash"]) > 0
    assert result.result_hash == run_manual_daily_portfolio_v3(
        daily=daily, eligibility=eligibility, benchmark=benchmark,
        signals={days[0]: {"600000.SH": 1}, days[1]: {"600000.SH": 2}},
        corporate_actions=[action], calendar=Calendar(days), bundle=_bundle(),
        input_manifest=_manifest(), start=days[0], end=days[-1], initial_capital=100_000,
    ).result_hash
    assert result.promotion_eligible is False


def test_v3_fails_on_calendar_or_benchmark_evidence_and_surfaces_known_adjustments():
    days = [date(2027, 1, 4) + timedelta(days=index) for index in range(4)]
    daily, eligibility, benchmark = _inputs(days)
    bad_calendar = Calendar(days)
    bad_calendar.ensure_coverage = lambda start, end: {"complete": True, "content_hash": "0" * 64}
    with pytest.raises(ValueError, match="calendar_evidence_mismatch"):
        run_manual_daily_portfolio_v3(
            daily=daily, eligibility=eligibility, benchmark=benchmark, signals={},
            corporate_actions=[], calendar=bad_calendar, bundle=_bundle(),
            input_manifest=_manifest(), start=days[0], end=days[-1],
        )
    with pytest.raises(ValueError, match="benchmark_coverage"):
        run_manual_daily_portfolio_v3(
            daily=daily, eligibility=eligibility, benchmark=benchmark.iloc[:-1], signals={},
            corporate_actions=[], calendar=Calendar(days), bundle=_bundle(),
            input_manifest=_manifest(), start=days[0], end=days[-1],
        )
    result = run_manual_daily_portfolio_v3(
        daily=daily, eligibility=eligibility, benchmark=benchmark, signals={},
        corporate_actions=[], calendar=Calendar(days), bundle=_bundle(),
        input_manifest=_manifest(unresolved=50), start=days[0], end=days[-1],
    )
    assert result.quality_errors == ["unexplained_reference_adjustments"]


def test_v3_metrics_replay_and_atomic_twelve_file_output(tmp_path):
    days = [date(2027, 1, 4) + timedelta(days=index) for index in range(8)]
    days = [day for day in days if day.weekday() < 5]
    daily, eligibility, benchmark = _inputs(days)
    result = run_manual_daily_portfolio_v3(
        daily=daily, eligibility=eligibility, benchmark=benchmark,
        signals={days[0]: {"600000.SH": 1}, days[1]: {"000001.SZ": 2}},
        corporate_actions=[], calendar=Calendar(days), bundle=_bundle(),
        input_manifest=_manifest(), start=days[0], end=days[-1], initial_capital=100_000,
    )
    metrics = result.metrics()
    assert {
        "total_return", "benchmark_total_return", "excess_return", "sharpe_252_rf0",
        "max_drawdown_magnitude", "annual_turnover_double_sided",
        "capacity_fill_rate_amount_weighted", "total_fees",
    } <= set(metrics)
    assert result.replay()["accounting_passed"] is True
    assert result.replay()["passed"] is False
    output = result.write_evidence(tmp_path / "evidence")
    assert {path.name for path in output.iterdir()} == {
        "signals.parquet", "order_intents.parquet", "order_attempts.parquet",
        "trades.parquet", "corporate_actions.parquet", "positions.parquet",
        "daily_portfolio.parquet", "benchmark.parquet", "audit.json",
        "replay.json", "summary.json", "manifest.json",
    }
    manifest = __import__("json").loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["files"]) == 11
    assert len(manifest["manifest_hash"]) == 64
    assert manifest["eligible_for_artifact_registration"] is False
    verified = verify_portfolio_evidence_directory(output)
    assert verified["verified"] is True and verified["file_count"] == 12
    assert str(pq.read_schema(output / "corporate_actions.parquet").field("cash_per_share").type) == "decimal128(24, 8)"
    assert pq.read_table(output / "corporate_actions.parquet").num_rows == 0
    with pytest.raises(FileExistsError, match="output_exists"):
        result.write_evidence(output)
    tampered = tmp_path / "tampered"
    shutil.copytree(output, tampered)
    (tampered / "summary.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="file_hash_mismatch:summary.json"):
        verify_portfolio_evidence_directory(tampered)
    extra = tmp_path / "extra"
    shutil.copytree(output, extra)
    (extra / "unexpected.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="file_set_mismatch"):
        verify_portfolio_evidence_directory(extra)
    result.daily[-1]["cash"] = "0"
    replay = result.replay()
    assert replay["passed"] is False
    assert replay["first_difference"]["check"] in {"cash_replay", "equity_replay"}


def test_exit_retries_share_one_logical_intent_denominator():
    days = [date(2027, 1, 4) + timedelta(days=index) for index in range(10)]
    days = [day for day in days if day.weekday() < 5]
    daily, eligibility, benchmark = _inputs(days)
    for day in (days[2], days[3]):
        daily.loc[(daily["date"] == day) & (daily["code"] == "600000.SH"), "is_suspended"] = True
    result = run_manual_daily_portfolio_v3(
        daily=daily, eligibility=eligibility, benchmark=benchmark,
        signals={days[0]: {"600000.SH": 1}}, corporate_actions=[],
        calendar=Calendar(days), bundle=_bundle(), input_manifest=_manifest(),
        start=days[0], end=days[-1], initial_capital=100_000,
    )
    exits = [row for row in result.intents if row["side"] == "sell"]
    assert len(exits) == 3
    assert len({row["logical_intent_id"] for row in exits}) == 1
    assert result.metrics()["capacity_fill_rate_amount_weighted"] == "1"


def test_v3_rejects_non_boolean_eligibility_and_action_source_mismatch():
    days = [date(2027, 1, 4) + timedelta(days=index) for index in range(4)]
    daily, eligibility, benchmark = _inputs(days)
    eligibility["is_eligible"] = "False"
    with pytest.raises(ValueError, match="eligibility_value_not_boolean"):
        run_manual_daily_portfolio_v3(
            daily=daily, eligibility=eligibility, benchmark=benchmark, signals={},
            corporate_actions=[], calendar=Calendar(days), bundle=_bundle(),
            input_manifest=_manifest(), start=days[0], end=days[-1],
        )
    eligibility["is_eligible"] = True
    action = ResearchCorporateAction(
        action_id="wrong-source", code="600000.SH", record_date=days[0],
        ex_date=days[1], pay_date=days[2], stock_listing_date=None,
        source_hash="9" * 64, cash_per_share="0.1", bonus_ratio="0",
    )
    with pytest.raises(ValueError, match="action_source_hash_mismatch"):
        run_manual_daily_portfolio_v3(
            daily=daily, eligibility=eligibility, benchmark=benchmark, signals={},
            corporate_actions=[action], calendar=Calendar(days), bundle=_bundle(),
            input_manifest=_manifest(), start=days[0], end=days[-1],
        )


def test_replay_detects_coordinated_position_and_daily_market_value_inflation():
    days = [date(2027, 1, 4) + timedelta(days=index) for index in range(6)]
    daily, eligibility, benchmark = _inputs(days)
    result = run_manual_daily_portfolio_v3(
        daily=daily, eligibility=eligibility, benchmark=benchmark,
        signals={days[0]: {"600000.SH": 1}}, corporate_actions=[],
        calendar=Calendar(days), bundle=_bundle(), input_manifest=_manifest(),
        start=days[0], end=days[-1], initial_capital=100_000,
    )
    target_date = result.positions[0]["date"]
    result.positions[0]["market_value"] = str(Decimal(result.positions[0]["market_value"]) + 10_000)
    daily_row = next(row for row in result.daily if row["date"] == target_date)
    daily_row["market_value"] = str(Decimal(daily_row["market_value"]) + 10_000)
    daily_row["equity"] = str(Decimal(daily_row["equity"]) + 10_000)
    replay = result.replay()
    assert replay["accounting_passed"] is False
    assert replay["first_difference"]["check"] == "position_market_value_replay"
