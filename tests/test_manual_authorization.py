"""M4 account-specific user authorization tests."""
import pytest

from server.services.manual_authorization import (
    AuthorizationError,
    activate_on_first_fill,
    approve_authorization,
    create_authorization,
    revoke_authorization,
)
from server.services.manual_ledger import create_manual_account
from server.services.strategy_promotion import create_strategy_release, promote_release
from tests.test_strategy_promotion import _evidence


def _ready_release(db):
    release = create_strategy_release(
        db, strategy_key="auth-test", version="v1", bundle_hash="1" * 64,
        strategy_fingerprint="2" * 64, research_evidence={"source": "fresh"},
        execution_policy={"auto_submit": False}, risk_policy={"max_drawdown": "0.2"},
    )
    return promote_release(db, release.id, target_status="manual_ready", evidence=_evidence(), approved_by="operator")


def _limits(db, release_id, account_id):
    return create_authorization(
        db, release_id=release_id, account_id=account_id, capital_limit="100000",
        max_order_notional="50000", max_gross_exposure="0.9", max_single_weight="0.45",
        max_daily_items=20, max_daily_loss="2000", max_drawdown="0.2",
        revocation_policy={"on_revoke": "hold_and_reconcile"},
        valid_from="2024-01-01T00:00:00+08:00", valid_until="2025-01-01T00:00:00+08:00",
    )


def test_authorization_is_separate_from_release_and_audit_is_written(db_session):
    release = _ready_release(db_session)
    account = create_manual_account(db_session, "人工账户")
    authorization = _limits(db_session, release.id, account.id)
    assert authorization.status == "pending"
    approved = approve_authorization(db_session, authorization.id, approved_by="user")
    assert approved.status == "approved"
    active = activate_on_first_fill(db_session, approved.id, event_id="event-1", filled_at="2024-01-02T08:00:00+00:00")
    assert active.status == "active"
    revoked = revoke_authorization(db_session, active.id, actor="user", reason="manual stop")
    assert revoked.status == "revoked"
    assert db_session.query(__import__("server.models.schema", fromlist=["AuditEvent"]).AuditEvent).count() == 2


def test_missing_limit_or_second_active_authorization_is_rejected(db_session):
    release = _ready_release(db_session)
    account = create_manual_account(db_session, "账户")
    with pytest.raises(AuthorizationError, match="positive"):
        create_authorization(
            db_session, release_id=release.id, account_id=account.id, capital_limit=0,
            max_order_notional=1, max_gross_exposure="0.9", max_single_weight="0.4",
            max_daily_items=1, max_daily_loss=1, max_drawdown="0.2", revocation_policy={},
            valid_from="2024-01-01T00:00:00+00:00", valid_until="2025-01-01T00:00:00+00:00",
        )
    first = _limits(db_session, release.id, account.id)
    approve_authorization(db_session, first.id, approved_by="user")
    with pytest.raises(AuthorizationError, match="already"):
        _limits(db_session, release.id, account.id)
