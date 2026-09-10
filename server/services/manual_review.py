"""Manual daily valuation, review, deviation and research-revision services."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.models.schema import (
    ManualAccount,
    ManualAccountSnapshot,
    ManualCashEvent,
    ManualCorporateActionFact,
    ManualDailyReview,
    ManualExecutionEvent,
    ManualExecutionItem,
    ManualExecutionPlan,
    ManualReconciliation,
    ManualValuation,
    ResearchRevision,
)


class ManualReviewError(ValueError):
    pass


ZERO = Decimal("0")
MONEY_QUANT = Decimal("0.00000001")
RATE_QUANT = Decimal("0.0000000001")
CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _date(value: date | datetime | str, field: str) -> date:
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
        raise ManualReviewError(f"{field}_must_be_iso_date") from exc


def _money(value: Any, field: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ManualReviewError(f"{field}_must_be_numeric") from exc
    if not result.is_finite():
        raise ManualReviewError(f"{field}_must_be_finite")
    return result.quantize(MONEY_QUANT)


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _hash(value: Any) -> str:
    encoded = json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _account(db: Session, account_id: str) -> ManualAccount:
    account = db.get(ManualAccount, account_id)
    if account is None:
        raise ManualReviewError("manual_account_not_found")
    if account.status in {"closed", "suspended"}:
        raise ManualReviewError(f"manual_account_{account.status}")
    return account


def record_manual_valuation(
    db: Session,
    *,
    account_id: str,
    valuation_date: date | str,
    cash: Any,
    market_value: Any,
    total_asset: Any,
    idempotency_key: str,
    price_as_of: str | None = None,
    note: str | None = None,
) -> ManualValuation:
    account = _account(db, account_id)
    day = _date(valuation_date, "valuation_date")
    if day > datetime.now(timezone.utc).date():
        raise ManualReviewError("valuation_date_in_future")
    if not str(idempotency_key).strip():
        raise ManualReviewError("valuation_idempotency_key_required")
    cash_value, market_value_value, asset_value = (
        _money(cash, "cash"), _money(market_value, "market_value"), _money(total_asset, "total_asset")
    )
    if min(cash_value, market_value_value, asset_value) < ZERO:
        raise ManualReviewError("valuation_values_must_be_nonnegative")
    if cash_value + market_value_value != asset_value:
        raise ManualReviewError("valuation_components_must_sum")
    existing = db.scalars(select(ManualValuation).where(ManualValuation.idempotency_key == idempotency_key)).first()
    if existing:
        same = (existing.account_id == account_id and existing.valuation_date == day.isoformat()
                and existing.cash == cash_value and existing.market_value == market_value_value
                and existing.total_asset == asset_value)
        if not same:
            raise ManualReviewError("valuation_idempotency_conflict")
        return existing
    same_day = db.scalars(select(ManualValuation).where(
        ManualValuation.account_id == account_id, ManualValuation.valuation_date == day.isoformat(),
    )).first()
    if same_day:
        raise ManualReviewError("valuation_date_already_recorded")
    previous = db.scalars(select(ManualValuation).where(
        ManualValuation.account_id == account_id, ManualValuation.valuation_date < day.isoformat(),
    ).order_by(ManualValuation.valuation_date.desc()).limit(1)).first()
    previous_asset = _money(previous.total_asset, "previous_total_asset") if previous else ZERO
    flow = ZERO
    if previous:
        events = [
            event for event in db.scalars(select(ManualCashEvent).where(
                ManualCashEvent.account_id == account_id,
            )).all()
            if _date(previous.valuation_date, "previous_valuation_date")
            < _date(event.occurred_at, "cash_event_date") <= day
        ]
        flow = sum((_money(event.amount, "cash_event_amount") for event in events), ZERO)
    pnl = asset_value - previous_asset - flow if previous else ZERO
    daily_return = (pnl / previous_asset).quantize(RATE_QUANT) if previous and previous_asset > ZERO else ZERO
    row = ManualValuation(
        account_id=account_id, valuation_date=day.isoformat(), cash=cash_value,
        market_value=market_value_value, total_asset=asset_value, daily_return=daily_return,
        external_cash_flow=flow, pnl=pnl, price_source="user_reported",
        price_as_of=price_as_of, price_freshness="user_reported", ledger_checkpoint_hash=account.ledger_checkpoint_hash,
        idempotency_key=idempotency_key,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _execution_stats(db: Session, account_id: str, day: date) -> dict[str, Any]:
    plans = db.scalars(select(ManualExecutionPlan).where(
        ManualExecutionPlan.account_id == account_id, ManualExecutionPlan.execution_date == day.isoformat(),
        ManualExecutionPlan.status.notin_({"draft", "blocked", "cancelled", "expired", "superseded"}),
    )).all()
    items: list[ManualExecutionItem] = []
    for plan in plans:
        items.extend(db.scalars(select(ManualExecutionItem).where(ManualExecutionItem.plan_id == plan.id)).all())
    events = [event for event in db.scalars(select(ManualExecutionEvent).where(
        ManualExecutionEvent.account_id == account_id,
        ManualExecutionEvent.event_type.in_(["fill", "partial_fill", "fill_correction", "fill_reversal"]),
    )).all() if event.traded_at and _date(event.traded_at, "execution_event_date") == day]
    planned_notional = sum((_money(item.expected_notional, "expected_notional") for item in items), ZERO)
    actual_notional = sum((
        (-1 if event.event_type == "fill_reversal" else 1)
        * _money(event.quantity or ZERO, "fill_quantity")
        * _money(event.price or ZERO, "fill_price")
        for event in events
    ), ZERO)
    return {
        "planned_item_count": len(items),
        "reported_fill_count": len(events),
        "unfilled_item_count": sum(1 for item in items if item.status not in {"filled", "partially_filled"}),
        "execution_deviation": actual_notional - planned_notional,
    }


def create_daily_review(
    db: Session,
    *,
    account_id: str,
    review_date: date | str,
    valuation_id: str | None = None,
    notes: str | None = None,
) -> ManualDailyReview:
    account = _account(db, account_id)
    day = _date(review_date, "review_date")
    valuation = db.get(ManualValuation, valuation_id) if valuation_id else db.scalars(select(ManualValuation).where(
        ManualValuation.account_id == account_id, ManualValuation.valuation_date == day.isoformat(),
    )).first()
    if valuation is None or valuation.account_id != account_id or valuation.valuation_date != day.isoformat():
        raise ManualReviewError("manual_valuation_required_before_review")
    existing = db.scalars(select(ManualDailyReview).where(
        ManualDailyReview.account_id == account_id, ManualDailyReview.review_date == day.isoformat(),
    )).first()
    if existing:
        if existing.valuation_id != valuation.id:
            raise ManualReviewError("daily_review_already_recorded")
        return existing
    reconciliation = db.scalars(select(ManualReconciliation).where(
        ManualReconciliation.account_id == account_id,
    ).order_by(ManualReconciliation.calculated_at.desc()).limit(1)).first()
    reconciliation_status = reconciliation.status if reconciliation else "not_run"
    snapshot = db.get(ManualAccountSnapshot, reconciliation.snapshot_id) if reconciliation else None
    evidence_matches = bool(
        reconciliation
        and reconciliation_status in {"matched", "resolved"}
        and snapshot is not None
        and snapshot.as_of[:10] == day.isoformat()
        and reconciliation.ledger_checkpoint_hash == valuation.ledger_checkpoint_hash
        and valuation.ledger_checkpoint_hash == account.ledger_checkpoint_hash
        and _money(valuation.cash, "valuation_cash") == _money(account.confirmed_cash, "account_cash")
    )
    stats = _execution_stats(db, account_id, day)
    status = "ready" if account.status == "active" and evidence_matches else "blocked"
    payload = {
        "account_id": account_id, "review_date": day, "valuation_id": valuation.id,
        "status": status, "reconciliation_status": reconciliation_status, **stats,
        "factor_decay_status": "not_evaluated", "data_health": "user_reported", "notes": notes,
    }
    row = ManualDailyReview(
        account_id=account_id, review_date=day.isoformat(), valuation_id=valuation.id, status=status,
        reconciliation_status=reconciliation_status, planned_item_count=stats["planned_item_count"],
        reported_fill_count=stats["reported_fill_count"], unfilled_item_count=stats["unfilled_item_count"],
        execution_deviation=stats["execution_deviation"], factor_decay_status="not_evaluated",
        data_health="user_reported", notes=notes, review_hash=_hash(payload),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def queue_research_revision(
    db: Session,
    *,
    review_date: date | str,
    reason_codes: list[str],
    evidence: Mapping[str, Any],
    release_id: str | None = None,
    account_id: str | None = None,
) -> ResearchRevision:
    day = _date(review_date, "review_date")
    reasons = sorted({str(item).strip() for item in reason_codes if str(item).strip()})
    if not reasons:
        raise ManualReviewError("research_revision_reason_required")
    evidence_json = _canonical(dict(evidence))
    revision_hash = _hash({"release_id": release_id, "account_id": account_id, "review_date": day, "reason_codes": reasons, "evidence": evidence_json})
    existing = db.scalars(select(ResearchRevision).where(ResearchRevision.revision_hash == revision_hash)).first()
    if existing:
        return existing
    row = ResearchRevision(
        release_id=release_id, account_id=account_id, review_date=day.isoformat(),
        reason_codes=json.dumps(reasons, ensure_ascii=False), evidence=json.dumps(evidence_json, ensure_ascii=False, sort_keys=True),
        revision_hash=revision_hash, status="queued",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def record_corporate_action_fact(
    db: Session,
    *,
    account_id: str,
    code: str,
    action_type: str,
    effective_date: date | str,
    idempotency_key: str,
    factor: Any = Decimal("1"),
    cash_amount: Any = ZERO,
    note: str | None = None,
) -> ManualCorporateActionFact:
    _account(db, account_id)
    if not CODE_RE.fullmatch(str(code).upper()):
        raise ManualReviewError("manual_corporate_action_code_invalid")
    if not str(action_type).strip() or not str(idempotency_key).strip():
        raise ManualReviewError("corporate_action_type_and_idempotency_required")
    day = _date(effective_date, "effective_date")
    normalized_factor = _money(factor, "factor")
    if normalized_factor <= ZERO:
        raise ManualReviewError("corporate_action_factor_must_be_positive")
    amount = _money(cash_amount, "cash_amount")
    existing = db.scalars(select(ManualCorporateActionFact).where(ManualCorporateActionFact.idempotency_key == idempotency_key)).first()
    if existing:
        if existing.account_id != account_id or existing.code != str(code).upper() or existing.effective_date != day.isoformat():
            raise ManualReviewError("corporate_action_idempotency_conflict")
        return existing
    row = ManualCorporateActionFact(
        account_id=account_id, code=str(code).upper(), action_type=str(action_type).strip(),
        effective_date=day.isoformat(), factor=normalized_factor, cash_amount=amount,
        source="user_reported", idempotency_key=idempotency_key, note=note,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


__all__ = [
    "ManualReviewError", "record_manual_valuation", "create_daily_review",
    "queue_research_revision", "record_corporate_action_fact",
]
