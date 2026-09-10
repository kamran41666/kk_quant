"""M5 decision -> cohorts -> persisted human plan integration tests."""
from datetime import date
from decimal import Decimal
import pytest

from quant_engine.trading.manual_protocol import AccountSnapshot, DailyDecision, ManualCohort as ProtocolCohort, QuoteSnapshot
from server.models.schema import ManualExecutionItem
from server.services.manual_cohort import CohortError, create_decision_cohorts
from server.services.manual_data_readiness import assess_manual_data_readiness
from server.services.manual_decision import create_daily_decision
from server.services.manual_ledger import create_manual_account, record_cash_event
from server.services.manual_plan_persistence import freeze_plan_ready, mark_plan_viewed, persist_execution_plan
from server.services.manual_planning import PreflightResult
from server.services.manual_planning import draft
from server.services.manual_authorization import approve_authorization
from tests.test_manual_authorization import _limits, _ready_release


def _calendar():
    class C:
        def next_trading_day(self, value):
            from datetime import timedelta
            return value + timedelta(days=1)
    return C()


def test_data_readiness_is_fail_closed():
    class DataCalendar:
        def ensure_coverage(self, start, end):
            return {"complete": False}

        def get_trading_days(self, start, end):
            return []

    result = assess_manual_data_readiness(
        signal_date=date(2024, 1, 2), data_as_of="2024-01-02T08:00:00+00:00",
        calendar=DataCalendar(), required_codes=["600000.SH"], quotes={},
    )
    assert result["ready"] is False
    assert "TRADING_CALENDAR_UNAVAILABLE" in result["reasons"]


def test_daily_decision_cohorts_and_plan_remain_separate_from_ledger(db_session):
    release = _ready_release(db_session)
    account = create_manual_account(db_session, "人工账户")
    record_cash_event(db_session, account.id, idempotency_key="opening", event_type="opening_balance", amount="100000", occurred_at="2024-01-02")
    authorization = _limits(db_session, release.id, account.id)
    approve_authorization(db_session, authorization.id, approved_by="user")
    decision_row = create_daily_decision(
        db_session, release_id=release.id, authorization_id=authorization.id,
        signal_date=date(2024, 1, 2), target_weights={"600000.SH": "0.45"},
        data_as_of="2024-01-02T08:00:00+00:00", input_facts={"factor_hash": "a"},
    )
    cohorts = create_decision_cohorts(db_session, decision_row.id, calendar=_calendar())
    assert len(cohorts) == 1
    pure_decision = DailyDecision(
        signal_date=date(2024, 1, 2), action="rebalance", target_weights={"600000.SH": Decimal("0.45")},
        release_id=release.id, authorization_id=authorization.id,
        data_as_of=decision_row.data_as_of, revision=decision_row.revision, id=decision_row.id,
    )
    pure_cohorts = [ProtocolCohort(
        id=item.id, signal_date=date.fromisoformat(item.signal_date),
        planned_entry_date=date.fromisoformat(item.planned_entry_date), planned_exit_date=date.fromisoformat(item.planned_exit_date),
        sleeve_index=item.sleeve_index, budget=item.budget,
    ) for item in cohorts]
    plan = draft(
        pure_decision, pure_cohorts,
        AccountSnapshot(confirmed_cash=Decimal("100000"), equity=Decimal("100000"), snapshot_id="snapshot-1"),
        {"600000.SH": QuoteSnapshot(code="600000.SH", price="10", as_of="2024-01-03T01:00:00+00:00")},
        execution_date=date(2024, 1, 3),
    )
    persisted = persist_execution_plan(db_session, plan, account_id=account.id, authorization_hash="f" * 64)
    assert persisted.status == "draft"
    assert db_session.query(ManualExecutionItem).count() == 1
    freeze_plan_ready(db_session, persisted.id, PreflightResult(True))
    mark_plan_viewed(db_session, persisted.id)
    assert db_session.get(type(persisted), persisted.id).status == "viewed"
    assert db_session.get(type(account), account.id).confirmed_cash == Decimal("100000.00000000")


def test_daily_decisions_alternate_one_sleeve_and_block_reuse_while_occupied(db_session):
    release = _ready_release(db_session)
    account = create_manual_account(db_session, "轮换账户")
    record_cash_event(db_session, account.id, idempotency_key="rotation-opening", event_type="opening_balance", amount="100000", occurred_at="2024-01-02")
    authorization = _limits(db_session, release.id, account.id)
    approve_authorization(db_session, authorization.id, approved_by="user")
    decisions = []
    for offset, day in enumerate((date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4))):
        decisions.append(create_daily_decision(
            db_session, release_id=release.id, authorization_id=authorization.id,
            signal_date=day, target_weights={"600000.SH": "0.45"},
            data_as_of=f"{day.isoformat()}T08:00:00+00:00", input_facts={"factor_hash": str(offset)},
        ))
    assert create_decision_cohorts(db_session, decisions[0].id, calendar=_calendar())[0].sleeve_index == 0
    assert create_decision_cohorts(db_session, decisions[1].id, calendar=_calendar())[0].sleeve_index == 1
    with pytest.raises(CohortError, match="occupied"):
        create_decision_cohorts(db_session, decisions[2].id, calendar=_calendar())
