"""Persist M1 pure plans without changing the manual shadow ledger."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from quant_engine.trading.manual_protocol import ExecutionPlan
from server.models.schema import ManualExecutionItem, ManualExecutionPlan, manual_now_str


class PlanPersistenceError(ValueError):
    pass


def persist_execution_plan(
    db: Session,
    plan: ExecutionPlan,
    *,
    account_id: str,
    authorization_hash: str,
    trading_rule_effective_date: date | None = None,
    idempotency_key: str | None = None,
) -> ManualExecutionPlan:
    plan_id = plan.id or plan.plan_hash
    key = idempotency_key or plan_id
    existing = db.scalars(select(ManualExecutionPlan).where(ManualExecutionPlan.idempotency_key == key)).first()
    if existing:
        if existing.plan_hash != plan.plan_hash:
            raise PlanPersistenceError("plan_idempotency_conflict")
        return existing
    effective_date = trading_rule_effective_date or plan.execution_date
    row = ManualExecutionPlan(
        id=plan_id, idempotency_key=key, decision_id=plan.decision_id,
        authorization_id=plan.authorization_id or "", account_id=account_id,
        execution_date=plan.execution_date.isoformat(), execution_session=plan.execution_session,
        plan_type=plan.plan_type, version=plan.version, input_hash=plan.input_hash,
        plan_hash=plan.plan_hash, account_snapshot_id=plan.account_snapshot_id,
        quote_snapshot_hash=plan.quote_snapshot_hash or "0" * 64,
        authorization_hash=authorization_hash, trading_rule_id=plan.trading_rule_id or "a-share-main-board",
        trading_rule_version=plan.trading_rule_version or "unknown",
        trading_rule_effective_date=effective_date.isoformat(), status=plan.status,
        cash_before=Decimal(str(plan.cash_before)), expected_cash_after=Decimal(str(plan.expected_cash_after)),
        expected_fees=Decimal(str(plan.expected_fees)), blocked_reason=plan.blocked_reason,
        supersedes_plan_id=plan.supersedes_plan_id,
    )
    db.add(row)
    for item in plan.items:
        db.add(ManualExecutionItem(
            id=f"{plan_id}-item-{item.order_sequence}", idempotency_key=f"{key}:item:{item.order_sequence}",
            plan_id=plan_id, cohort_id=item.cohort_id, code=item.code, side=item.side, phase=item.phase,
            pre_quantity=Decimal(str(item.available_quantity if item.side == "sell" else 0)),
            available_quantity=Decimal(str(item.available_quantity)), available_cash_before=Decimal(str(plan.cash_before)),
            target_quantity=Decimal(str(item.planned_quantity)), target_weight=Decimal(str(item.target_weight)),
            planned_quantity=Decimal(str(item.planned_quantity)), cash_required=Decimal(str(item.cash_required)),
            cash_dependency_type=item.cash_dependency_type, depends_on_item_ids="[]",
            reference_price=Decimal(str(item.reference_price)), price_source=item.price_source,
            price_as_of=item.price_as_of, expected_notional=Decimal(str(item.expected_notional)),
            estimated_commission=Decimal(str(item.estimated_commission)), estimated_tax=Decimal(str(item.estimated_tax)),
            estimated_other_fee=Decimal(str(item.estimated_other_fee)), order_sequence=item.order_sequence,
            reason_codes=json.dumps(list(item.reason_codes), ensure_ascii=False), status=item.status,
        ))
    db.commit()
    db.refresh(row)
    return row


def mark_plan_viewed(db: Session, plan_id: str) -> ManualExecutionPlan:
    row = db.get(ManualExecutionPlan, plan_id)
    if row is None:
        raise PlanPersistenceError("manual_plan_not_found")
    if row.status in {"cancelled", "expired", "blocked", "superseded"}:
        raise PlanPersistenceError("manual_plan_not_viewable")
    if row.status in {"draft", "ready"}:
        row.status, row.viewed_at, row.updated_at = "viewed", manual_now_str(), manual_now_str()
        db.commit()
        db.refresh(row)
    return row


__all__ = ["PlanPersistenceError", "persist_execution_plan", "mark_plan_viewed"]
