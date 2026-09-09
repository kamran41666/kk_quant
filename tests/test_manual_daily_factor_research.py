"""M3 manual-daily bundle and raw-price portfolio research tests."""
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd

from quant_engine.backtest.manual_daily_portfolio import run_manual_daily_portfolio
from quant_engine.factor.manual_daily_bundle import ManualDailyFactorBundleV2


class Calendar:
    def __init__(self, days):
        self.days = days

    def ensure_coverage(self, start, end):
        return {"complete": start >= self.days[0] and end <= self.days[-1], "content_hash": "calendar"}

    def get_trading_days(self, start, end):
        return [day for day in self.days if start <= day <= end]


def _bundle(cost_scenario="baseline"):
    return ManualDailyFactorBundleV2(
        strategy_key="manual-test", factor_name="factor-x",
        factor_expression_hash="1" * 64, label_spec_hash="2" * 64,
        training_evidence_hash="3" * 64, validation_evidence_hash="4" * 64,
        dataset_content_hash="5" * 64, top_n=1, cost_scenario=cost_scenario,
    )


def _bars(days):
    rows = []
    for index, day in enumerate(days):
        rows.append({"date": day, "code": "600000.SH", "open": 10 + index, "close": 10.2 + index})
        rows.append({"date": day, "code": "000001.SZ", "open": 20 + index, "close": 20.1 + index})
    return pd.DataFrame(rows)


def test_bundle_identity_is_independent_and_contains_manual_protocol():
    bundle = _bundle()
    identity = bundle.as_dict()
    assert identity["protocol_version"] == "manual-daily-factor-bundle-v2"
    assert identity["execution_policy"] == {
        "signal_phase": "close", "entry_offset": 1, "entry_phase": "open",
        "exit_offset": 2, "exit_phase": "close", "cohort_count": 2,
        "cohort_gross_exposure": "0.45", "entry_expiry": 1,
        "exit_retry_policy": "next_trading_close", "auto_submit": False,
    }
    assert len(bundle.bundle_hash) == 64


def test_two_cohorts_use_close_signal_next_open_entry_and_close_exit():
    days = [date(2024, 1, 2) + timedelta(days=index) for index in range(5)]
    days = [day for day in days if day.weekday() < 5]
    signals = {days[0]: {"600000.SH": Decimal("2")}, days[1]: {"000001.SZ": Decimal("3")}}
    calendar = Calendar(days)
    result = run_manual_daily_portfolio(
        daily=_bars(days), signals=signals, calendar=calendar, bundle=_bundle(),
        start=days[0], end=days[-1], initial_capital="100000",
    )
    assert result.trades[0]["date"] == days[1].isoformat()
    assert result.trades[0]["side"] == "buy"
    assert any(item["side"] == "sell" and item["date"] == days[3].isoformat() for item in result.trades)
    assert [item["date"] for item in result.daily] == [day.isoformat() for day in days]
    assert result.summary()["research_only"] is True
    assert result.summary()["paper_authorized"] is False
    repeat = run_manual_daily_portfolio(
        daily=_bars(days), signals=signals, calendar=calendar, bundle=_bundle(),
        start=days[0], end=days[-1], initial_capital="100000",
    )
    assert repeat.result_hash == result.result_hash


def test_missing_exit_bar_is_recorded_and_does_not_create_fake_fill():
    days = [date(2024, 1, 2) + timedelta(days=index) for index in range(5)]
    days = [day for day in days if day.weekday() < 5]
    frame = _bars(days)
    frame = frame[~((frame["date"] == days[2]) & (frame["code"] == "600000.SH"))]
    result = run_manual_daily_portfolio(
        daily=frame, signals={days[0]: {"600000.SH": 1}}, calendar=Calendar(days), bundle=_bundle(),
        start=days[0], end=days[-1], initial_capital=100000,
    )
    assert any(item["reason"] == "exit_close_unavailable" for item in result.audit)
