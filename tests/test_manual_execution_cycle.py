"""M9 confirmation, first-fill activation and manual ledger integration tests."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from server.models.schema import ManualExecutionItem, ManualExecutionPlan
from server.services.manual_authorization import approve_authorization, create_authorization
from server.services.manual_execution_cycle import (
    ManualExecutionCycleError,
    confirm_plan_item,
    record_confirmed_fill,
)
from server.services.manual_ledger import create_manual_account, get_manual_state, record_cash_event
from server.services.manual_plan_persistence import mark_plan_viewed
from server.services.strategy_promotion import create_strategy_release


class Calendar:
    def next_trading_day(self, value: date) -> date:
        result = value + timedelta(days=1)
        while result.weekday() >= 5:
            result += timedelta(days=1)
        return result


def test_first_fill_requires_confirmation_and_activates_authorization(db_session):
    account = create_manual_account(db_session, "M9 账户", idempotency_key="m9-account-1")
    record_cash_event(db_session, account.id, idempotency_key="m9-cash-1", event_type="opening_balance", amount="100000", occurred_at="2024-01-02")
    release = create_strategy_release(
        db_session, strategy_key="m9-strategy", version="v1", bundle_hash="a" * 64,
        strategy_fingerprint="b" * 64, research_evidence={}, execution_policy={"auto_submit": False}, risk_policy={"paper_only": True},
    )
    release.status = "manual_ready"
    db_session.commit()
    authorization = create_authorization(
        db_session, release_id=release.id, account_id=account.id, capital_limit="50000",
        max_order_notional="20000", max_gross_exposure="0.9", max_single_weight="0.45",
        max_daily_items=10, max_daily_loss="1000", max_drawdown="0.2",
        revocation_policy={"manual": True}, valid_from="2024-01-01T00:00:00+00:00", valid_until="2025-01-01T00:00:00+00:00",
    )
    approve_authorization(db_session, authorization.id, approved_by="test-user")
    plan = ManualExecutionPlan(
        id="m9-plan-1", idempotency_key="m9-plan-key", decision_id="decision-m9", authorization_id=authorization.id,
        account_id=account.id, execution_date="2024-01-02", execution_session="open", plan_type="entry", version=1,
        input_hash="c" * 64, plan_hash="d" * 64, quote_snapshot_hash="e" * 64, authorization_hash="f" * 64,
        trading_rule_id="a-share-main-board", trading_rule_version="v1", trading_rule_effective_date="2024-01-01",
        status="ready", cash_before=Decimal("100000"), expected_cash_after=Decimal("90000"), expected_fees=Decimal("0"),
    )
    item = ManualExecutionItem(
        id="m9-item-1", idempotency_key="m9-item-key", plan_id=plan.id, code="600000.SH", side="buy", phase="buy",
        pre_quantity=Decimal("0"), available_quantity=Decimal("0"), available_cash_before=Decimal("100000"),
        target_quantity=Decimal("100"), target_weight=Decimal("0.1"), planned_quantity=Decimal("100"), cash_required=Decimal("1000"),
        reference_price=Decimal("10"), price_source="user_reported", expected_notional=Decimal("1000"),
        estimated_commission=Decimal("0"), estimated_tax=Decimal("0"), estimated_other_fee=Decimal("0"), order_sequence=1,
        reason_codes="[]", status="planned", confirmed_quantity=Decimal("0"),
    )
    db_session.add_all([plan, item])
    db_session.commit()
    mark_plan_viewed(db_session, plan.id)
    with pytest.raises(ManualExecutionCycleError, match="confirmation"):
        record_confirmed_fill(
            db_session, plan_id=plan.id, item_id=item.id, client_event_id="m9-fill-before-confirm",
            quantity=100, price="10", traded_at="2024-01-02", calendar=Calendar(),
        )
    confirmation = confirm_plan_item(db_session, plan_id=plan.id, item_id=item.id, actor="test-user")
    assert confirmation.actor == "test-user"
    fill = record_confirmed_fill(
        db_session, plan_id=plan.id, item_id=item.id, client_event_id="m9-fill-1",
        quantity=100, price="10", traded_at="2024-01-02", calendar=Calendar(),
    )
    db_session.refresh(item)
    db_session.refresh(authorization)
    assert fill.source == "user_reported"
    assert item.status == "filled"
    assert plan.status == "completed"
    assert authorization.status == "active"
    assert get_manual_state(db_session, account.id)["positions"]["600000.SH"]["quantity"] == Decimal("100.00000000")
