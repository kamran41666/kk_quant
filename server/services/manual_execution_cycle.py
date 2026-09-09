"""User-confirmed plan execution and first-fill authorization activation."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.models.schema import (
    ManualAccount,
    ManualExecutionAuthorization,
    ManualExecutionConfirmation,
    ManualExecutionEvent,
    ManualExecutionItem,
    ManualExecutionPlan,
    manual_now_str,
)
from server.services.manual_authorization import activate_on_first_fill
from server.services.manual_ledger import ManualLedgerError, record_execution_event
from server.services.manual_plan_persistence import mark_plan_viewed


class ManualExecutionCycleError(ValueError):
    pass


class CalendarLike(Protocol):
    def next_trading_day(self, value: date) -> date:
        ...


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _quantity(value: Any) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ManualExecutionCycleError("fill_quantity_must_be_numeric") from exc
    if not result.is_finite() or result <= 0:
        raise ManualExecutionCycleError("fill_quantity_must_be_positive")
    return result


def _owned_item(db: Session, plan_id: str, item_id: str) -> tuple[ManualExecutionPlan, ManualExecutionItem, ManualAccount]:
    plan = db.get(ManualExecutionPlan, plan_id)
    item = db.get(ManualExecutionItem, item_id)
    if plan is None or item is None or item.plan_id != plan_id:
        raise ManualExecutionCycleError("manual_plan_item_not_found")
    account = db.get(ManualAccount, plan.account_id)
    if account is None:
        raise ManualExecutionCycleError("manual_account_not_found")
    return plan, item, account


def confirm_plan_item(db: Session, *, plan_id: str, item_id: str, actor: str) -> ManualExecutionConfirmation:
    plan, item, account = _owned_item(db, plan_id, item_id)
    if plan.status not in {"ready", "viewed", "partially_filled"}:
        raise ManualExecutionCycleError("manual_plan_not_confirmable")
    if account.status != "active":
        raise ManualExecutionCycleError("manual_account_not_active")
    if not str(actor).strip():
        raise ManualExecutionCycleError("confirmation_actor_required")
    existing = db.scalars(select(ManualExecutionConfirmation).where(
        ManualExecutionConfirmation.plan_id == plan_id, ManualExecutionConfirmation.item_id == item_id,
    )).first()
    if existing:
        if existing.actor != str(actor).strip():
            raise ManualExecutionCycleError("execution_confirmation_actor_conflict")
        return existing
    if plan.status == "ready":
        plan = mark_plan_viewed(db, plan_id)
    confirmed_at = manual_now_str()
    confirmation_hash = _hash({"plan_id": plan_id, "item_id": item_id, "account_id": account.id, "actor": str(actor).strip(), "confirmed_at": confirmed_at})
    row = ManualExecutionConfirmation(
        plan_id=plan_id, item_id=item_id, account_id=account.id,
        actor=str(actor).strip(), confirmed_at=confirmed_at, confirmation_hash=confirmation_hash,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def record_confirmed_fill(
    db: Session,
    *,
    plan_id: str,
    item_id: str,
    client_event_id: str,
    quantity: Any,
    price: Any,
    traded_at: date | datetime | str,
    commission: Any = Decimal("0"),
    stamp_duty: Any = Decimal("0"),
    other_fee: Any = Decimal("0"),
    total_fee: Any | None = None,
    user_trade_ref: str | None = None,
    calendar: CalendarLike | None = None,
) -> ManualExecutionEvent:
    plan, item, account = _owned_item(db, plan_id, item_id)
    if account.status != "active":
        raise ManualExecutionCycleError("manual_account_not_active")
    if plan.status not in {"viewed", "partially_filled"}:
        raise ManualExecutionCycleError("manual_plan_must_be_viewed_before_fill")
    confirmation = db.scalars(select(ManualExecutionConfirmation).where(
        ManualExecutionConfirmation.plan_id == plan_id, ManualExecutionConfirmation.item_id == item_id,
    )).first()
    if confirmation is None:
        raise ManualExecutionCycleError("execution_item_confirmation_required")
    authorization = db.get(ManualExecutionAuthorization, plan.authorization_id)
    if authorization is None or authorization.account_id != account.id:
        raise ManualExecutionCycleError("manual_authorization_not_found")
    if authorization.status not in {"approved", "active"}:
        raise ManualExecutionCycleError("manual_authorization_not_approved")
    filled = _quantity(quantity)
    confirmed = Decimal(str(item.confirmed_quantity or 0))
    planned = Decimal(str(item.planned_quantity))
    if confirmed + filled > planned:
        raise ManualExecutionCycleError("fill_exceeds_planned_quantity")
    try:
        notional = filled * Decimal(str(price))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ManualExecutionCycleError("fill_price_must_be_numeric") from exc
    if notional > Decimal(str(authorization.max_order_notional)):
        raise ManualExecutionCycleError("fill_exceeds_authorized_order_notional")
    existing = db.scalars(select(ManualExecutionEvent).where(
        ManualExecutionEvent.client_event_id == client_event_id,
    )).first()
    if existing:
        if existing.account_id != account.id or existing.item_id != item_id:
            raise ManualExecutionCycleError("execution_idempotency_conflict")
        return existing
    try:
        row = record_execution_event(
            db, account.id, client_event_id=client_event_id, item_id=item_id,
            user_trade_ref=user_trade_ref, event_type="fill", code=item.code, side=item.side,
            quantity=filled, price=price, commission=commission, stamp_duty=stamp_duty,
            other_fee=other_fee, total_fee=total_fee, traded_at=traded_at, calendar=calendar,
        )
        if authorization.status == "approved":
            activate_on_first_fill(db, authorization.id, event_id=row.id, filled_at=row.traded_at)
        item.confirmed_quantity = confirmed + filled
        item.status = "filled" if item.confirmed_quantity >= planned else "partially_filled"
        item.updated_at = manual_now_str()
        siblings = db.scalars(select(ManualExecutionItem).where(ManualExecutionItem.plan_id == plan.id)).all()
        plan.status = "completed" if siblings and all(sibling.status in {"filled", "skipped", "cancelled", "rejected", "unfilled"} for sibling in siblings) else "partially_filled"
        plan.updated_at = manual_now_str()
        db.commit()
        db.refresh(row)
        return row
    except ManualLedgerError:
        raise


def mark_item_unfilled(db: Session, *, plan_id: str, item_id: str, reason: str) -> ManualExecutionItem:
    plan, item, account = _owned_item(db, plan_id, item_id)
    if account.status != "active":
        raise ManualExecutionCycleError("manual_account_not_active")
    if not str(reason).strip():
        raise ManualExecutionCycleError("unfilled_reason_required")
    if item.confirmed_quantity and Decimal(str(item.confirmed_quantity)) > 0:
        raise ManualExecutionCycleError("partially_filled_item_cannot_be_marked_unfilled")
    item.status = "unfilled"
    item.reason_codes = json.dumps([str(reason).strip()], ensure_ascii=False)
    item.updated_at = manual_now_str()
    siblings = db.scalars(select(ManualExecutionItem).where(ManualExecutionItem.plan_id == plan.id)).all()
    plan.status = "completed" if siblings and all(sibling.status in {"filled", "skipped", "cancelled", "rejected", "unfilled"} for sibling in siblings) else "partially_filled"
    plan.updated_at = manual_now_str()
    db.commit()
    db.refresh(item)
    return item


__all__ = ["ManualExecutionCycleError", "confirm_plan_item", "record_confirmed_fill", "mark_item_unfilled"]
