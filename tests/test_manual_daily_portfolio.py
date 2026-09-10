"""M3 portfolio cost and data-boundary checks."""
from datetime import date, timedelta

import pytest

from quant_engine.backtest.manual_daily_portfolio import run_manual_daily_portfolio
from tests.test_manual_daily_factor_research import Calendar, _bars, _bundle


def test_incomplete_calendar_fails_closed():
    days = [date(2024, 1, 2) + timedelta(days=index) for index in range(3)]
    days = [day for day in days if day.weekday() < 5]
    calendar = Calendar(days)
    calendar.days = days[:1]
    with pytest.raises(ValueError, match="coverage"):
        run_manual_daily_portfolio(
            daily=_bars(days), signals={days[0]: {"600000.SH": 1}}, calendar=calendar,
            bundle=_bundle(), start=days[0], end=days[-1], initial_capital=100000,
        )


def test_stress_costs_are_not_silently_baseline():
    days = [date(2024, 1, 2) + timedelta(days=index) for index in range(5)]
    days = [day for day in days if day.weekday() < 5]
    kwargs = {"daily": _bars(days), "signals": {days[0]: {"600000.SH": 1}}, "calendar": Calendar(days), "start": days[0], "end": days[-1], "initial_capital": 100000}
    baseline = run_manual_daily_portfolio(bundle=_bundle("baseline"), **kwargs)
    stress = run_manual_daily_portfolio(bundle=_bundle("stress"), **kwargs)
    assert stress.final_equity < baseline.final_equity


def test_open_capacity_uses_previous_session_volume_without_lookahead():
    days = [date(2024, 1, 2) + timedelta(days=index) for index in range(4)]
    frame = _bars(days)
    frame.loc[(frame["date"] == days[0]) & (frame["code"] == "600000.SH"), "volume"] = 0
    frame.loc[(frame["date"] == days[1]) & (frame["code"] == "600000.SH"), "volume"] = 100_000_000
    result = run_manual_daily_portfolio(
        daily=frame, signals={days[0]: {"600000.SH": 1}}, calendar=Calendar(days),
        bundle=_bundle(), start=days[0], end=days[-1], initial_capital=100000,
    )
    assert result.trades == []
    assert any(item["reason"] == "previous_session_volume_unavailable" for item in result.audit)
