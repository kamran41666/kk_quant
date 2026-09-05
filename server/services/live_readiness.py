"""Fail-closed Phase 3 live-trading readiness services.

This module deliberately contains no broker SDK and never accepts a secret in
an API request.  It provides the durable control plane around a future
``LiveBrokerGateway``: opaque credential references, safety controls, a
server-authoritative pre-trade draft, and an append-only audit trail.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from quant_engine.trading.gateway import BrokerCapabilities, OrderIntent, RiskEngine, RiskLimits
from server.config import settings
from server.models.schema import (
    AuditEvent,
    BrokerConnection,
    LiveControl,
    LiveOrderDraft,
    PaperAccount,
    PaperAccountPosition,
    PaperLot,
    PaperValuation,
)
from server.services.paper_trading import (
    COMMISSION_RATE,
    MIN_COMMISSION,
    STAMP_DUTY_RATE,
    _validate_quote,
)

_service_lock = threading.RLock()
_credential_ref_pattern = re.compile(r"^(?:env|keychain):[A-Za-z0-9_.:/-]{1,120}$")
_sensitive_key_pattern = re.compile(
    r"(?:credential|api[_-]?key|access[_-]?key|secret|password|passwd|token|"
    r"authorization|cookie|private[_-]?key|signature)",
    re.IGNORECASE,
)
_sensitive_text_pattern = re.compile(
    r"(?:bearer\s+|(?:api[_-]?key|access[_-]?key|secret|password|passwd|"
    r"token|authorization|cookie|private[_-]?key|signature)\s*[:=]\s*)"
    r"[^\s,;]+",
    re.IGNORECASE,
)
# No adapter is registered until a user selects a broker and supplies its
# official sandbox/API contract.  Tests and a future plugin may register a
# reviewed capability without changing this safety default.
_adapter_capabilities: dict[str, BrokerCapabilities] = {}
_LIVE_EXECUTION_IMPLEMENTED = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def register_adapter_capability(capabilities: BrokerCapabilities) -> None:
    """Register metadata for a reviewed adapter (no credentials are stored)."""
    if not capabilities.provider.strip():
        raise ValueError("provider is required")
    _adapter_capabilities[capabilities.provider.lower()] = capabilities


def _redact_audit_details(value: Any, *, key: Optional[str] = None) -> Any:
    """Recursively remove secrets before an audit row is constructed.

    Audit events are durable and intentionally append-only, so redaction must
    happen at this single write boundary rather than relying on each caller.
    Free-form user reasons are not persisted verbatim; known secret-shaped
    values are replaced even when they arrive under an otherwise harmless key.
    """
    if key and _sensitive_key_pattern.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _redact_audit_details(item_value, key=str(item_key))
                for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_audit_details(item) for item in value]
    if isinstance(value, str):
        return _sensitive_text_pattern.sub("[REDACTED]", value)
    return value


def _control(db: Session) -> LiveControl:
    row = db.query(LiveControl).filter(LiveControl.id == "global").first()
    if row:
        return row
    row = LiveControl(id="global", live_enabled=False, kill_switch_active=True,
                      reason="live_trading_disabled_by_default", updated_by="system")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _audit(db: Session, *, action: str, resource_type: str,
           resource_id: Optional[str], outcome: str,
           details: Optional[dict[str, Any]] = None, actor: str = "local-user") -> None:
    # Redact at the persistence boundary.  Never trust a future adapter or
    # caller to sanitize a free-form error/reason before it reaches SQLite.
    safe_details = _redact_audit_details(details or {})
    db.add(AuditEvent(actor=actor, action=action, resource_type=resource_type,
                      resource_id=resource_id, outcome=outcome,
                      details=json.dumps(safe_details, ensure_ascii=False, sort_keys=True)))


def _connection_dict(row: BrokerConnection) -> dict[str, Any]:
    return {
        "id": row.id,
        "provider": row.provider,
        "account_ref": row.account_ref,
        "mode": row.mode,
        "status": row.status,
        "enabled": row.enabled,
        "credential_ref_configured": bool(row.credential_ref),
        "last_error": row.last_error,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "capabilities": _adapter_capabilities.get(row.provider.lower()).__dict__ if row.provider.lower() in _adapter_capabilities else None,
    }


def register_connection(db: Session, *, provider: str, account_ref: str,
                        mode: str = "sandbox", credential_ref: Optional[str] = None) -> dict[str, Any]:
    provider = provider.strip().lower()
    account_ref = account_ref.strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,79}", provider):
        raise ValueError("provider must be a lowercase identifier")
    if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,120}", account_ref):
        raise ValueError("account_ref contains unsupported characters")
    if mode not in {"sandbox", "live"}:
        raise ValueError("mode must be sandbox or live")
    if credential_ref is not None and not _credential_ref_pattern.fullmatch(credential_ref.strip()):
        raise ValueError("credential_ref must be an env: or keychain: reference; secret values are not accepted")
    if mode == "live" and not credential_ref:
        raise ValueError("live connection requires an opaque credential_ref")
    with _service_lock:
        if db.query(BrokerConnection).filter(BrokerConnection.provider == provider,
                                              BrokerConnection.account_ref == account_ref).first():
            raise ValueError("broker connection already exists")
        row = BrokerConnection(provider=provider, account_ref=account_ref,
                               mode=mode, status="disabled", enabled=False,
                               credential_ref=credential_ref.strip() if credential_ref else None,
                               last_error="broker_adapter_not_configured")
        db.add(row)
        try:
            db.flush()
            _audit(db, action="broker_connection.register", resource_type="broker_connection",
                   resource_id=row.id, outcome="accepted",
                   details={"provider": provider, "account_ref": account_ref, "mode": mode,
                            "credential_ref_configured": bool(credential_ref)})
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ValueError("broker connection already exists") from exc
        db.refresh(row)
        return _connection_dict(row)


def list_connections(db: Session) -> list[dict[str, Any]]:
    return [_connection_dict(row) for row in db.query(BrokerConnection).order_by(BrokerConnection.created_at.desc()).all()]


def _capability_payload(db: Session) -> dict[str, Any]:
    control = _control(db)
    registered = db.query(BrokerConnection).filter(BrokerConnection.enabled.is_(True)).count()
    reasons: list[str] = []
    if not settings.live_trading_enabled:
        reasons.append("live_trading_disabled_by_config")
    if control.kill_switch_active:
        reasons.append("kill_switch_active")
    if not _adapter_capabilities:
        reasons.append("broker_adapter_not_configured")
    if registered == 0:
        reasons.append("no_enabled_broker_connection")
    if not _LIVE_EXECUTION_IMPLEMENTED:
        reasons.append("live_execution_not_implemented")
    return {
        "phase": "3A",
        "live_trading_enabled": settings.live_trading_enabled,
        "kill_switch_active": control.kill_switch_active,
        "can_submit_live": not reasons,
        "blocked_reasons": reasons,
        "available_adapters": [cap.__dict__ for cap in _adapter_capabilities.values()],
        "registered_connections": db.query(BrokerConnection).count(),
        "enabled_connections": registered,
        "paper_only": bool(reasons),
    }


def capabilities(db: Session) -> dict[str, Any]:
    return _capability_payload(db)


def control_status(db: Session) -> dict[str, Any]:
    control = _control(db)
    return {**_capability_payload(db), "reason": control.reason,
            "updated_by": control.updated_by, "updated_at": control.updated_at}


def set_kill_switch(db: Session, *, active: bool, reason: str,
                    confirmation_phrase: Optional[str] = None,
                    actor: str = "local-user") -> dict[str, Any]:
    reason = reason.strip()
    if not reason:
        raise ValueError("reason is required")
    if not active and confirmation_phrase != "UNLOCK LIVE TRADING":
        raise ValueError("confirmation_phrase_required")
    with _service_lock:
        control = _control(db)
        control.kill_switch_active = active
        # Live mode is never enabled by this endpoint.  A separate, reviewed
        # deployment gate must set it after broker verification.
        control.live_enabled = False
        # Do not persist arbitrary user text in the control row.  It is only a
        # confirmation that an operator supplied a reason; the text could be
        # an API key pasted by mistake and must never reach SQLite.
        control.reason = "user_reason_provided"
        control.updated_by = actor
        control.updated_at = _now()
        _audit(db, action="live_control.kill_switch", resource_type="live_control",
               resource_id=control.id, outcome="activated" if active else "deactivated",
               details={"active": active, "reason_present": True}, actor=actor)
        db.commit()
        return control_status(db)


def test_connection(db: Session, connection_id: str) -> dict[str, Any]:
    row = db.query(BrokerConnection).filter(BrokerConnection.id == connection_id).first()
    if not row:
        raise KeyError("broker connection not found")
    capability = _adapter_capabilities.get(row.provider.lower())
    if capability is None:
        row.status, row.enabled, row.last_error = "unavailable", False, "broker_adapter_not_configured"
        row.updated_at = _now()
        _audit(db, action="broker_connection.test", resource_type="broker_connection",
               resource_id=row.id, outcome="blocked",
               details={"provider": row.provider, "reason": row.last_error})
        db.commit()
        return _connection_dict(row)
    # A future adapter factory will perform the network handshake.  Capability
    # metadata alone is intentionally insufficient to mark a connection ready.
    row.status, row.enabled, row.last_error = "unavailable", False, "adapter_health_check_not_implemented"
    row.updated_at = _now()
    _audit(db, action="broker_connection.test", resource_type="broker_connection",
           resource_id=row.id, outcome="blocked",
           details={"provider": row.provider, "reason": row.last_error})
    db.commit()
    return _connection_dict(row)


def enable_connection(db: Session, connection_id: str) -> dict[str, Any]:
    row = db.query(BrokerConnection).filter(BrokerConnection.id == connection_id).first()
    if not row:
        raise KeyError("broker connection not found")
    capability = _adapter_capabilities.get(row.provider.lower())
    if capability is None:
        _audit(db, action="broker_connection.enable", resource_type="broker_connection",
               resource_id=row.id, outcome="blocked",
               details={"reason": "broker_adapter_not_configured"})
        db.commit()
        raise ValueError("broker_adapter_not_configured")
    raise ValueError("adapter_health_check_required")


def _draft_dict(row: LiveOrderDraft) -> dict[str, Any]:
    return {"id": row.id, "connection_id": row.connection_id,
            "paper_account_id": row.paper_account_id,
            "idempotency_key": row.idempotency_key, "code": row.code,
            "side": row.side, "quantity": row.quantity,
            "limit_price": row.limit_price, "price_source": row.price_source,
            "price_as_of": row.price_as_of, "price_freshness": row.price_freshness,
            "notional": row.notional, "estimated_fee": row.estimated_fee,
            "cash_before": row.cash_before, "cash_after": row.cash_after,
            "risk_status": row.risk_status, "risk_reason": row.risk_reason,
            "status": row.status, "confirmed_at": row.confirmed_at,
            "created_at": row.created_at, "updated_at": row.updated_at}


def _settled_shares(db: Session, account_id: str, code: str) -> int:
    position = db.query(PaperAccountPosition).filter(PaperAccountPosition.account_id == account_id,
                                                      PaperAccountPosition.code == code).first()
    if not position:
        return 0
    lots = db.query(PaperLot).filter(PaperLot.account_id == account_id,
                                     PaperLot.code == code,
                                     PaperLot.remaining_quantity > 0).all()
    if not lots:
        return position.shares
    today = datetime.now().date().isoformat()
    return sum(lot.remaining_quantity for lot in lots if lot.unlock_date <= today)


def create_order_draft(db: Session, *, connection_id: str, paper_account_id: str,
                       idempotency_key: str, code: str, side: str,
                       quantity: int, limit_price: float,
                       price_source: str, price_as_of: Optional[str],
                       price_freshness: str) -> dict[str, Any]:
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
        raise ValueError("code must be a canonical A-share code")
    if side not in {"buy", "sell"} or quantity <= 0 or quantity % 100:
        raise ValueError("side and quantity are invalid; use positive 100-share lots")
    if not idempotency_key or len(idempotency_key) < 8:
        raise ValueError("idempotency_key must be at least 8 characters")
    _validate_quote(price=limit_price, price_source=price_source,
                    price_as_of=price_as_of, price_freshness=price_freshness)
    connection = db.query(BrokerConnection).filter(BrokerConnection.id == connection_id).first()
    if not connection:
        raise KeyError("broker connection not found")
    account = db.query(PaperAccount).filter(PaperAccount.id == paper_account_id).first()
    if not account:
        raise KeyError("paper account not found")
    existing = db.query(LiveOrderDraft).filter(LiveOrderDraft.idempotency_key == idempotency_key).first()
    if existing:
        same = (existing.connection_id == connection_id and existing.paper_account_id == paper_account_id
                and existing.code == code and existing.side == side
                and existing.quantity == quantity and abs(existing.limit_price - limit_price) <= 1e-9)
        if not same:
            raise ValueError("idempotency_key already belongs to a different live draft")
        return _draft_dict(existing)

    positions = db.query(PaperAccountPosition).filter(PaperAccountPosition.account_id == paper_account_id).all()
    position = next((item for item in positions if item.code == code), None)
    equity = account.cash + sum(item.market_value for item in positions)
    latest = (db.query(PaperValuation).filter(PaperValuation.account_id == paper_account_id)
              .order_by(PaperValuation.valuation_date.desc()).first())
    daily_return = latest.daily_return if latest else 0.0
    current_position_value = position.shares * limit_price if position else 0.0
    risk = RiskEngine(RiskLimits(account.max_order_notional, account.max_position_weight, account.max_daily_loss))
    intent = OrderIntent(idempotency_key, code, side, quantity, limit_price)
    reasons: list[str] = []
    risk_reason = risk.check(intent, reference_price=limit_price, equity=equity,
                             current_position_value=current_position_value,
                             daily_return=daily_return)
    if risk_reason:
        reasons.append(risk_reason)
    notional = quantity * limit_price
    estimated_fee = max(notional * COMMISSION_RATE, MIN_COMMISSION) + (notional * STAMP_DUTY_RATE if side == "sell" else 0.0)
    if side == "buy" and notional + estimated_fee > account.cash + 1e-9:
        reasons.append("insufficient_cash_including_fees")
    if side == "sell" and quantity > _settled_shares(db, paper_account_id, code):
        reasons.append("insufficient_settled_position")
    control = _control(db)
    if control.kill_switch_active:
        reasons.append("kill_switch_active")
    if not connection.enabled:
        reasons.append("connection_not_enabled")
    if connection.provider.lower() not in _adapter_capabilities:
        reasons.append("broker_adapter_not_configured")
    if not settings.live_trading_enabled:
        reasons.append("live_trading_disabled_by_config")
    if not _LIVE_EXECUTION_IMPLEMENTED:
        reasons.append("live_execution_not_implemented")
    draft = LiveOrderDraft(
        connection_id=connection_id, paper_account_id=paper_account_id,
        idempotency_key=idempotency_key, code=code, side=side,
        quantity=quantity, limit_price=limit_price, price_source=price_source,
        price_as_of=price_as_of, price_freshness=price_freshness,
        notional=notional, estimated_fee=estimated_fee,
        cash_before=account.cash,
        cash_after=account.cash - notional - estimated_fee if side == "buy" else account.cash + notional - estimated_fee,
        risk_status="blocked" if reasons else "approved",
        risk_reason=";".join(dict.fromkeys(reasons)) or None,
        status="blocked" if reasons else "draft",
    )
    db.add(draft)
    db.flush()
    _audit(db, action="live_order_draft.create", resource_type="live_order_draft",
           resource_id=draft.id,
           outcome="blocked" if reasons else "accepted",
           details={"connection_id": connection_id, "paper_account_id": paper_account_id,
                    "code": code, "side": side, "quantity": quantity,
                    "risk_status": draft.risk_status, "risk_reason": draft.risk_reason})
    db.commit()
    db.refresh(draft)
    return _draft_dict(draft)


def list_drafts(db: Session, limit: int = 50) -> list[dict[str, Any]]:
    rows = (db.query(LiveOrderDraft).order_by(LiveOrderDraft.created_at.desc())
            .limit(max(1, min(limit, 200))).all())
    return [_draft_dict(row) for row in rows]


def confirm_draft(db: Session, draft_id: str, *, confirmation_phrase: str,
                  actor: str = "local-user") -> dict[str, Any]:
    row = db.query(LiveOrderDraft).filter(LiveOrderDraft.id == draft_id).first()
    if not row:
        raise KeyError("live order draft not found")
    if confirmation_phrase != "CONFIRM LIVE ORDER":
        raise ValueError("confirmation_phrase_required")
    if row.status != "draft" or row.risk_status != "approved":
        raise ValueError(row.risk_reason or "live_order_draft_not_confirmable")
    # Re-run the quote and account checks inside the confirmation transaction.
    # A draft can remain open in a browser while the quote, cash, position or
    # daily-loss state changes underneath it.
    try:
        _validate_quote(price=row.limit_price, price_source=row.price_source,
                        price_as_of=row.price_as_of, price_freshness=row.price_freshness)
    except ValueError as exc:
        raise ValueError("quote_stale_or_unknown") from exc
    account = db.query(PaperAccount).filter(PaperAccount.id == row.paper_account_id).first()
    if not account:
        raise ValueError("paper_account_not_found")
    positions = db.query(PaperAccountPosition).filter(
        PaperAccountPosition.account_id == row.paper_account_id).all()
    position = next((item for item in positions if item.code == row.code), None)
    equity = account.cash + sum(item.market_value for item in positions)
    latest = (db.query(PaperValuation).filter(PaperValuation.account_id == row.paper_account_id)
              .order_by(PaperValuation.valuation_date.desc()).first())
    daily_return = latest.daily_return if latest else 0.0
    current_position_value = position.shares * row.limit_price if position else 0.0
    risk_reason = RiskEngine(RiskLimits(account.max_order_notional,
                                        account.max_position_weight,
                                        account.max_daily_loss)).check(
        OrderIntent(row.idempotency_key, row.code, row.side, row.quantity, row.limit_price),
        reference_price=row.limit_price, equity=equity,
        current_position_value=current_position_value, daily_return=daily_return)
    refreshed_reasons: list[str] = []
    if risk_reason:
        refreshed_reasons.append(risk_reason)
    notional = row.quantity * row.limit_price
    estimated_fee = max(notional * COMMISSION_RATE, MIN_COMMISSION) + (
        notional * STAMP_DUTY_RATE if row.side == "sell" else 0.0)
    if row.side == "buy" and notional + estimated_fee > account.cash + 1e-9:
        refreshed_reasons.append("insufficient_cash_including_fees")
    if row.side == "sell" and row.quantity > _settled_shares(db, row.paper_account_id, row.code):
        refreshed_reasons.append("insufficient_settled_position")
    if refreshed_reasons:
        refreshed_reason = ";".join(dict.fromkeys(refreshed_reasons))
        row.risk_status = "blocked"
        row.risk_reason = refreshed_reason
        row.status = "blocked"
        row.updated_at = _now()
        _audit(db, action="live_order_draft.confirm", resource_type="live_order_draft",
               resource_id=row.id, outcome="blocked",
               details={"reason": refreshed_reason, "code": row.code})
        db.commit()
        raise ValueError(refreshed_reason)
    # This is intentionally checked again at confirmation time.  A draft may
    # sit in the UI while the global control or connection changes.
    control = _control(db)
    connection = db.query(BrokerConnection).filter(BrokerConnection.id == row.connection_id).first()
    if (control.kill_switch_active or not settings.live_trading_enabled
            or not connection or not connection.enabled or not _LIVE_EXECUTION_IMPLEMENTED):
        raise ValueError("live_submission_disabled")
    if not connection or connection.provider.lower() not in _adapter_capabilities:
        raise ValueError("broker_adapter_not_configured")
    row.confirmed_at = _now()
    row.status = "confirmed"
    row.updated_at = _now()
    _audit(db, action="live_order_draft.confirm", resource_type="live_order_draft",
           resource_id=row.id, outcome="confirmed",
           details={"connection_id": row.connection_id, "code": row.code,
                    "side": row.side, "quantity": row.quantity}, actor=actor)
    db.commit()
    db.refresh(row)
    return _draft_dict(row)


def cancel_draft(db: Session, draft_id: str, *, reason: str = "cancelled_by_user",
                 actor: str = "local-user") -> dict[str, Any]:
    row = db.query(LiveOrderDraft).filter(LiveOrderDraft.id == draft_id).first()
    if not row:
        raise KeyError("live order draft not found")
    if row.status in {"submitted", "filled"}:
        raise ValueError("live_order_draft_already_submitted")
    reason = reason.strip()
    if not reason:
        raise ValueError("cancel_reason_required")
    row.status = "cancelled"
    row.updated_at = _now()
    _audit(db, action="live_order_draft.cancel", resource_type="live_order_draft",
           resource_id=row.id, outcome="cancelled", details={"reason_present": True}, actor=actor)
    db.commit()
    db.refresh(row)
    return _draft_dict(row)


def list_audit_events(db: Session, limit: int = 100) -> list[dict[str, Any]]:
    rows = (db.query(AuditEvent).order_by(AuditEvent.id.desc())
            .limit(max(1, min(limit, 500))).all())
    return [{"id": row.id, "actor": row.actor, "action": row.action,
             "resource_type": row.resource_type, "resource_id": row.resource_id,
             "outcome": row.outcome, "details": json.loads(row.details or "{}"),
             "created_at": row.created_at} for row in rows]
