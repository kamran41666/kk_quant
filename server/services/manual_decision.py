"""Persist one immutable daily decision revision per release/account/date."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Mapping
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from quant_engine.trading.manual_protocol import DecisionAction
from server.models.schema import DailyDecision, ManualAccount, ManualExecutionAuthorization, StrategyRelease, manual_now_str, uuid4_str


class DecisionError(ValueError):
    pass


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _timestamp(value: str | None) -> str:
    if value is None:
        return manual_now_str()
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise DecisionError("data_as_of_requires_timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def create_daily_decision(
    db: Session,
    *,
    release_id: str,
    authorization_id: str,
    signal_date: date,
    target_weights: Mapping[str, Any],
    data_as_of: str,
    input_facts: Mapping[str, Any],
    action: str = DecisionAction.REBALANCE.value,
    risk_state: str = "normal",
    reason_codes: tuple[str, ...] = (),
    blocked_reason: str | None = None,
) -> DailyDecision:
    release = db.get(StrategyRelease, release_id)
    authorization = db.get(ManualExecutionAuthorization, authorization_id)
    if release is None or release.status != "manual_ready":
        raise DecisionError("release_is_not_manual_ready")
    if authorization is None or authorization.release_id != release_id or authorization.status not in {"approved", "active"}:
        raise DecisionError("authorization_is_not_active")
    account = db.get(ManualAccount, authorization.account_id)
    if account is None or account.status in {"draft", "suspended", "closed"}:
        raise DecisionError("manual_account_is_not_active")
    decision_as_of = _timestamp(data_as_of)
    if not authorization.valid_from <= decision_as_of < authorization.valid_until:
        raise DecisionError("authorization_is_outside_validity_window")
    normalized_action = str(action).strip().lower()
    if normalized_action not in {item.value for item in DecisionAction}:
        raise DecisionError("unsupported_decision_action")
    normalized_weights = {str(code).strip().upper(): str(Decimal(str(weight))) for code, weight in sorted(target_weights.items())}
    if any(Decimal(value) < 0 for value in normalized_weights.values()):
        raise DecisionError("target_weight_must_be_nonnegative")
    if sum((Decimal(value) for value in normalized_weights.values()), Decimal("0")) > Decimal("0.45"):
        raise DecisionError("daily_cohort_target_weight_exceeds_45_percent")
    if account.status == "reconcile" and normalized_action != DecisionAction.RECONCILE.value:
        raise DecisionError("manual_account_requires_reconcile_decision")
    facts = {"release_id": release_id, "authorization_id": authorization_id, "signal_date": signal_date.isoformat(), "data_as_of": data_as_of, "target_weights": normalized_weights, "input_facts": dict(input_facts), "action": normalized_action, "risk_state": risk_state, "reason_codes": sorted(set(reason_codes)), "blocked_reason": blocked_reason}
    input_hash = _hash(facts)
    decision_hash = _hash({**facts, "input_hash": input_hash})
    existing = db.scalars(select(DailyDecision).where(DailyDecision.decision_hash == decision_hash)).first()
    if existing:
        return existing
    prior = db.scalars(select(DailyDecision).where(
        DailyDecision.release_id == release_id, DailyDecision.authorization_id == authorization_id,
        DailyDecision.signal_date == signal_date.isoformat(), DailyDecision.status != "superseded",
    ).order_by(DailyDecision.revision.desc())).first()
    revision = (prior.revision + 1) if prior else 1
    if prior:
        prior.status = "superseded"
    resolved_status = "blocked" if blocked_reason or normalized_action in {"blocked", "reconcile"} else "ready"
    row = DailyDecision(
        id=uuid4_str(), release_id=release_id, authorization_id=authorization_id,
        signal_date=signal_date.isoformat(), revision=revision, data_as_of=decision_as_of,
        input_hash=input_hash, decision_hash=decision_hash, action=normalized_action,
        risk_state=risk_state, target_weights=json.dumps(normalized_weights, sort_keys=True),
        reason_codes=json.dumps(sorted(set(reason_codes))), status=resolved_status,
        blocked_reason=blocked_reason,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


__all__ = ["DecisionError", "create_daily_decision"]
