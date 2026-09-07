import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from quant_engine.trading.gateway import BrokerCapabilities
from server.api.live import BrokerConnectionRequest
from server.models.database import Base
from server.models.schema import PaperOrder
from server.services import live_readiness
from server.services.live_readiness import (
    capabilities,
    cancel_draft,
    confirm_draft,
    create_order_draft,
    export_audit_events,
    list_audit_events,
    register_connection,
    rotate_connection_credential,
    set_kill_switch,
    test_connection as probe_connection,
)
from server.services.paper_trading import create_account


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_live_capabilities_are_fail_closed_by_default(monkeypatch):
    db = _db()
    monkeypatch.setattr(live_readiness, "_adapter_capabilities", {})
    payload = capabilities(db)
    assert payload["can_submit_live"] is False
    assert payload["paper_only"] is True
    assert payload["kill_switch_active"] is True
    assert "broker_adapter_not_configured" in payload["blocked_reasons"]
    assert "live_trading_disabled_by_config" in payload["blocked_reasons"]


def test_connection_accepts_only_opaque_credential_reference(monkeypatch):
    db = _db()
    monkeypatch.setattr(live_readiness, "_adapter_capabilities", {})
    with pytest.raises(ValueError, match="secret values"):
        register_connection(db, provider="ibkr", account_ref="demo", mode="live", credential_ref="actual-secret")
    connection = register_connection(db, provider="ibkr", account_ref="demo", mode="sandbox", credential_ref="env:IBKR_API_KEY")
    assert connection["credential_ref_configured"] is True
    assert "IBKR_API_KEY" not in str(connection)
    checked = probe_connection(db, connection["id"])
    assert checked["status"] == "unavailable"
    assert checked["enabled"] is False
    audit = list_audit_events(db)
    assert "IBKR_API_KEY" not in str(audit)


def test_credential_reference_rotation_invalidates_connection_until_retest(monkeypatch):
    db = _db()
    monkeypatch.setattr(live_readiness, "_adapter_capabilities", {})
    connection = register_connection(db, provider="ibkr", account_ref="rotate-demo",
                                     mode="sandbox", credential_ref="env:OLD_REF")
    rotated = rotate_connection_credential(db, connection["id"], credential_ref="keychain:NEW_REF")
    assert rotated["credential_ref_configured"] is True
    assert rotated["enabled"] is False
    assert rotated["status"] == "disabled"
    assert rotated["last_error"] == "credential_reference_changed_retest_required"
    audit = list_audit_events(db)
    assert any(event["action"] == "broker_connection.credential_ref.rotate" for event in audit)
    assert "NEW_REF" not in str(audit)
    with pytest.raises(ValueError, match="secret values"):
        rotate_connection_credential(db, connection["id"], credential_ref="raw-secret")


def test_order_draft_runs_server_side_checks_and_never_creates_order(monkeypatch):
    db = _db()
    monkeypatch.setattr(live_readiness, "_adapter_capabilities", {})
    account = create_account(db, name="shadow", initial_capital=100_000, max_position_weight=1.0)
    connection = register_connection(db, provider="ibkr", account_ref="demo", mode="sandbox", credential_ref="env:IBKR_API_KEY")
    draft = create_order_draft(
        db, connection_id=connection["id"], paper_account_id=account["id"],
        idempotency_key="live-draft-001", code="000001.SZ", side="buy",
        quantity=100, limit_price=10, price_source="manual_input",
        price_as_of=None, price_freshness="manual",
    )
    assert draft["status"] == "blocked"
    assert draft["risk_status"] == "blocked"
    assert "connection_not_enabled" in draft["risk_reason"]
    assert "broker_adapter_not_configured" in draft["risk_reason"]
    assert draft["estimated_fee"] == 5.0
    assert db.query(PaperOrder).count() == 0
    replay = create_order_draft(
        db, connection_id=connection["id"], paper_account_id=account["id"],
        idempotency_key="live-draft-001", code="000001.SZ", side="buy",
        quantity=100, limit_price=10, price_source="manual_input",
        price_as_of=None, price_freshness="manual",
    )
    assert replay["id"] == draft["id"]


def test_draft_rejects_stale_quotes_and_confirmation_is_blocked(monkeypatch):
    db = _db()
    monkeypatch.setattr(live_readiness, "_adapter_capabilities", {})
    account = create_account(db, name="shadow-stale", initial_capital=100_000, max_position_weight=1.0)
    connection = register_connection(db, provider="ibkr", account_ref="stale", mode="sandbox", credential_ref="env:IBKR_API_KEY")
    with pytest.raises(ValueError, match="quote_stale_or_unknown"):
        create_order_draft(
            db, connection_id=connection["id"], paper_account_id=account["id"],
            idempotency_key="live-draft-stale", code="000001.SZ", side="buy",
            quantity=100, limit_price=10, price_source="feed",
            price_as_of="2020-01-01T00:00:00+00:00", price_freshness="fresh",
        )
    draft = create_order_draft(
        db, connection_id=connection["id"], paper_account_id=account["id"],
        idempotency_key="live-draft-blocked", code="000001.SZ", side="buy",
        quantity=100, limit_price=10, price_source="manual_input",
        price_as_of=None, price_freshness="manual",
    )
    with pytest.raises(ValueError, match="connection_not_enabled|live_order_draft_not_confirmable"):
        confirm_draft(db, draft["id"], confirmation_phrase="CONFIRM LIVE ORDER")
    cancelled = cancel_draft(db, draft["id"])
    assert cancelled["status"] == "cancelled"


def test_kill_switch_requires_explicit_unlock_phrase_and_remains_not_live(monkeypatch):
    db = _db()
    monkeypatch.setattr(live_readiness, "_adapter_capabilities", {"ibkr": BrokerCapabilities("ibkr", "IBKR", supports_sandbox=True)})
    with pytest.raises(ValueError, match="confirmation_phrase_required"):
        set_kill_switch(db, active=False, reason="testing")
    unlocked = set_kill_switch(db, active=False, reason="sandbox-only test", confirmation_phrase="UNLOCK LIVE TRADING")
    assert unlocked["kill_switch_active"] is False
    assert unlocked["can_submit_live"] is False
    assert unlocked["live_trading_enabled"] is False
    locked = set_kill_switch(db, active=True, reason="restore safety")
    assert locked["kill_switch_active"] is True


def test_live_request_models_forbid_unexpected_fields():
    with pytest.raises(Exception):
        BrokerConnectionRequest.model_validate({"provider": "ibkr", "account_ref": "demo", "secret": "do-not-accept"})


def test_audit_redacts_secret_shaped_free_text_at_write_boundary(monkeypatch):
    db = _db()
    monkeypatch.setattr(live_readiness, "_adapter_capabilities", {})
    set_kill_switch(db, active=True, reason="token=SUPER_SECRET_VALUE")
    events = list_audit_events(db)
    assert events
    serialized = str(events)
    assert "SUPER_SECRET_VALUE" not in serialized
    assert "token=[REDACTED]" not in serialized
    assert events[0]["details"]["reason_present"] is True
    assert live_readiness.control_status(db)["reason"] == "user_reason_provided"


def test_audit_export_uses_redacted_view_and_explicit_format():
    db = _db()
    set_kill_switch(db, active=True, reason="token=SUPER_SECRET_VALUE")
    json_body, json_type = export_audit_events(db, format="json")
    csv_body, csv_type = export_audit_events(db, format="csv")
    assert json_type.startswith("application/json")
    assert csv_type.startswith("text/csv")
    assert "SUPER_SECRET_VALUE" not in json_body
    assert "SUPER_SECRET_VALUE" not in csv_body
    assert "action" in csv_body and "outcome" in csv_body
    with pytest.raises(ValueError, match="format must be json or csv"):
        export_audit_events(db, format="xml")
