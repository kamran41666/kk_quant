"""M7 cash-flow-neutral review and research-only revision tests."""
from datetime import date
from decimal import Decimal

import pytest

from server.services.manual_backup import ManualBackupError, build_manual_backup, restore_manual_backup, verify_manual_backup, write_manual_backup
from server.services.manual_ledger import create_manual_account, reconcile_account, record_cash_event, verify_ledger_hash_chain
from server.services.manual_review import (
    create_daily_review,
    queue_research_revision,
    record_corporate_action_fact,
    record_manual_valuation,
)


def _account(db_session):
    account = create_manual_account(db_session, "M7 账户", idempotency_key="m7-account-1")
    record_cash_event(db_session, account.id, idempotency_key="m7-cash-1", event_type="opening_balance", amount="100000", occurred_at="2024-01-02")
    return account


def test_valuation_neutralizes_external_cash_flow_and_review_is_idempotent(db_session):
    account = _account(db_session)
    first = record_manual_valuation(
        db_session, account_id=account.id, valuation_date="2024-01-02", cash="100000", market_value="0", total_asset="100000", idempotency_key="m7-val-1",
    )
    record_cash_event(db_session, account.id, idempotency_key="m7-cash-2", event_type="deposit", amount="10000", occurred_at="2024-01-03")
    second = record_manual_valuation(
        db_session, account_id=account.id, valuation_date="2024-01-03", cash="110000", market_value="0", total_asset="110000", idempotency_key="m7-val-2",
    )
    assert first.daily_return == Decimal("0E-10")
    assert second.external_cash_flow == Decimal("10000.00000000")
    assert second.pnl == Decimal("0E-8")
    reconcile_account(
        db_session, account.id, idempotency_key="m7-reconcile-1",
        as_of="2024-01-03T16:00:00+08:00", cash="110000", total_asset="110000", positions=[],
    )
    review = create_daily_review(db_session, account_id=account.id, review_date="2024-01-03", valuation_id=second.id)
    assert review.status == "ready"
    assert create_daily_review(db_session, account_id=account.id, review_date="2024-01-03", valuation_id=second.id).id == review.id


def test_review_without_matching_reconciliation_is_blocked(db_session):
    account = _account(db_session)
    valuation = record_manual_valuation(
        db_session, account_id=account.id, valuation_date="2024-01-02",
        cash="1", market_value="0", total_asset="1", idempotency_key="m7-unmatched-val",
    )
    review = create_daily_review(
        db_session, account_id=account.id, review_date="2024-01-02", valuation_id=valuation.id,
    )
    assert review.status == "blocked"
    assert review.reconciliation_status == "not_run"


def test_revision_and_corporate_action_are_idempotent_and_research_only(db_session):
    account = _account(db_session)
    revision = queue_research_revision(
        db_session, account_id=account.id, review_date=date(2024, 1, 3), reason_codes=["tracking_error", "tracking_error"], evidence={"actual": "0.01"},
    )
    assert queue_research_revision(
        db_session, account_id=account.id, review_date="2024-01-03", reason_codes=["tracking_error"], evidence={"actual": "0.01"},
    ).id == revision.id
    action = record_corporate_action_fact(
        db_session, account_id=account.id, code="000001.SZ", action_type="split", effective_date="2024-01-04", idempotency_key="m7-action-1", factor="2",
    )
    assert action.source == "user_reported"
    assert record_corporate_action_fact(
        db_session, account_id=account.id, code="000001.SZ", action_type="split", effective_date="2024-01-04", idempotency_key="m7-action-1", factor="2",
    ).id == action.id


def test_manual_backup_round_trip_preserves_ledger(tmp_path, db_session):
    account = _account(db_session)
    path = tmp_path / "manual-backup.json"
    result = write_manual_backup(db_session, path)
    verified = verify_manual_backup(path)
    assert result["content_hash"] == verified["content_hash"]

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine("sqlite:///:memory:")
    from server.models.database import Base
    Base.metadata.create_all(engine)
    restored_db = sessionmaker(bind=engine)()
    try:
        counts = restore_manual_backup(restored_db, path)
        assert counts["manual_account"] == 1
        restored = restored_db.get(type(account), account.id)
        assert restored is not None and restored.confirmed_cash == Decimal("100000.00000000")
        assert verify_ledger_hash_chain(restored_db, account.id)
    finally:
        restored_db.close()
        Base.metadata.drop_all(engine)


def test_backup_rejects_unimplemented_account_scope_instead_of_leaking_other_accounts(db_session):
    account = _account(db_session)
    create_manual_account(db_session, "另一个账户", idempotency_key="m7-account-2")
    with pytest.raises(ManualBackupError, match="not_implemented"):
        build_manual_backup(db_session, account_id=account.id)
