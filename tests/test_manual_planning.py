from datetime import date
from dataclasses import replace
from decimal import Decimal

import pytest

from quant_engine.trading.manual_protocol import (
    AccountSnapshot,
    DailyDecision,
    ManualAuthorizationLimits,
    ManualCohort,
    ManualPosition,
    PlanStatus,
    QuoteSnapshot,
)
from server.services.manual_planning import draft, preflight, refresh, revise


def _calendar_dates():
    return (
        date(2024, 1, 5),
        date(2024, 1, 8),
        date(2024, 1, 9),
        date(2024, 1, 10),
    )


def _decision(action="rebalance", target_weights=None):
    return DailyDecision(
        signal_date=date(2024, 1, 5),
        action=action,
        target_weights=(
            {"000001.SZ": Decimal("0.45"), "600000.SH": Decimal("0.45")}
            if target_weights is None else target_weights
        ),
        authorization_id="auth-1",
    )


def _cohort():
    return ManualCohort(
        id="cohort-a",
        signal_date=date(2024, 1, 5),
        planned_entry_date=date(2024, 1, 8),
        planned_exit_date=date(2024, 1, 9),
        sleeve_index=0,
        budget=Decimal("0.45"),
    )


def _quotes():
    return {
        "000001.SZ": QuoteSnapshot("000001.SZ", "10", source="open-feed", as_of="2024-01-08T09:30:00+08:00"),
        "600000.SH": QuoteSnapshot("600000.SH", "20", source="open-feed", as_of="2024-01-08T09:30:00+08:00"),
    }


def test_draft_sells_before_buys_and_uses_confirmed_cash_only():
    account = AccountSnapshot(
        confirmed_cash=Decimal("1000"),
        equity=Decimal("10000"),
        positions=(ManualPosition("000001.SZ", 450, 50, market_value=Decimal("4500"), cohort_id="old"),),
    )
    decision = _decision(target_weights={"000001.SZ": Decimal("0.45"), "600000.SH": Decimal("0.45")})
    plan = draft(decision, [_cohort()], account, _quotes(), execution_date=date(2024, 1, 8))
    assert plan.status == PlanStatus.BLOCKED.value
    assert plan.blocked_reason == "confirmed_cash_insufficient"
    assert plan.items == ()
    assert plan.expected_cash_after == account.confirmed_cash


def test_draft_buy_rounding_and_fee_estimates_are_separate_from_actual_fees():
    account = AccountSnapshot(confirmed_cash=100_000, equity=100_000)
    plan = draft(
        _decision(target_weights={"000001.SZ": Decimal("0.45")}),
        [_cohort()], account, _quotes(), execution_date=date(2024, 1, 8),
    )
    assert plan.status == PlanStatus.DRAFT.value
    item = plan.buy_items[0]
    assert item.planned_quantity == 4_500
    assert item.cash_required == Decimal("45011.25")
    assert item.estimated_commission == Decimal("11.25000")
    assert item.estimated_tax == Decimal("0")
    assert not hasattr(item, "actual_fee")
    assert plan.sell_items == ()


def test_full_odd_lot_sell_is_allowed_but_buy_is_lot_aligned():
    account = AccountSnapshot(
        confirmed_cash=100,
        equity=1000,
        positions=(ManualPosition("000001.SZ", 155, 155, market_value=Decimal("1550"), cohort_id="old"),),
    )
    plan = draft(
        _decision(action="flat", target_weights={}),
        [_cohort()], account,
        {"000001.SZ": QuoteSnapshot("000001.SZ", 10, source="close")},
        execution_date=date(2024, 1, 9),
    )
    assert plan.status == PlanStatus.DRAFT.value
    assert plan.sell_items[0].planned_quantity == 155
    assert plan.sell_items[0].available_quantity == 155


def test_blocked_and_reconcile_do_not_create_new_buys_but_keep_due_exit():
    account = AccountSnapshot(
        confirmed_cash=100_000,
        equity=100_000,
        positions=(ManualPosition("000001.SZ", 100, 100, market_value=1000, cohort_id="cohort-a"),),
    )
    for action in ("blocked", "reconcile"):
        plan = draft(
            _decision(action=action), [_cohort()], account, _quotes(), execution_date=date(2024, 1, 9),
        )
        # The decision blocks new entries, while a due exit remains executable.
        assert plan.status == PlanStatus.DRAFT.value
        assert plan.buy_items == ()
        assert plan.sell_items[0].planned_quantity == 100


def test_due_exit_is_independent_of_new_signal_and_missing_sell_quantity_blocks():
    account = AccountSnapshot(
        confirmed_cash=100_000,
        equity=100_000,
        positions=(ManualPosition("000001.SZ", 100, 0, market_value=1000, cohort_id="cohort-a"),),
    )
    plan = draft(
        _decision(target_weights={}), [_cohort()], account,
        {"000001.SZ": QuoteSnapshot("000001.SZ", 10, source="close")},
        execution_date=date(2024, 1, 9),
    )
    assert plan.status == PlanStatus.BLOCKED.value
    assert "sell_quantity_unavailable:000001.SZ" in plan.blocked_reason
    assert plan.items == ()


def test_refresh_only_accepts_draft_and_changes_quote_hash_deterministically():
    account = AccountSnapshot(confirmed_cash=100_000, equity=100_000)
    plan = draft(
        _decision(target_weights={"000001.SZ": Decimal("0.45")}), [_cohort()], account,
        _quotes(), execution_date=date(2024, 1, 8),
    )
    refreshed = refresh(
        plan,
        {"000001.SZ": QuoteSnapshot("000001.SZ", 11, source="new-feed")},
        account,
    )
    assert refreshed.status == PlanStatus.DRAFT.value
    assert refreshed.items[0].reference_price == Decimal("11")
    assert refreshed.plan_hash != plan.plan_hash
    with pytest.raises(ValueError, match="only draft"):
        refresh(replace(plan, status="ready"), _quotes(), account)


def test_revise_creates_new_version_and_does_not_mutate_ready_plan():
    account = AccountSnapshot(confirmed_cash=100_000, equity=100_000)
    draft_plan = draft(
        _decision(target_weights={"000001.SZ": Decimal("0.45")}), [_cohort()], account,
        _quotes(), execution_date=date(2024, 1, 8),
    )
    ready_plan = replace(draft_plan, status="ready", plan_hash="")
    revised = revise(ready_plan, _quotes(), account, "sell fill released cash")
    assert ready_plan.status == "ready"
    assert revised.version == ready_plan.version + 1
    assert revised.supersedes_plan_id == ready_plan.id
    assert revised.id != ready_plan.id


def test_preflight_checks_limit_up_down_cash_and_authorization():
    account = AccountSnapshot(
        confirmed_cash=100_000,
        equity=10_000,
        positions=(ManualPosition("000001.SZ", 100, 100, market_value=1000),),
    )
    plan = draft(
        _decision(target_weights={"600000.SH": Decimal("0.45")}), [_cohort()], account,
        {"600000.SH": QuoteSnapshot("600000.SH", 20, source="open", limit_up=True)},
        execution_date=date(2024, 1, 8),
    )
    result = preflight(
        plan,
        ManualAuthorizationLimits(capital_limit=10_000, max_order_notional=5_000, max_daily_items=2),
        account,
        {"600000.SH": QuoteSnapshot("600000.SH", 20, source="open", limit_up=True)},
    )
    assert result.allowed is False
    assert any("limit_up_buy" in item for item in result.reason_codes)
    with pytest.raises(ValueError, match="only ready or viewed"):
        revise(plan, _quotes(), account, "not frozen")


def test_preflight_sell_at_limit_down_is_blocked_without_deleting_exit():
    account = AccountSnapshot(
        confirmed_cash=1000,
        equity=10_000,
        positions=(ManualPosition("000001.SZ", 100, 100, market_value=1000, cohort_id="cohort-a"),),
    )
    plan = draft(
        _decision(action="flat", target_weights={}), [_cohort()], account,
        {"000001.SZ": QuoteSnapshot("000001.SZ", 10, source="close")},
        execution_date=date(2024, 1, 9),
    )
    result = preflight(
        plan,
        ManualAuthorizationLimits(capital_limit=10_000),
        account,
        {"000001.SZ": QuoteSnapshot("000001.SZ", 10, source="close", limit_down=True)},
    )
    assert result.allowed is False
    assert "000001.SZ:limit_down_sell" in result.reason_codes
    assert plan.sell_items[0].planned_quantity == 100


def test_same_code_due_exit_only_sells_the_due_cohort_and_new_sleeve_can_enter():
    old = _cohort()
    new = ManualCohort(
        id="cohort-b", signal_date=date(2024, 1, 8),
        planned_entry_date=date(2024, 1, 9), planned_exit_date=date(2024, 1, 10),
        sleeve_index=1, budget=Decimal("0.45"),
    )
    account = AccountSnapshot(
        confirmed_cash=10_000, equity=10_000,
        positions=(
            ManualPosition("000001.SZ", 100, 100, market_value=1000, cohort_id="cohort-a"),
            ManualPosition("000001.SZ", 100, 100, market_value=1000, cohort_id="cohort-b"),
        ),
    )
    quote = {"000001.SZ": QuoteSnapshot("000001.SZ", 10, source="verified", as_of="2024-01-09T15:00:00+08:00")}
    close_plan = draft(
        _decision(action="hold", target_weights={"000001.SZ": Decimal("0.45")}),
        [old, new], account, quote, execution_date=date(2024, 1, 9), execution_session="close",
    )
    assert [(item.cohort_id, item.planned_quantity) for item in close_plan.sell_items] == [("cohort-a", 100)]
    assert close_plan.buy_items == ()
    open_plan = draft(
        _decision(action="hold", target_weights={"000001.SZ": Decimal("0.45")}),
        [old, new], account, quote, execution_date=date(2024, 1, 9), execution_session="open",
    )
    assert open_plan.sell_items == ()
    assert open_plan.buy_items[0].cohort_id == "cohort-b"
    assert open_plan.buy_items[0].planned_quantity == 300


def test_preflight_requires_same_fresh_quote_and_checks_resulting_single_weight():
    account = AccountSnapshot(
        confirmed_cash=10_000, equity=10_000,
        positions=(ManualPosition("000001.SZ", 200, 200, market_value=2000),),
    )
    quote = {"000001.SZ": QuoteSnapshot("000001.SZ", 10, source="verified", as_of="2024-01-08T09:30:00+08:00")}
    plan = draft(
        _decision(target_weights={"000001.SZ": Decimal("0.20")}), [_cohort()],
        AccountSnapshot(confirmed_cash=10_000, equity=10_000), quote,
        execution_date=date(2024, 1, 8),
    )
    limits = ManualAuthorizationLimits(
        capital_limit=10_000, max_single_weight=Decimal("0.30"), max_gross_exposure=Decimal("0.90"),
    )
    changed = {"000001.SZ": QuoteSnapshot("000001.SZ", 20, source="verified", as_of="2024-01-08T09:31:00+08:00")}
    assert "quote_snapshot_changed_requires_revision" in preflight(plan, limits, account, changed).reason_codes
    result = preflight(plan, limits, account, quote)
    assert "000001.SZ:max_single_weight_exceeded" in result.reason_codes
