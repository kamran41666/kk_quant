"""Append-only ledger for user-reported A-share executions.

This module is deliberately separate from ``paper_trading``.  It records what
the user says happened at a broker; it never submits an order, obtains broker
credentials, or creates a ``Paper*`` row.  The materialized lots and account
cash are rebuilt from the hash-chained ledger after every economic event.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import threading
import uuid
from typing import Any, Mapping, Protocol, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.models.schema import (
    ManualAccount,
    ManualAccountSnapshot,
    ManualCashEvent,
    ManualExecutionEvent,
    ManualLedgerEvent,
    ManualPositionLot,
    ManualPositionSnapshot,
    ManualReconciliation,
    manual_now_str,
)


ZERO = Decimal("0")
MONEY_QUANT = Decimal("0.00000001")
QUANTITY_QUANT = Decimal("0.00000001")
_ACCOUNT_LOCKS: dict[str, threading.RLock] = {}
_ACCOUNT_LOCKS_GUARD = threading.Lock()


class ManualLedgerError(ValueError):
    """A rejected or inconsistent manual-ledger operation."""


class TradingCalendarLike(Protocol):
    def next_trading_day(self, value: date) -> date:
        ...


def _lock_for(account_id: str) -> threading.RLock:
    with _ACCOUNT_LOCKS_GUARD:
        return _ACCOUNT_LOCKS.setdefault(account_id, threading.RLock())


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _decimal(value: Any, field: str, *, default: Decimal | None = None) -> Decimal:
    if value is None and default is not None:
        return default
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ManualLedgerError(f"{field}_must_be_numeric") from exc
    if not result.is_finite():
        raise ManualLedgerError(f"{field}_must_be_finite")
    return result


def _money(value: Any, field: str, *, default: Decimal | None = None) -> Decimal:
    result = _decimal(value, field, default=default).quantize(MONEY_QUANT)
    return result


def _quantity(value: Any, field: str) -> Decimal:
    return _decimal(value, field).quantize(QUANTITY_QUANT)


def _iso_date(value: date | str, field: str) -> date:
    if isinstance(value, datetime):
        result = value.date()
    elif isinstance(value, date):
        result = value
    else:
        try:
            result = date.fromisoformat(str(value)[:10])
        except ValueError as exc:
            raise ManualLedgerError(f"{field}_must_be_iso_date") from exc
    return result


def _timestamp(value: date | datetime | str | None) -> str:
    if value is None:
        return manual_now_str()
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    else:
        text = str(value).strip()
        if not text:
            return manual_now_str()
        # Persist an ISO value exactly as supplied after requiring a date
        # prefix.  A timezone is required for timestamps with a time component.
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ManualLedgerError("traded_at_must_be_iso_timestamp") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _fees(commission: Any, stamp_duty: Any, other_fee: Any, total_fee: Any | None) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    values = (
        _money(commission, "commission", default=ZERO),
        _money(stamp_duty, "stamp_duty", default=ZERO),
        _money(other_fee, "other_fee", default=ZERO),
    )
    if any(value < ZERO for value in values):
        raise ManualLedgerError("fees_must_be_nonnegative")
    calculated = sum(values, ZERO)
    total = calculated if total_fee is None else _money(total_fee, "total_fee")
    if total < ZERO or total != calculated:
        raise ManualLedgerError("total_fee_must_equal_fee_components")
    return (*values, total)


def _get_account(db: Session, account_id: str) -> ManualAccount:
    account = db.get(ManualAccount, account_id)
    if account is None:
        raise ManualLedgerError("manual_account_not_found")
    if account.status in {"closed", "suspended"}:
        raise ManualLedgerError(f"manual_account_{account.status}")
    return account


def _latest_ledger(db: Session, account_id: str) -> ManualLedgerEvent | None:
    return db.scalars(
        select(ManualLedgerEvent)
        .where(ManualLedgerEvent.account_id == account_id)
        .order_by(ManualLedgerEvent.account_sequence.desc())
        .limit(1)
    ).first()


def _append_ledger(
    db: Session,
    account_id: str,
    *,
    event_type: str,
    reference_id: str,
    trade_date: date,
    cash_delta: Decimal = ZERO,
    quantity_delta: Decimal = ZERO,
    code: str | None = None,
    cohort_id: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> ManualLedgerEvent:
    cash_delta = _money(cash_delta, "cash_delta")
    quantity_delta = _quantity(quantity_delta, "quantity_delta")
    if cash_delta == ZERO and quantity_delta == ZERO:
        raise ManualLedgerError("ledger_event_must_change_cash_or_quantity")
    previous = _latest_ledger(db, account_id)
    sequence = (previous.account_sequence + 1) if previous else 1
    previous_hash = previous.event_hash if previous else ""
    normalized_payload = dict(payload or {})
    hash_input = {
        "account_id": account_id,
        "account_sequence": sequence,
        "event_type": event_type,
        "reference_id": reference_id,
        "trade_date": trade_date.isoformat(),
        "cash_delta": cash_delta,
        "quantity_delta": quantity_delta,
        "code": code,
        "cohort_id": cohort_id,
        "payload": normalized_payload,
        "previous_hash": previous_hash,
    }
    row = ManualLedgerEvent(
        id=_new_id(),
        account_id=account_id,
        account_sequence=sequence,
        event_type=event_type,
        reference_id=reference_id,
        trade_date=trade_date.isoformat(),
        cash_delta=cash_delta,
        quantity_delta=quantity_delta,
        code=code,
        cohort_id=cohort_id,
        payload=_canonical_json(normalized_payload),
        previous_hash=previous_hash,
        event_hash=_sha256(hash_input),
    )
    db.add(row)
    db.flush()
    return row


def _available_lots(db: Session, account_id: str, code: str, trade_date: date) -> list[ManualPositionLot]:
    return list(db.scalars(
        select(ManualPositionLot)
        .where(
            ManualPositionLot.account_id == account_id,
            ManualPositionLot.code == code,
            ManualPositionLot.remaining_quantity > ZERO,
            ManualPositionLot.unlock_date <= trade_date.isoformat(),
        )
        .order_by(ManualPositionLot.buy_at.asc(), ManualPositionLot.id.asc())
    ).all())


def _sell_allocations(db: Session, account_id: str, code: str, quantity: Decimal, trade_date: date) -> list[dict[str, Any]]:
    remaining = quantity
    allocations: list[dict[str, Any]] = []
    for lot in _available_lots(db, account_id, code, trade_date):
        if remaining <= ZERO:
            break
        consumed = min(remaining, _quantity(lot.remaining_quantity, "remaining_quantity"))
        allocations.append({
            "buy_at": lot.buy_at,
            "unlock_date": lot.unlock_date,
            "avg_cost": _quantity(lot.avg_cost, "avg_cost"),
            "quantity": consumed,
        })
        remaining -= consumed
    if remaining > ZERO:
        raise ManualLedgerError("t_plus_one_or_insufficient_position")
    return allocations


def _rebuild_lots(db: Session, account_id: str) -> None:
    """Replay quantity events into materialized lots without editing facts."""
    lots = list(db.scalars(select(ManualPositionLot).where(ManualPositionLot.account_id == account_id)).all())
    for lot in lots:
        db.delete(lot)
    db.flush()
    events = db.scalars(
        select(ManualLedgerEvent)
        .where(ManualLedgerEvent.account_id == account_id)
        .order_by(ManualLedgerEvent.account_sequence.asc())
    ).all()
    for event in events:
        quantity = _quantity(event.quantity_delta, "quantity_delta")
        if quantity == ZERO or not event.code:
            continue
        payload = json.loads(event.payload or "{}")
        effect = str(payload.get("effect", "")).strip().lower()
        if quantity > ZERO and effect == "restore_sell":
            for allocation in payload.get("restore_allocations", []):
                restored = _quantity(allocation["quantity"], "restore_quantity")
                db.add(ManualPositionLot(
                    id=_new_id(), account_id=account_id, code=event.code,
                    cohort_id=event.cohort_id, quantity=restored,
                    remaining_quantity=restored,
                    avg_cost=_quantity(allocation["avg_cost"], "avg_cost"),
                    buy_at=str(allocation["buy_at"]),
                    unlock_date=str(allocation["unlock_date"]),
                    status="open",
                ))
            db.flush()
            continue
        if quantity > ZERO:
            unlock_date = str(payload.get("unlock_date", event.trade_date))
            db.add(ManualPositionLot(
                id=_new_id(), account_id=account_id, code=event.code,
                cohort_id=event.cohort_id, quantity=quantity,
                remaining_quantity=quantity,
                avg_cost=_quantity(payload.get("price", ZERO), "price"),
                buy_at=str(payload.get("buy_at", event.trade_date)),
                unlock_date=unlock_date,
                planned_exit_date=payload.get("planned_exit_date"),
                status="open",
            ))
            db.flush()
            continue
        sell_quantity = -quantity
        allow_locked = effect == "reverse_buy"
        candidates = list(db.scalars(
            select(ManualPositionLot)
            .where(
                ManualPositionLot.account_id == account_id,
                ManualPositionLot.code == event.code,
                ManualPositionLot.remaining_quantity > ZERO,
                (ManualPositionLot.unlock_date <= event.trade_date if not allow_locked else True),
            )
            .order_by(ManualPositionLot.buy_at.asc(), ManualPositionLot.id.asc())
        ).all())
        for lot in candidates:
            if sell_quantity <= ZERO:
                break
            consumed = min(sell_quantity, _quantity(lot.remaining_quantity, "remaining_quantity"))
            lot.remaining_quantity = _quantity(lot.remaining_quantity, "remaining_quantity") - consumed
            if lot.remaining_quantity == ZERO:
                lot.status = "closed"
            sell_quantity -= consumed
        if sell_quantity > ZERO:
            raise ManualLedgerError("ledger_replay_insufficient_position")
    db.flush()


def _rebuild_account(db: Session, account: ManualAccount) -> None:
    events = db.scalars(
        select(ManualLedgerEvent)
        .where(ManualLedgerEvent.account_id == account.id)
        .order_by(ManualLedgerEvent.account_sequence.asc())
    ).all()
    cash = sum((_money(event.cash_delta, "cash_delta") for event in events), ZERO)
    if cash < ZERO:
        raise ManualLedgerError("manual_cash_would_be_negative")
    _rebuild_lots(db, account.id)
    account.confirmed_cash = cash
    account.ledger_checkpoint_hash = events[-1].event_hash if events else ""
    account.updated_at = manual_now_str()
    if account.status == "draft" and events:
        account.status = "active"
    db.flush()


def create_manual_account(
    db: Session,
    name: str,
    *,
    currency: str = "CNY",
    broker_label: str = "",
    risk_policy: Mapping[str, Any] | None = None,
) -> ManualAccount:
    if str(currency).upper() != "CNY":
        raise ManualLedgerError("manual_a_share_account_currency_must_be_cny")
    if not str(name).strip():
        raise ManualLedgerError("manual_account_name_required")
    account = ManualAccount(
        id=_new_id(), name=str(name).strip(), currency="CNY",
        broker_label=str(broker_label or ""), risk_policy=_canonical_json(risk_policy or {}),
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def record_cash_event(
    db: Session,
    account_id: str,
    *,
    idempotency_key: str,
    event_type: str,
    amount: Any,
    occurred_at: date | datetime | str | None = None,
    source: str = "user_reported",
    correction_of: str | None = None,
    note: str | None = None,
) -> ManualCashEvent:
    if source != "user_reported":
        raise ManualLedgerError("manual_cash_source_must_be_user_reported")
    normalized_type = str(event_type).strip().lower()
    if normalized_type not in {"opening_balance", "deposit", "withdrawal", "interest", "fee_adjustment"}:
        raise ManualLedgerError("unsupported_manual_cash_event_type")
    value = _money(amount, "amount")
    if value == ZERO:
        raise ManualLedgerError("cash_amount_must_not_be_zero")
    if normalized_type in {"opening_balance", "deposit", "interest"} and value < ZERO:
        raise ManualLedgerError("cash_inflow_must_be_positive")
    if normalized_type == "withdrawal" and value > ZERO:
        raise ManualLedgerError("withdrawal_must_be_negative")
    if not str(idempotency_key).strip():
        raise ManualLedgerError("cash_idempotency_key_required")
    account = _get_account(db, account_id)
    with _lock_for(account_id):
        existing = db.scalars(select(ManualCashEvent).where(
            ManualCashEvent.account_id == account_id,
            ManualCashEvent.idempotency_key == idempotency_key,
        )).first()
        occurred = _timestamp(occurred_at)
        if existing:
            same = (
                existing.event_type == normalized_type
                and _money(existing.amount, "amount") == value
                and existing.occurred_at == occurred
                and existing.correction_of == correction_of
            )
            if not same:
                raise ManualLedgerError("cash_idempotency_conflict")
            return existing
        if normalized_type == "opening_balance" and db.scalars(select(ManualCashEvent).where(
            ManualCashEvent.account_id == account_id,
            ManualCashEvent.event_type == "opening_balance",
        )).first():
            raise ManualLedgerError("opening_balance_already_recorded")
        try:
            row = ManualCashEvent(
                id=_new_id(), account_id=account_id, idempotency_key=idempotency_key,
                event_type=normalized_type, amount=value, occurred_at=occurred,
                source="user_reported", correction_of=correction_of, note=note,
            )
            db.add(row)
            db.flush()
            trade_date = _iso_date(occurred, "occurred_at")
            _append_ledger(
                db, account_id, event_type=f"cash_{normalized_type}", reference_id=row.id,
                trade_date=trade_date, cash_delta=value,
                payload={"amount": value, "cash_event_type": normalized_type, "occurred_at": occurred},
            )
            _rebuild_account(db, account)
            db.commit()
            db.refresh(row)
            return row
        except Exception:
            db.rollback()
            raise


def _event_payload(
    *,
    client_event_id: str,
    account_id: str,
    item_id: str | None,
    code: str | None,
    cohort_id: str | None,
    user_trade_ref: str | None,
    event_type: str,
    side: str | None,
    quantity: Decimal | None,
    price: Decimal | None,
    commission: Decimal,
    stamp_duty: Decimal,
    other_fee: Decimal,
    total_fee: Decimal,
    traded_at: str | None,
    source: str,
    supersedes_event_id: str | None,
) -> dict[str, Any]:
    return {
        "client_event_id": client_event_id, "account_id": account_id, "item_id": item_id, "code": code,
        "cohort_id": cohort_id, "user_trade_ref": user_trade_ref, "event_type": event_type,
        "side": side, "quantity": quantity, "price": price, "commission": commission,
        "stamp_duty": stamp_duty, "other_fee": other_fee, "total_fee": total_fee,
        "traded_at": traded_at, "source": source, "supersedes_event_id": supersedes_event_id,
    }


def _insert_execution_row(
    db: Session,
    *,
    client_event_id: str,
    account_id: str,
    item_id: str | None,
    code: str | None,
    cohort_id: str | None,
    user_trade_ref: str | None,
    event_type: str,
    side: str | None,
    quantity: Decimal | None,
    price: Decimal | None,
    commission: Decimal,
    stamp_duty: Decimal,
    other_fee: Decimal,
    total_fee: Decimal,
    traded_at: str | None,
    supersedes_event_id: str | None,
) -> ManualExecutionEvent:
    payload = _event_payload(
        client_event_id=client_event_id, account_id=account_id, item_id=item_id, code=code,
        cohort_id=cohort_id, user_trade_ref=user_trade_ref, event_type=event_type,
        side=side, quantity=quantity, price=price, commission=commission,
        stamp_duty=stamp_duty, other_fee=other_fee, total_fee=total_fee,
        traded_at=traded_at, source="user_reported", supersedes_event_id=supersedes_event_id,
    )
    row = ManualExecutionEvent(
        id=_new_id(), client_event_id=client_event_id, item_id=item_id,
        account_id=account_id, code=code, cohort_id=cohort_id, user_trade_ref=user_trade_ref,
        event_type=event_type, side=side, quantity=quantity, price=price,
        commission=commission, stamp_duty=stamp_duty, other_fee=other_fee,
        total_fee=total_fee, traded_at=traded_at, source="user_reported",
        supersedes_event_id=supersedes_event_id, payload_hash=_sha256(payload),
    )
    db.add(row)
    db.flush()
    return row


def _post_fill_ledger(
    db: Session,
    account: ManualAccount,
    row: ManualExecutionEvent,
    *,
    calendar: TradingCalendarLike | None,
    effect: str = "normal",
    restore_allocations: Sequence[Mapping[str, Any]] | None = None,
) -> ManualLedgerEvent:
    if row.side not in {"buy", "sell"} or row.quantity is None or row.price is None or not row.traded_at:
        raise ManualLedgerError("economic_execution_event_requires_side_quantity_price_time")
    code = _normalize_a_share_code(row.code or "")
    trade_date = _iso_date(row.traded_at, "traded_at")
    quantity = _quantity(row.quantity, "quantity")
    price = _money(row.price, "price")
    gross = _money(quantity * price, "gross_amount")
    fees = _money(row.total_fee, "total_fee")
    unlock = trade_date
    allocations: list[dict[str, Any]] = []
    if row.side == "buy":
        if calendar is None and effect == "normal":
            raise ManualLedgerError("trading_calendar_required_for_a_share_buy")
        if calendar is not None and effect == "normal":
            try:
                unlock = calendar.next_trading_day(trade_date)
            except Exception as exc:
                raise ManualLedgerError("trading_calendar_unavailable_for_t_plus_one") from exc
        cash_delta = -(gross + fees)
        quantity_delta = quantity
    else:
        if effect == "normal":
            allocations = _sell_allocations(db, account.id, code, quantity, trade_date)
        cash_delta = gross - fees
        quantity_delta = -quantity
    if effect == "reverse_buy":
        cash_delta = gross + fees
        quantity_delta = -quantity
    elif effect == "restore_sell":
        cash_delta = -(gross - fees)
        quantity_delta = quantity
    payload: dict[str, Any] = {
        "side": row.side, "quantity": quantity, "price": price,
        "fees": {"commission": row.commission, "stamp_duty": row.stamp_duty, "other_fee": row.other_fee, "total": row.total_fee},
        "effect": effect, "buy_at": trade_date, "unlock_date": unlock,
    }
    if allocations:
        payload["sell_allocations"] = allocations
    if restore_allocations:
        payload["restore_allocations"] = list(restore_allocations)
    return _append_ledger(
        db, account.id, event_type=f"execution_{row.event_type}", reference_id=row.id,
        trade_date=trade_date, cash_delta=cash_delta, quantity_delta=quantity_delta,
        code=code, cohort_id=row.cohort_id, payload=payload,
    )


def _normalize_a_share_code(value: str) -> str:
    code = str(value).strip().upper()
    if len(code) != 9 or code[:6].isdigit() is False or code[6:] not in {".SH", ".SZ", ".BJ"}:
        raise ManualLedgerError("manual_a_share_code_must_use_six_digits_and_exchange")
    return code


def record_execution_event(
    db: Session,
    account_id: str,
    *,
    client_event_id: str,
    event_type: str,
    side: str | None = None,
    code: str | None = None,
    quantity: Any | None = None,
    price: Any | None = None,
    commission: Any = ZERO,
    stamp_duty: Any = ZERO,
    other_fee: Any = ZERO,
    total_fee: Any | None = None,
    traded_at: date | datetime | str | None = None,
    item_id: str | None = None,
    cohort_id: str | None = None,
    user_trade_ref: str | None = None,
    source: str = "user_reported",
    calendar: TradingCalendarLike | None = None,
) -> ManualExecutionEvent:
    if source != "user_reported":
        raise ManualLedgerError("manual_execution_source_must_be_user_reported")
    if not str(client_event_id).strip():
        raise ManualLedgerError("execution_client_event_id_required")
    normalized_type = str(event_type).strip().lower()
    supported = {"submitted", "partial_fill", "fill", "cancelled", "rejected", "skipped", "fee_adjustment"}
    if normalized_type not in supported:
        raise ManualLedgerError("use_record_fill_correction_for_reversal_or_correction")
    normalized_side = None if side is None else str(side).strip().lower()
    if normalized_side is not None and normalized_side not in {"buy", "sell"}:
        raise ManualLedgerError("side_must_be_buy_or_sell")
    normalized_quantity = None if quantity is None else _quantity(quantity, "quantity")
    if normalized_quantity is not None and normalized_quantity <= ZERO:
        raise ManualLedgerError("quantity_must_be_positive")
    normalized_price = None if price is None else _money(price, "price")
    if normalized_price is not None and normalized_price <= ZERO:
        raise ManualLedgerError("price_must_be_positive")
    fee_values = _fees(commission, stamp_duty, other_fee, total_fee)
    traded = None if traded_at is None else _timestamp(traded_at)
    account = _get_account(db, account_id)
    payload = _event_payload(
        client_event_id=client_event_id, account_id=account_id, item_id=item_id, code=code,
        cohort_id=cohort_id, user_trade_ref=user_trade_ref, event_type=normalized_type,
        side=normalized_side, quantity=normalized_quantity, price=normalized_price,
        commission=fee_values[0], stamp_duty=fee_values[1], other_fee=fee_values[2],
        total_fee=fee_values[3], traded_at=traded, source="user_reported", supersedes_event_id=None,
    )
    payload_hash = _sha256(payload)
    with _lock_for(account_id):
        existing = db.scalars(select(ManualExecutionEvent).where(
            ManualExecutionEvent.client_event_id == client_event_id,
        )).first()
        if existing:
            if existing.payload_hash != payload_hash:
                raise ManualLedgerError("execution_idempotency_conflict")
            return existing
        try:
            row = _insert_execution_row(
                db, client_event_id=client_event_id, account_id=account_id, item_id=item_id,
                code=code,
                cohort_id=cohort_id, user_trade_ref=user_trade_ref, event_type=normalized_type,
                side=normalized_side, quantity=normalized_quantity, price=normalized_price,
                commission=fee_values[0], stamp_duty=fee_values[1], other_fee=fee_values[2],
                total_fee=fee_values[3], traded_at=traded, supersedes_event_id=None,
            )
            if normalized_type in {"fill", "partial_fill"}:
                _post_fill_ledger(db, account, row, calendar=calendar)
            elif normalized_type == "fee_adjustment":
                if fee_values[3] <= ZERO or not traded:
                    raise ManualLedgerError("fee_adjustment_requires_positive_fee_and_time")
                _append_ledger(
                    db, account_id, event_type="execution_fee_adjustment", reference_id=row.id,
                    trade_date=_iso_date(traded, "traded_at"), cash_delta=-fee_values[3],
                    payload={"total_fee": fee_values[3], "execution_event_id": row.id},
                )
            _rebuild_account(db, account)
            db.commit()
            db.refresh(row)
            return row
        except IntegrityError as exc:
            db.rollback()
            raise ManualLedgerError("execution_idempotency_conflict") from exc
        except Exception:
            db.rollback()
            raise


def record_fill_correction(
    db: Session,
    account_id: str,
    *,
    original_event_id: str,
    replacement_client_event_id: str,
    side: str,
    quantity: Any,
    code: str | None = None,
    price: Any,
    commission: Any = ZERO,
    stamp_duty: Any = ZERO,
    other_fee: Any = ZERO,
    total_fee: Any | None = None,
    traded_at: date | datetime | str,
    item_id: str | None = None,
    cohort_id: str | None = None,
    user_trade_ref: str | None = None,
    calendar: TradingCalendarLike | None = None,
) -> ManualExecutionEvent:
    """Correct a fill by appending reversal and replacement facts."""
    account = _get_account(db, account_id)
    with _lock_for(account_id):
        existing = db.scalars(select(ManualExecutionEvent).where(
            ManualExecutionEvent.client_event_id == replacement_client_event_id,
        )).first()
        if existing:
            return existing
        original = db.get(ManualExecutionEvent, original_event_id)
        if original is None or original.account_id != account_id:
            raise ManualLedgerError("original_execution_event_not_found")
        if original.event_type not in {"fill", "partial_fill"}:
            raise ManualLedgerError("only_fill_events_can_be_corrected")
        if original.quantity is None or original.price is None or original.side is None:
            raise ManualLedgerError("original_fill_is_not_economic")
        original_allocations: list[dict[str, Any]] = []
        original_ledger = db.scalars(select(ManualLedgerEvent).where(
            ManualLedgerEvent.reference_id == original.id,
        )).first()
        if original_ledger:
            original_payload = json.loads(original_ledger.payload or "{}")
            original_allocations = list(original_payload.get("sell_allocations", []))
        try:
            reversal_type = "fill_reversal"
            reversal_id = f"{replacement_client_event_id}:reversal"
            reverse_side = original.side
            reverse_effect = "reverse_buy" if original.side == "buy" else "restore_sell"
            reverse_fees = (
                _money(original.commission, "commission"),
                _money(original.stamp_duty, "stamp_duty"),
                _money(original.other_fee, "other_fee"),
                _money(original.total_fee, "total_fee"),
            )
            reversal = _insert_execution_row(
                db, client_event_id=reversal_id, account_id=account_id, item_id=original.item_id,
                code=original.code,
                cohort_id=original.cohort_id, user_trade_ref=None, event_type=reversal_type,
                side=reverse_side, quantity=_quantity(original.quantity, "quantity"),
                price=_money(original.price, "price"), commission=reverse_fees[0],
                stamp_duty=reverse_fees[1], other_fee=reverse_fees[2], total_fee=reverse_fees[3],
                traded_at=original.traded_at, supersedes_event_id=original.id,
            )
            _post_fill_ledger(
                db, account, reversal, calendar=calendar, effect=reverse_effect,
                restore_allocations=original_allocations,
            )
            replacement = _insert_execution_row(
                db, client_event_id=replacement_client_event_id, account_id=account_id,
                item_id=item_id or original.item_id, code=code or original.code,
                cohort_id=cohort_id or original.cohort_id,
                user_trade_ref=user_trade_ref, event_type="fill_correction",
                side=str(side).strip().lower(), quantity=_quantity(quantity, "quantity"),
                price=_money(price, "price"),
                commission=_fees(commission, stamp_duty, other_fee, total_fee)[0],
                stamp_duty=_fees(commission, stamp_duty, other_fee, total_fee)[1],
                other_fee=_fees(commission, stamp_duty, other_fee, total_fee)[2],
                total_fee=_fees(commission, stamp_duty, other_fee, total_fee)[3],
                traded_at=_timestamp(traded_at), supersedes_event_id=original.id,
            )
            # A correction is a new user-reported fill after the reversal.
            _post_fill_ledger(db, account, replacement, calendar=calendar)
            _rebuild_account(db, account)
            db.commit()
            db.refresh(replacement)
            return replacement
        except Exception:
            db.rollback()
            raise


def verify_ledger_hash_chain(db: Session, account_id: str) -> bool:
    previous_hash = ""
    expected_sequence = 1
    events = db.scalars(select(ManualLedgerEvent).where(
        ManualLedgerEvent.account_id == account_id,
    ).order_by(ManualLedgerEvent.account_sequence.asc())).all()
    for event in events:
        if event.account_sequence != expected_sequence or event.previous_hash != previous_hash:
            return False
        payload = json.loads(event.payload or "{}")
        expected = _sha256({
            "account_id": event.account_id, "account_sequence": event.account_sequence,
            "event_type": event.event_type, "reference_id": event.reference_id,
            "trade_date": event.trade_date, "cash_delta": _money(event.cash_delta, "cash_delta"),
            "quantity_delta": _quantity(event.quantity_delta, "quantity_delta"),
            "code": event.code, "cohort_id": event.cohort_id, "payload": payload,
            "previous_hash": event.previous_hash,
        })
        if expected != event.event_hash:
            return False
        previous_hash = event.event_hash
        expected_sequence += 1
    return True


def get_manual_state(db: Session, account_id: str, *, as_of: date | str | None = None) -> dict[str, Any]:
    account = _get_account(db, account_id)
    cutoff = _iso_date(as_of, "as_of") if as_of is not None else None
    lots = db.scalars(select(ManualPositionLot).where(
        ManualPositionLot.account_id == account_id,
        ManualPositionLot.remaining_quantity > ZERO,
    ).order_by(ManualPositionLot.code.asc(), ManualPositionLot.buy_at.asc(), ManualPositionLot.id.asc())).all()
    positions: dict[str, dict[str, Decimal]] = {}
    for lot in lots:
        total = _quantity(lot.remaining_quantity, "remaining_quantity")
        available = total if cutoff is None or lot.unlock_date <= cutoff.isoformat() else ZERO
        item = positions.setdefault(lot.code, {"quantity": ZERO, "available_quantity": ZERO})
        item["quantity"] += total
        item["available_quantity"] += available
    return {
        "account_id": account.id,
        "cash": _money(account.confirmed_cash, "confirmed_cash"),
        "ledger_checkpoint_hash": account.ledger_checkpoint_hash,
        "positions": positions,
    }


def reconcile_account(
    db: Session,
    account_id: str,
    *,
    idempotency_key: str,
    as_of: date | datetime | str,
    cash: Any,
    total_asset: Any,
    positions: Sequence[Mapping[str, Any]] = (),
    note: str | None = None,
) -> ManualReconciliation:
    account = _get_account(db, account_id)
    if not str(idempotency_key).strip():
        raise ManualLedgerError("snapshot_idempotency_key_required")
    snapshot_time = _timestamp(as_of)
    reported_cash = _money(cash, "cash")
    reported_asset = _money(total_asset, "total_asset")
    if reported_cash < ZERO or reported_asset < ZERO:
        raise ManualLedgerError("snapshot_values_must_be_nonnegative")
    with _lock_for(account_id):
        existing = db.scalars(select(ManualAccountSnapshot).where(
            ManualAccountSnapshot.account_id == account_id,
            ManualAccountSnapshot.idempotency_key == idempotency_key,
        )).first()
        if existing:
            recon = db.scalars(select(ManualReconciliation).where(
                ManualReconciliation.snapshot_id == existing.id,
            )).first()
            if recon is None:
                raise ManualLedgerError("snapshot_reconciliation_missing")
            return recon
        try:
            snapshot = ManualAccountSnapshot(
                id=_new_id(), account_id=account_id, as_of=snapshot_time,
                cash=reported_cash, total_asset=reported_asset,
                idempotency_key=idempotency_key, status="pending", note=note,
            )
            db.add(snapshot)
            db.flush()
            reported: dict[str, Decimal] = {}
            for item in positions:
                code = _normalize_a_share_code(str(item.get("code", "")))
                total_quantity = _quantity(item.get("total_quantity", ZERO), "total_quantity")
                available_quantity = _quantity(item.get("available_quantity", ZERO), "available_quantity")
                avg_cost = _money(item.get("avg_cost", ZERO), "avg_cost")
                market_value = _money(item.get("market_value", ZERO), "market_value")
                if total_quantity < ZERO or available_quantity < ZERO or available_quantity > total_quantity:
                    raise ManualLedgerError("snapshot_position_quantity_invalid")
                db.add(ManualPositionSnapshot(
                    snapshot_id=snapshot.id, code=code, total_quantity=total_quantity,
                    available_quantity=available_quantity, avg_cost=avg_cost,
                    market_value=market_value,
                ))
                reported[code] = total_quantity
            state = get_manual_state(db, account_id)
            ledger_positions = {code: values["quantity"] for code, values in state["positions"].items()}
            all_codes = sorted(set(reported) | set(ledger_positions))
            differences = {
                code: {"reported": reported.get(code, ZERO), "ledger": ledger_positions.get(code, ZERO),
                       "difference": reported.get(code, ZERO) - ledger_positions.get(code, ZERO)}
                for code in all_codes
                if reported.get(code, ZERO) != ledger_positions.get(code, ZERO)
            }
            cash_difference = reported_cash - state["cash"]
            matched = cash_difference == ZERO and not differences
            snapshot.status = "reconciled" if matched else "different"
            reconciliation = ManualReconciliation(
                id=_new_id(), account_id=account_id, snapshot_id=snapshot.id,
                status="matched" if matched else "different",
                position_differences=_canonical_json(differences),
                quantity_differences=_canonical_json(differences),
                cash_difference=cash_difference,
                ledger_checkpoint_hash=state["ledger_checkpoint_hash"],
            )
            db.add(reconciliation)
            account.last_reconciled_at = snapshot_time
            account.status = "active" if matched else "reconcile"
            account.updated_at = manual_now_str()
            db.commit()
            db.refresh(reconciliation)
            return reconciliation
        except Exception:
            db.rollback()
            raise


__all__ = [
    "ManualLedgerError", "create_manual_account", "record_cash_event",
    "record_execution_event", "record_fill_correction", "verify_ledger_hash_chain",
    "get_manual_state", "reconcile_account",
]
