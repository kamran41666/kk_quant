"""Separate user/account authorization for an already manual-ready release."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.models.schema import AuditEvent, ManualAccount, ManualExecutionAuthorization, StrategyRelease, manual_now_str, uuid4_str


class AuthorizationError(ValueError):
    pass


def _d(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise AuthorizationError(f"{field}_must_be_numeric") from exc
    if not result.is_finite() or result <= 0:
        raise AuthorizationError(f"{field}_must_be_positive")
    return result


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _timestamp(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthorizationError("authorization_validity_must_be_iso_timestamp") from exc
    if parsed.tzinfo is None:
        raise AuthorizationError("authorization_validity_requires_timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def create_authorization(
    db: Session,
    *,
    release_id: str,
    account_id: str,
    capital_limit: Any,
    max_order_notional: Any,
    max_gross_exposure: Any,
    max_single_weight: Any,
    max_daily_items: int,
    max_daily_loss: Any,
    max_drawdown: Any,
    revocation_policy: Mapping[str, Any],
    valid_from: str,
    valid_until: str,
) -> ManualExecutionAuthorization:
    release = db.get(StrategyRelease, release_id)
    account = db.get(ManualAccount, account_id)
    if release is None or release.status != "manual_ready":
        raise AuthorizationError("release_is_not_manual_ready")
    if account is None:
        raise AuthorizationError("manual_account_not_found")
    if int(max_daily_items) <= 0:
        raise AuthorizationError("max_daily_items_must_be_positive")
    exposure, weight, drawdown = _d(max_gross_exposure, "max_gross_exposure"), _d(max_single_weight, "max_single_weight"), _d(max_drawdown, "max_drawdown")
    if exposure > 1 or weight > 1 or drawdown > 1:
        raise AuthorizationError("ratio_limit_must_be_at_most_one")
    begin, finish = _timestamp(valid_from), _timestamp(valid_until)
    if begin >= finish:
        raise AuthorizationError("authorization_valid_from_must_precede_until")
    active = db.scalars(select(ManualExecutionAuthorization).where(
        ManualExecutionAuthorization.account_id == account_id,
        ManualExecutionAuthorization.status.in_({"approved", "active", "reconcile"}),
    )).first()
    if active:
        raise AuthorizationError("manual_account_already_has_active_authorization")
    policy_json = json.dumps(dict(revocation_policy), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    row = ManualExecutionAuthorization(
        id=uuid4_str(), release_id=release_id, account_id=account_id, status="pending",
        capital_limit=_d(capital_limit, "capital_limit"), max_order_notional=_d(max_order_notional, "max_order_notional"),
        max_gross_exposure=exposure, max_single_weight=weight, max_daily_items=int(max_daily_items),
        max_daily_loss=_d(max_daily_loss, "max_daily_loss"), max_drawdown=drawdown,
        revocation_policy=policy_json, revocation_policy_hash=_hash(json.loads(policy_json)),
        valid_from=begin, valid_until=finish,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def approve_authorization(
    db: Session,
    authorization_id: str,
    *,
    approved_by: str,
    now: str | None = None,
) -> ManualExecutionAuthorization:
    row = db.get(ManualExecutionAuthorization, authorization_id)
    if row is None:
        raise AuthorizationError("authorization_not_found")
    release = db.get(StrategyRelease, row.release_id)
    if release is None or release.status != "manual_ready":
        raise AuthorizationError("release_is_not_manual_ready")
    if row.status != "pending":
        raise AuthorizationError("only_pending_authorization_can_be_approved")
    approval_time = _timestamp(now or manual_now_str())
    if not row.valid_from <= approval_time < row.valid_until:
        raise AuthorizationError("authorization_not_currently_valid")
    conflicting = db.scalars(select(ManualExecutionAuthorization).where(
        ManualExecutionAuthorization.account_id == row.account_id,
        ManualExecutionAuthorization.id != row.id,
        ManualExecutionAuthorization.status.in_({"approved", "active", "reconcile"}),
    )).first()
    if conflicting is not None:
        raise AuthorizationError("manual_account_already_has_active_authorization")
    row.status = "approved"
    row.approved_by, row.approved_at, row.updated_at = str(approved_by), approval_time, approval_time
    db.add(AuditEvent(actor=str(approved_by), action="approve_manual_authorization", resource_type="manual_execution_authorization", resource_id=row.id, outcome="approved", details=json.dumps({"release_id": row.release_id, "account_id": row.account_id}, sort_keys=True)))
    db.commit()
    db.refresh(row)
    return row


def activate_on_first_fill(
    db: Session,
    authorization_id: str,
    *,
    event_id: str,
    filled_at: str | None = None,
    commit: bool = True,
) -> ManualExecutionAuthorization:
    row = db.get(ManualExecutionAuthorization, authorization_id)
    if row is None:
        raise AuthorizationError("authorization_not_found")
    if row.status not in {"approved", "active"}:
        raise AuthorizationError("authorization_must_be_approved_before_first_fill")
    row.status = "active"
    row.first_fill_event_id = event_id
    row.first_fill_at = filled_at or manual_now_str()
    row.updated_at = manual_now_str()
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row


def revoke_authorization(db: Session, authorization_id: str, *, actor: str, reason: str) -> ManualExecutionAuthorization:
    row = db.get(ManualExecutionAuthorization, authorization_id)
    if row is None:
        raise AuthorizationError("authorization_not_found")
    if row.status in {"revoked", "expired"}:
        return row
    row.status, row.revoked_at, row.reason, row.updated_at = "revoked", manual_now_str(), str(reason), manual_now_str()
    db.add(AuditEvent(actor=str(actor), action="revoke_manual_authorization", resource_type="manual_execution_authorization", resource_id=row.id, outcome="revoked", details=json.dumps({"reason": str(reason)}, ensure_ascii=False, sort_keys=True)))
    db.commit()
    db.refresh(row)
    return row


__all__ = ["AuthorizationError", "create_authorization", "approve_authorization", "activate_on_first_fill", "revoke_authorization"]
