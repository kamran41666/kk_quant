from datetime import date
from decimal import Decimal

import pytest

from quant_engine.factor.manual_daily_label import (
    MANUAL_DAILY_LABEL_V1,
    compute_manual_daily_label,
    label_dates,
)
from quant_engine.trading.effective_rules import EffectiveDatedTradingRuleRegistry
from quant_engine.trading.manual_protocol import (
    AccountSnapshot,
    CohortStatus,
    DailyDecision,
    DecisionAction,
    ManualCohort,
    ManualDailyPolicy,
    ManualPosition,
    QuoteSnapshot,
    resolve_action,
)


class Calendar:
    def __init__(self, dates):
        self.dates = tuple(dates)

    def next_trading_day(self, value):
        for item in self.dates:
            if item > value:
                return item
        raise ValueError("no next session")


def test_manual_policy_and_protocol_hashes_are_stable():
    first = ManualDailyPolicy()
    second = ManualDailyPolicy()
    assert first.policy_hash == second.policy_hash
    decision = DailyDecision(
        signal_date=date(2024, 1, 5),
        action=DecisionAction.HOLD,
        target_weights={"000001.SZ": Decimal("0.45")},
    )
    same = DailyDecision(
        signal_date=date(2024, 1, 5),
        action="hold",
        target_weights={"000001.SZ": Decimal("0.45")},
    )
    assert decision.input_hash == same.input_hash
    assert decision.decision_hash == same.decision_hash
    assert decision.id == same.id


def test_protocol_rejects_invalid_policy_and_positions():
    with pytest.raises(ValueError, match="cannot submit"):
        ManualDailyPolicy(auto_submit=True)
    with pytest.raises(ValueError, match="available_quantity"):
        ManualPosition("000001.SZ", quantity=100, available_quantity=101)
    with pytest.raises(ValueError, match="target weights"):
        DailyDecision(date(2024, 1, 5), "hold", {"000001.SZ": 1.1})


def test_action_priority_is_fail_closed():
    assert resolve_action("hold", "rebalance") == "rebalance"
    assert resolve_action("rebalance", "reduce", "flat") == "flat"
    assert resolve_action("flat", "blocked") == "blocked"
    assert resolve_action("blocked", "reconcile") == "reconcile"
    assert resolve_action() == "hold"


def test_cohort_uses_trading_dates_and_typed_status():
    cohort = ManualCohort(
        id="cohort-a",
        signal_date=date(2024, 1, 5),
        planned_entry_date=date(2024, 1, 8),
        planned_exit_date=date(2024, 1, 9),
        sleeve_index=0,
        budget=Decimal("0.45"),
        status=CohortStatus.PLANNED,
    )
    assert cohort.status == "planned"


def test_manual_daily_label_skips_weekend_by_calendar_only():
    calendar = Calendar([date(2024, 1, 8), date(2024, 1, 9)])
    assert label_dates(date(2024, 1, 5), calendar) == (date(2024, 1, 8), date(2024, 1, 9))
    label = compute_manual_daily_label(
        date(2024, 1, 5),
        {
            date(2024, 1, 8): {"adjusted_open": "10.00"},
            date(2024, 1, 9): {"adjusted_close": "10.50"},
        },
        calendar,
    )
    assert label.spec_id == "manual-daily-label-v1"
    assert label.entry_date == date(2024, 1, 8)
    assert label.exit_date == date(2024, 1, 9)
    assert label.value == Decimal("0.05")
    assert label.input_hash == compute_manual_daily_label(
        date(2024, 1, 5),
        {
            "2024-01-08": {"adjusted_open": 10},
            "2024-01-09": {"adjusted_close": 10.5},
        },
        calendar,
    ).input_hash


def test_manual_daily_label_requires_both_positive_prices():
    calendar = Calendar([date(2024, 1, 8), date(2024, 1, 9)])
    with pytest.raises(ValueError, match="missing price row"):
        compute_manual_daily_label(date(2024, 1, 5), {}, calendar, MANUAL_DAILY_LABEL_V1)
    with pytest.raises(ValueError, match="positive"):
        compute_manual_daily_label(
            date(2024, 1, 5),
            {date(2024, 1, 8): {"open": 0}, date(2024, 1, 9): {"close": 10}},
            calendar,
        )


def test_effective_rules_change_stamp_duty_by_date_without_float_money():
    registry = EffectiveDatedTradingRuleRegistry.default()
    before = registry.resolve("a-share", date(2023, 8, 25))
    after = registry.resolve("a-share", date(2023, 8, 28))
    assert before.stamp_duty_rate == Decimal("0.001")
    assert after.stamp_duty_rate == Decimal("0.0005")
    fee = registry.estimate_fee("a-share", Decimal("10000"), "sell", date(2024, 1, 2))
    assert fee.commission == Decimal("5")
    assert fee.stamp_duty == Decimal("5.0000")
    assert isinstance(fee.total, Decimal)


def test_quote_and_account_snapshots_are_normalized_deterministically():
    account = AccountSnapshot(
        confirmed_cash="100000.00",
        equity="101000.00",
        positions=(ManualPosition("000001.SZ", 100, 100, "1000"),),
    )
    quote = QuoteSnapshot("000001.SZ", "10.00", source="close-feed")
    assert account.cash == Decimal("100000.00")
    assert quote.price == Decimal("10.00")
    assert account.input_hash == AccountSnapshot(
        confirmed_cash=100000,
        equity=101000,
        positions=(ManualPosition("000001.SZ", 100, 100, 1000),),
    ).input_hash
