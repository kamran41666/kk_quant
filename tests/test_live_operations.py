import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from server.models.database import Base
from server.services.live_operations import create_backup, operations_status, verify_backup
from server.services.live_readiness import register_connection, set_kill_switch
from server.services.live_readiness import create_order_draft
from server.services.paper_trading import create_account


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_operations_status_is_safe_and_reports_blocked_control():
    db = _db()
    register_connection(db, provider="ibkr", account_ref="demo", mode="sandbox",
                        credential_ref="env:IBKR_API_KEY")
    payload = operations_status(db)
    assert payload["status"] == "ok"
    assert payload["safety"]["kill_switch_active"] is True
    assert payload["safety"]["credentials_in_backup"] is False
    assert payload["counts"]["connections"] == 1


def test_backup_checksum_and_tamper_detection_without_credentials():
    db = _db()
    register_connection(db, provider="ibkr", account_ref="backup-demo", mode="sandbox",
                        credential_ref="env:IBKR_API_KEY")
    account = create_account(db, name="backup-account", initial_capital=100_000,
                             max_position_weight=1.0)
    connection = register_connection(db, provider="alpaca", account_ref="backup-draft",
                                     mode="sandbox")
    create_order_draft(
        db, connection_id=connection["id"], paper_account_id=account["id"],
        idempotency_key="backup-draft-001", code="000001.SZ", side="buy",
        quantity=100, limit_price=10, price_source="token=SECRET",
        price_as_of=datetime.now(timezone.utc).isoformat(), price_freshness="fresh",
    )
    set_kill_switch(db, active=True, reason="backup test")
    backup = create_backup(db)
    serialized = str(backup)
    assert "IBKR_API_KEY" not in serialized
    assert "token=SECRET" not in serialized
    assert "price_source" not in serialized
    assert "idempotency_key" not in serialized
    assert backup["payload"]["safety"]["credentials_included"] is False
    valid = verify_backup(backup)
    assert valid["valid"] is True
    tampered = {**backup, "payload": {**backup["payload"], "control": {"kill_switch_active": False}}}
    assert verify_backup(tampered)["valid"] is False
    with pytest.raises(ValueError, match="unsupported backup schema_version"):
        verify_backup({**backup, "payload": {**backup["payload"], "schema_version": 99}})
