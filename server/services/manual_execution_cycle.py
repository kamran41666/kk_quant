"""User-confirmed plan execution and first-fill authorization activation."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.models.schema import (
    ManualAccount,
    ManualCohort,
    ManualExecutionAuthorization,
    ManualExecutionConfirmation,
    ManualExecutionEvent,
    ManualExecutionItem,
    ManualExecutionPlan,
    ManualPositionLot,
    manual_now_str,
)
from server.services.manual_authorization import activate_on_first_fill
from server.services.manual_ledger import ManualLedgerError, record_execution_event, record_fill_correction


class ManualExecutionCycleError(ValueError):
    pass


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


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
    if result != result.to_integral_value():
        raise ManualExecutionCycleError("fill_quantity_must_be_integer_shares")
    return result


def _event_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.astimezone(SHANGHAI_TZ).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    try:
        text = str(value)
        if "T" in text:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return moment.astimezone(SHANGHAI_TZ).date() if moment.tzinfo else moment.date()
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ManualExecutionCycleError("fill_time_must_be_iso_timestamp") from exc


def _recompute_item_and_plan(db: Session, item: ManualExecutionItem, plan: ManualExecutionPlan) -> None:
    events = db.scalars(select(ManualExecutionEvent).where(
        ManualExecutionEvent.item_id == item.id,
    )).all()
    confirmed = Decimal("0")
    for event in events:
        quantity = Decimal(str(event.quantity or 0))
        if event.event_type in {"fill", "partial_fill", "fill_correction"}:
            confirmed += quantity
        elif event.event_type == "fill_reversal":
            confirmed -= quantity
    if confirmed < 0:
        raise ManualExecutionCycleError("execution_event_net_quantity_negative")
    planned = Decimal(str(item.planned_quantity))
    if confirmed > planned:
        raise ManualExecutionCycleError("fill_exceeds_planned_quantity")
    item.confirmed_quantity = confirmed
    if confirmed == planned:
        item.status = "filled"
    elif confirmed > 0:
        item.status = "partially_filled"
    elif item.status in {"filled", "partially_filled"}:
        item.status = "planned"
    item.updated_at = manual_now_str()
    siblings = db.scalars(select(ManualExecutionItem).where(ManualExecutionItem.plan_id == plan.id)).all()
    terminal = {"filled", "skipped", "cancelled", "rejected", "unfilled", "expired"}
    plan.status = "completed" if siblings and all(sibling.status in terminal for sibling in siblings) else (
        "partially_filled" if any(Decimal(str(sibling.confirmed_quantity or 0)) > 0 for sibling in siblings) else "viewed"
    )
    plan.updated_at = manual_now_str()


def _update_cohort_state(db: Session, item: ManualExecutionItem, plan: ManualExecutionPlan) -> None:
    if not item.cohort_id:
        return
    cohort = db.get(ManualCohort, item.cohort_id)
    if cohort is None:
        raise ManualExecutionCycleError("manual_execution_cohort_not_found")
    confirmed = Decimal(str(item.confirmed_quantity or 0))
    if item.side == "buy":
        if confirmed > 0:
            cohort.status = "open" if item.status in {"filled", "unfilled"} else "entering"
        elif item.status in {"unfilled", "cancelled", "rejected", "blocked"}:
            cohort.status = "blocked"
        else:
            cohort.status = "entering"
    else:
        remaining = db.scalars(select(ManualPositionLot).where(
            ManualPositionLot.account_id == plan.account_id,
            ManualPositionLot.cohort_id == cohort.id,
            ManualPositionLot.remaining_quantity > 0,
        )).first()
        if remaining is None:
            cohort.status = "closed"
            cohort.closed_at = manual_now_str()
        elif item.status in {"unfilled", "cancelled", "rejected", "blocked"}:
            cohort.status = "blocked"
        else:
            cohort.status = "exiting"


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
    if plan.status not in {"viewed", "partially_filled"}:
        raise ManualExecutionCycleError("manual_plan_not_confirmable")
    if account.status != "active":
        raise ManualExecutionCycleError("manual_account_not_active")
    if item.status not in {"planned", "submitted", "partially_filled"}:
        raise ManualExecutionCycleError("manual_plan_item_not_confirmable")
    if not str(actor).strip():
        raise ManualExecutionCycleError("confirmation_actor_required")
    existing = db.scalars(select(ManualExecutionConfirmation).where(
        ManualExecutionConfirmation.plan_id == plan_id, ManualExecutionConfirmation.item_id == item_id,
    )).first()
    if existing:
        if existing.actor != str(actor).strip():
            raise ManualExecutionCycleError("execution_confirmation_actor_conflict")
        return existing
    confirmed_at = manual_now_str()
    confirmation_hash = _hash({
        "plan_id": plan_id, "plan_hash": plan.plan_hash, "item_id": item_id,
        "code": item.code, "side": item.side, "planned_quantity": str(item.planned_quantity),
        "reference_price": str(item.reference_price), "account_id": account.id,
        "actor": str(actor).strip(), "confirmed_at": confirmed_at,
    })
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
    existing = db.scalars(select(ManualExecutionEvent).where(
        ManualExecutionEvent.client_event_id == client_event_id,
    )).first()
    if existing is not None:
        try:
            return record_execution_event(
                db, account.id, client_event_id=client_event_id, item_id=item_id,
                cohort_id=item.cohort_id, user_trade_ref=user_trade_ref, event_type="fill",
                code=item.code, side=item.side, quantity=quantity, price=price,
                commission=commission, stamp_duty=stamp_duty, other_fee=other_fee,
                total_fee=total_fee, traded_at=traded_at, calendar=calendar,
            )
        except ManualLedgerError as exc:
            raise ManualExecutionCycleError(str(exc)) from exc
    if account.status != "active":
        raise ManualExecutionCycleError("manual_account_not_active")
    if plan.status not in {"viewed", "partially_filled"}:
        raise ManualExecutionCycleError("manual_plan_must_be_viewed_before_fill")
    if item.status not in {"planned", "submitted", "partially_filled"}:
        raise ManualExecutionCycleError("manual_plan_item_not_fillable")
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
    traded_date = _event_date(traded_at)
    if traded_date.isoformat() != plan.execution_date:
        raise ManualExecutionCycleError("fill_date_does_not_match_plan")
    begin = datetime.fromisoformat(authorization.valid_from.replace("Z", "+00:00")).astimezone(timezone.utc)
    finish = datetime.fromisoformat(authorization.valid_until.replace("Z", "+00:00")).astimezone(timezone.utc)
    if isinstance(traded_at, datetime):
        traded_timestamp = traded_at
    elif isinstance(traded_at, str) and "T" in traded_at:
        try:
            traded_timestamp = datetime.fromisoformat(traded_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ManualExecutionCycleError("fill_time_must_be_iso_timestamp") from exc
    else:
        traded_timestamp = None
    if traded_timestamp is not None:
        if traded_timestamp.tzinfo is None:
            raise ManualExecutionCycleError("fill_time_requires_timezone")
        if not begin <= traded_timestamp.astimezone(timezone.utc) < finish:
            raise ManualExecutionCycleError("manual_authorization_outside_validity_window")
    elif not begin.astimezone(SHANGHAI_TZ).date() <= traded_date < finish.astimezone(SHANGHAI_TZ).date():
        raise ManualExecutionCycleError("manual_authorization_outside_validity_window")
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
    fill_price = Decimal(str(price))
    if item.min_price is not None and fill_price < Decimal(str(item.min_price)):
        raise ManualExecutionCycleError("fill_price_below_plan_minimum")
    if item.max_price is not None and fill_price > Decimal(str(item.max_price)):
        raise ManualExecutionCycleError("fill_price_above_plan_maximum")
    try:
        row = record_execution_event(
            db, account.id, client_event_id=client_event_id, item_id=item_id,
            cohort_id=item.cohort_id,
            user_trade_ref=user_trade_ref, event_type="fill", code=item.code, side=item.side,
            quantity=filled, price=price, commission=commission, stamp_duty=stamp_duty,
            other_fee=other_fee, total_fee=total_fee, traded_at=traded_at, calendar=calendar,
            commit=False,
        )
        if authorization.status == "approved":
            activate_on_first_fill(db, authorization.id, event_id=row.id, filled_at=row.traded_at, commit=False)
        _recompute_item_and_plan(db, item, plan)
        _update_cohort_state(db, item, plan)
        db.commit()
        db.refresh(row)
        return row
    except Exception:
        db.rollback()
        raise


def correct_confirmed_fill(
    db: Session,
    *,
    original_event_id: str,
    replacement_client_event_id: str,
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
    original = db.get(ManualExecutionEvent, original_event_id)
    if original is None or not original.item_id:
        raise ManualExecutionCycleError("planned_execution_event_not_found")
    item = db.get(ManualExecutionItem, original.item_id)
    if item is None:
        raise ManualExecutionCycleError("manual_plan_item_not_found")
    plan, item, _account = _owned_item(db, item.plan_id, item.id)
    try:
        replacement = record_fill_correction(
            db, plan.account_id, original_event_id=original_event_id,
            replacement_client_event_id=replacement_client_event_id,
            side=item.side, quantity=_quantity(quantity), code=item.code, price=price,
            commission=commission, stamp_duty=stamp_duty, other_fee=other_fee,
            total_fee=total_fee, traded_at=traded_at, item_id=item.id,
            cohort_id=item.cohort_id, user_trade_ref=user_trade_ref,
            calendar=calendar, commit=False,
        )
        _recompute_item_and_plan(db, item, plan)
        _update_cohort_state(db, item, plan)
        db.commit()
        db.refresh(replacement)
        return replacement
    except Exception:
        db.rollback()
        raise


def mark_item_unfilled(db: Session, *, plan_id: str, item_id: str, reason: str) -> ManualExecutionItem:
    plan, item, account = _owned_item(db, plan_id, item_id)
    if account.status != "active":
        raise ManualExecutionCycleError("manual_account_not_active")
    if not str(reason).strip():
        raise ManualExecutionCycleError("unfilled_reason_required")
    item.status = "unfilled"
    item.reason_codes = json.dumps([str(reason).strip()], ensure_ascii=False)
    item.updated_at = manual_now_str()
    siblings = db.scalars(select(ManualExecutionItem).where(ManualExecutionItem.plan_id == plan.id)).all()
    plan.status = "completed" if siblings and all(sibling.status in {"filled", "skipped", "cancelled", "rejected", "unfilled"} for sibling in siblings) else "partially_filled"
    plan.updated_at = manual_now_str()
    _update_cohort_state(db, item, plan)
    db.commit()
    db.refresh(item)
    return item


__all__ = [
    "ManualExecutionCycleError", "confirm_plan_item", "record_confirmed_fill",
    "correct_confirmed_fill", "mark_item_unfilled",
]
