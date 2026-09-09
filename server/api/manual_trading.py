"""Loopback manual-execution API.

Every write records user-entered facts or a plan.  There is intentionally no
submit/send-to-broker endpoint and no conversion to ``LiveOrderDraft``.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from quant_engine.data.calendar import TradingCalendar
from server.models.database import get_db
from server.models.schema import ManualAccount, ManualDailyJob, ManualDailyReview, ManualExecutionAuthorization, ManualExecutionItem, ManualExecutionPlan, ManualValuation
from server.services.manual_ledger import (
    ManualLedgerError,
    create_manual_account,
    get_manual_state,
    reconcile_account,
    record_cash_event,
    record_execution_event,
    record_fill_correction,
)
from server.services.manual_plan_persistence import mark_plan_viewed
from server.services.manual_review import (
    ManualReviewError,
    create_daily_review,
    queue_research_revision,
    record_corporate_action_fact,
    record_manual_valuation,
)
from server.models.schema import ManualProspectivePilot
from server.services.manual_pilot import (
    ManualPilotError,
    create_pilot,
    finalize_pilot,
    record_pilot_observation,
)
from server.services.manual_authorization import (
    AuthorizationError,
    approve_authorization,
    create_authorization,
)
from server.services.manual_execution_cycle import (
    ManualExecutionCycleError,
    confirm_plan_item,
    record_confirmed_fill,
)
from server.services.operator_auth import require_operator


router = APIRouter(prefix="/manual-trading", tags=["manual-trading"], dependencies=[Depends(require_operator)])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountRequest(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    broker_label: str = Field(default="", max_length=120)


class CashEventRequest(StrictModel):
    event_type: str = Field(pattern=r"^(opening_balance|deposit|withdrawal|interest|fee_adjustment)$")
    amount: Decimal
    occurred_at: datetime | date | str
    correction_of: Optional[str] = None
    note: Optional[str] = Field(default=None, max_length=500)


class ExecutionEventRequest(StrictModel):
    client_event_id: str = Field(min_length=8, max_length=160)
    event_type: str = Field(pattern=r"^(submitted|partial_fill|fill|cancelled|rejected|skipped|fee_adjustment)$")
    code: Optional[str] = Field(default=None, pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    side: Optional[str] = Field(default=None, pattern=r"^(buy|sell)$")
    quantity: Optional[Decimal] = Field(default=None, gt=0)
    price: Optional[Decimal] = Field(default=None, gt=0)
    commission: Decimal = Field(default=Decimal("0"), ge=0)
    stamp_duty: Decimal = Field(default=Decimal("0"), ge=0)
    other_fee: Decimal = Field(default=Decimal("0"), ge=0)
    total_fee: Optional[Decimal] = Field(default=None, ge=0)
    traded_at: Optional[datetime | date | str] = None
    item_id: Optional[str] = None
    cohort_id: Optional[str] = None
    user_trade_ref: Optional[str] = None


class CorrectionRequest(StrictModel):
    replacement_client_event_id: str = Field(min_length=8, max_length=160)
    code: Optional[str] = Field(default=None, pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    side: str = Field(pattern=r"^(buy|sell)$")
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    commission: Decimal = Field(default=Decimal("0"), ge=0)
    stamp_duty: Decimal = Field(default=Decimal("0"), ge=0)
    other_fee: Decimal = Field(default=Decimal("0"), ge=0)
    total_fee: Optional[Decimal] = Field(default=None, ge=0)
    traded_at: datetime | date | str
    item_id: Optional[str] = None
    cohort_id: Optional[str] = None
    user_trade_ref: Optional[str] = None


class PositionInput(StrictModel):
    code: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    total_quantity: Decimal = Field(ge=0)
    available_quantity: Decimal = Field(ge=0)
    avg_cost: Decimal = Field(ge=0)
    market_value: Decimal = Field(ge=0)


class ReconcileRequest(StrictModel):
    idempotency_key: str = Field(min_length=8, max_length=160)
    as_of: datetime | date | str
    cash: Decimal = Field(ge=0)
    total_asset: Decimal = Field(ge=0)
    positions: list[PositionInput] = Field(default_factory=list)
    note: Optional[str] = Field(default=None, max_length=500)


class ValuationRequest(StrictModel):
    idempotency_key: str = Field(min_length=8, max_length=160)
    valuation_date: date | str
    cash: Decimal = Field(ge=0)
    market_value: Decimal = Field(ge=0)
    total_asset: Decimal = Field(ge=0)
    price_as_of: Optional[str] = Field(default=None, max_length=40)


class ReviewRequest(StrictModel):
    review_date: date | str
    valuation_id: Optional[str] = None
    notes: Optional[str] = Field(default=None, max_length=1000)


class RevisionRequest(StrictModel):
    review_date: date | str
    reason_codes: list[str] = Field(min_length=1, max_length=30)
    evidence: dict[str, Any] = Field(default_factory=dict)
    release_id: Optional[str] = None


class CorporateActionRequest(StrictModel):
    idempotency_key: str = Field(min_length=8, max_length=160)
    code: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    action_type: str = Field(min_length=1, max_length=40)
    effective_date: date | str
    factor: Decimal = Field(default=Decimal("1"), gt=0)
    cash_amount: Decimal = Decimal("0")
    note: Optional[str] = Field(default=None, max_length=500)


class PilotCreateRequest(StrictModel):
    release_id: str
    strategy_fingerprint: str = Field(min_length=64, max_length=64)
    start_date: date | str
    data_mode: str = Field(pattern=r"^(real_forward|synthetic_engineering)$")
    pilot_key: Optional[str] = Field(default=None, min_length=8, max_length=180)


class PilotObservationRequest(StrictModel):
    observation_date: date | str
    data_as_of: date | str
    received_at: datetime | str
    input_hash: str = Field(min_length=64, max_length=64)
    signal_hash: str = Field(min_length=64, max_length=64)
    observed_action: str = Field(min_length=1, max_length=30)
    actual_return: Decimal = Decimal("0")
    reconciled: bool = False
    idempotency_key: str = Field(min_length=8, max_length=160)
    notes: Optional[str] = Field(default=None, max_length=1000)


class PilotFinalizeRequest(StrictModel):
    evidence: dict[str, Any] = Field(default_factory=dict)


class AuthorizationRequest(StrictModel):
    release_id: str
    account_id: str
    capital_limit: Decimal = Field(gt=0)
    max_order_notional: Decimal = Field(gt=0)
    max_gross_exposure: Decimal = Field(gt=0, le=1)
    max_single_weight: Decimal = Field(gt=0, le=1)
    max_daily_items: int = Field(gt=0)
    max_daily_loss: Decimal = Field(gt=0)
    max_drawdown: Decimal = Field(gt=0, le=1)
    revocation_policy: dict[str, Any] = Field(default_factory=dict)
    valid_from: str
    valid_until: str


class ApproveAuthorizationRequest(StrictModel):
    approved_by: str = Field(min_length=1, max_length=80)


class ConfirmItemRequest(StrictModel):
    actor: str = Field(min_length=1, max_length=80)


class ConfirmedFillRequest(StrictModel):
    client_event_id: str = Field(min_length=8, max_length=160)
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    traded_at: datetime | date | str
    commission: Decimal = Field(default=Decimal("0"), ge=0)
    stamp_duty: Decimal = Field(default=Decimal("0"), ge=0)
    other_fee: Decimal = Field(default=Decimal("0"), ge=0)
    total_fee: Optional[Decimal] = Field(default=None, ge=0)
    user_trade_ref: Optional[str] = None


class UnfilledItemRequest(StrictModel):
    reason: str = Field(min_length=1, max_length=500)


def _envelope(data: Any, *, as_of: str | None = None, evidence_status: str = "user_reported") -> dict[str, Any]:
    return {
        "data": data, "manual_execution": True, "broker_connected": False,
        "user_reported_fills": True, "live_order_submission": False,
        "as_of": as_of or datetime.now().astimezone().isoformat(), "evidence_status": evidence_status,
    }


def _error(exc: Exception) -> HTTPException:
    message = str(exc)
    if "not_found" in message:
        return HTTPException(status_code=404, detail={"code": message})
    if "idempotency" in message or "conflict" in message:
        return HTTPException(status_code=409, detail={"code": "IDEMPOTENCY_CONFLICT", "message": message})
    return HTTPException(status_code=409, detail={"code": message})


def _account(account: ManualAccount) -> dict[str, Any]:
    return {"id": account.id, "name": account.name, "currency": account.currency, "broker_label": account.broker_label, "confirmed_cash": str(account.confirmed_cash), "status": account.status, "ledger_checkpoint_hash": account.ledger_checkpoint_hash, "created_at": account.created_at, "updated_at": account.updated_at}


def _calendar_for(value: Any) -> TradingCalendar:
    moment = value.date() if isinstance(value, datetime) else value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    calendar = TradingCalendar(start_year=moment.year, end_year=moment.year + 1)
    report = calendar.ensure_coverage(moment, moment + timedelta(days=10))
    if not report.get("complete"):
        raise ManualLedgerError("TRADING_CALENDAR_UNAVAILABLE")
    return calendar


@router.get("/accounts")
def list_manual_accounts(db: Session = Depends(get_db)):
    rows = db.scalars(select(ManualAccount).order_by(ManualAccount.created_at.desc(), ManualAccount.id.desc())).all()
    return _envelope([_account(row) for row in rows])


@router.post("/accounts", status_code=201)
def create_account(req: AccountRequest, idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=160), db: Session = Depends(get_db)):
    try:
        account = create_manual_account(db, req.name, broker_label=req.broker_label, idempotency_key=idempotency_key)
        return _envelope(_account(account), evidence_status="user_reported")
    except (ManualLedgerError, ValueError) as exc:
        raise _error(exc) from exc


@router.get("/accounts/{account_id}/state")
def manual_state(account_id: str, db: Session = Depends(get_db)):
    try:
        state = get_manual_state(db, account_id)
        state["cash"] = str(state["cash"])
        state["positions"] = {
            code: {
                "quantity": str(values["quantity"]),
                "available_quantity": str(values["available_quantity"]),
            }
            for code, values in state["positions"].items()
        }
        return _envelope(state)
    except (ManualLedgerError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/accounts/{account_id}/cash-events", status_code=201)
def add_cash_event(account_id: str, req: CashEventRequest, idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=160), db: Session = Depends(get_db)):
    try:
        row = record_cash_event(db, account_id, idempotency_key=idempotency_key, **req.model_dump())
        return _envelope({"id": row.id, "event_type": row.event_type, "amount": str(row.amount), "source": row.source}, as_of=row.occurred_at)
    except (ManualLedgerError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/accounts/{account_id}/execution-events", status_code=201)
def add_execution_event(account_id: str, req: ExecutionEventRequest, db: Session = Depends(get_db)):
    try:
        payload = req.model_dump()
        calendar = _calendar_for(req.traded_at) if req.event_type in {"fill", "partial_fill"} and req.side == "buy" else None
        row = record_execution_event(db, account_id, calendar=calendar, **payload)
        return _envelope({"id": row.id, "client_event_id": row.client_event_id, "event_type": row.event_type, "source": row.source, "code": row.code, "quantity": str(row.quantity) if row.quantity is not None else None}, as_of=row.traded_at)
    except (ManualLedgerError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/accounts/{account_id}/execution-events/{event_id}/correction", status_code=201)
def correct_execution_event(account_id: str, event_id: str, req: CorrectionRequest, db: Session = Depends(get_db)):
    try:
        payload = req.model_dump()
        calendar = _calendar_for(req.traded_at) if req.side == "buy" else None
        row = record_fill_correction(db, account_id, original_event_id=event_id, calendar=calendar, **payload)
        return _envelope({"id": row.id, "client_event_id": row.client_event_id, "event_type": row.event_type, "source": row.source}, as_of=row.traded_at)
    except (ManualLedgerError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/accounts/{account_id}/reconcile", status_code=201)
def reconcile(account_id: str, req: ReconcileRequest, db: Session = Depends(get_db)):
    try:
        row = reconcile_account(db, account_id, **req.model_dump())
        return _envelope({"id": row.id, "snapshot_id": row.snapshot_id, "status": row.status, "cash_difference": str(row.cash_difference)}, evidence_status=row.status)
    except (ManualLedgerError, ValueError) as exc:
        raise _error(exc) from exc


@router.get("/accounts/{account_id}/plans")
def list_plans(account_id: str, db: Session = Depends(get_db)):
    rows = db.scalars(select(ManualExecutionPlan).where(ManualExecutionPlan.account_id == account_id).order_by(ManualExecutionPlan.execution_date.desc(), ManualExecutionPlan.version.desc())).all()
    result = []
    for row in rows:
        items = db.scalars(select(ManualExecutionItem).where(ManualExecutionItem.plan_id == row.id).order_by(ManualExecutionItem.order_sequence.asc())).all()
        result.append({"id": row.id, "execution_date": row.execution_date, "execution_session": row.execution_session, "plan_type": row.plan_type, "status": row.status, "cash_before": str(row.cash_before), "expected_cash_after": str(row.expected_cash_after), "expected_fees": str(row.expected_fees), "blocked_reason": row.blocked_reason, "items": [{"id": item.id, "code": item.code, "side": item.side, "planned_quantity": str(item.planned_quantity), "reference_price": str(item.reference_price), "price_source": item.price_source, "status": item.status, "reason_codes": item.reason_codes} for item in items]})
    return _envelope(result)


@router.post("/accounts/{account_id}/plans/{plan_id}/view")
def view_plan(account_id: str, plan_id: str, db: Session = Depends(get_db)):
    try:
        row = db.get(ManualExecutionPlan, plan_id)
        if row is None or row.account_id != account_id:
            raise ManualLedgerError("manual_plan_not_found")
        return _envelope({"id": mark_plan_viewed(db, plan_id).id, "status": db.get(ManualExecutionPlan, plan_id).status})
    except (ManualLedgerError, ValueError) as exc:
        raise _error(exc) from exc


def _valuation(row: ManualValuation) -> dict[str, Any]:
    return {
        "id": row.id, "account_id": row.account_id, "valuation_date": row.valuation_date,
        "cash": str(row.cash), "market_value": str(row.market_value), "total_asset": str(row.total_asset),
        "daily_return": str(row.daily_return), "external_cash_flow": str(row.external_cash_flow),
        "pnl": str(row.pnl), "price_source": row.price_source, "price_as_of": row.price_as_of,
        "price_freshness": row.price_freshness, "ledger_checkpoint_hash": row.ledger_checkpoint_hash,
    }


def _review(row: ManualDailyReview) -> dict[str, Any]:
    return {
        "id": row.id, "account_id": row.account_id, "review_date": row.review_date,
        "valuation_id": row.valuation_id, "status": row.status,
        "reconciliation_status": row.reconciliation_status,
        "planned_item_count": row.planned_item_count, "reported_fill_count": row.reported_fill_count,
        "unfilled_item_count": row.unfilled_item_count, "execution_deviation": str(row.execution_deviation),
        "factor_decay_status": row.factor_decay_status, "data_health": row.data_health,
        "notes": row.notes, "review_hash": row.review_hash,
    }


@router.post("/accounts/{account_id}/valuations", status_code=201)
def add_valuation(account_id: str, req: ValuationRequest, db: Session = Depends(get_db)):
    try:
        row = record_manual_valuation(db, account_id=account_id, **req.model_dump())
        return _envelope(_valuation(row), as_of=row.valuation_date)
    except (ManualReviewError, ValueError) as exc:
        raise _error(exc) from exc


@router.get("/accounts/{account_id}/valuations")
def list_valuations(account_id: str, db: Session = Depends(get_db)):
    rows = db.scalars(select(ManualValuation).where(
        ManualValuation.account_id == account_id,
    ).order_by(ManualValuation.valuation_date.desc()).limit(120)).all()
    return _envelope([_valuation(row) for row in rows])


@router.post("/accounts/{account_id}/reviews", status_code=201)
def add_review(account_id: str, req: ReviewRequest, db: Session = Depends(get_db)):
    try:
        row = create_daily_review(db, account_id=account_id, **req.model_dump())
        return _envelope(_review(row), as_of=row.review_date, evidence_status=row.status)
    except (ManualReviewError, ValueError) as exc:
        raise _error(exc) from exc


@router.get("/accounts/{account_id}/reviews")
def list_reviews(account_id: str, db: Session = Depends(get_db)):
    rows = db.scalars(select(ManualDailyReview).where(
        ManualDailyReview.account_id == account_id,
    ).order_by(ManualDailyReview.review_date.desc()).limit(120)).all()
    return _envelope([_review(row) for row in rows])


@router.post("/accounts/{account_id}/research-revisions", status_code=201)
def add_research_revision(account_id: str, req: RevisionRequest, db: Session = Depends(get_db)):
    try:
        row = queue_research_revision(db, account_id=account_id, **req.model_dump())
        return _envelope({"id": row.id, "review_date": row.review_date, "reason_codes": row.reason_codes, "status": row.status, "revision_hash": row.revision_hash}, evidence_status="research_only")
    except (ManualReviewError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/accounts/{account_id}/corporate-actions", status_code=201)
def add_corporate_action(account_id: str, req: CorporateActionRequest, db: Session = Depends(get_db)):
    try:
        row = record_corporate_action_fact(db, account_id=account_id, **req.model_dump())
        return _envelope({"id": row.id, "code": row.code, "action_type": row.action_type, "effective_date": row.effective_date, "source": row.source}, as_of=row.effective_date)
    except (ManualReviewError, ValueError) as exc:
        raise _error(exc) from exc


@router.get("/accounts/{account_id}/jobs")
def list_jobs(account_id: str, db: Session = Depends(get_db)):
    rows = db.scalars(select(ManualDailyJob).where(
        ManualDailyJob.account_id == account_id,
    ).order_by(ManualDailyJob.run_date.desc(), ManualDailyJob.created_at.desc()).limit(120)).all()
    return _envelope([{
        "id": row.id, "job_key": row.job_key, "run_date": row.run_date, "job_type": row.job_type,
        "status": row.status, "attempt_count": row.attempt_count, "blocked_reason": row.blocked_reason,
        "result_hash": row.result_hash,
    } for row in rows])


def _pilot(row: ManualProspectivePilot) -> dict[str, Any]:
    return {
        "id": row.id, "pilot_key": row.pilot_key, "release_id": row.release_id,
        "strategy_fingerprint": row.strategy_fingerprint, "start_date": row.start_date,
        "target_days": row.target_days, "data_mode": row.data_mode, "status": row.status,
        "observation_days": row.observation_days, "valid_days": row.valid_days,
        "report_hash": row.report_hash, "blocked_reason": row.blocked_reason,
        "required_evidence": row.required_evidence,
    }


def _pilot_calendar(value: Any) -> TradingCalendar:
    if isinstance(value, datetime):
        moment = value.date()
    elif isinstance(value, date):
        moment = value
    else:
        moment = date.fromisoformat(str(value)[:10])
    calendar = TradingCalendar(start_year=moment.year, end_year=moment.year + 1)
    report = calendar.ensure_coverage(moment, moment + timedelta(days=90))
    if not report.get("complete"):
        raise ManualPilotError("TRADING_CALENDAR_UNAVAILABLE")
    return calendar


@router.post("/pilots", status_code=201)
def add_pilot(req: PilotCreateRequest, db: Session = Depends(get_db)):
    try:
        row = create_pilot(db, **req.model_dump())
        return _envelope(_pilot(row), evidence_status=row.status)
    except (ManualPilotError, ValueError) as exc:
        raise _error(exc) from exc


@router.get("/pilots")
def list_pilots(db: Session = Depends(get_db)):
    rows = db.scalars(select(ManualProspectivePilot).order_by(ManualProspectivePilot.created_at.desc())).all()
    return _envelope([_pilot(row) for row in rows])


@router.post("/pilots/{pilot_id}/observations", status_code=201)
def add_pilot_observation(pilot_id: str, req: PilotObservationRequest, db: Session = Depends(get_db)):
    try:
        row = record_pilot_observation(db, pilot_id, calendar=_pilot_calendar(req.observation_date), **req.model_dump())
        return _envelope({
            "id": row.id, "pilot_id": row.pilot_id, "observation_date": row.observation_date,
            "data_as_of": row.data_as_of, "received_at": row.received_at,
            "input_hash": row.input_hash, "signal_hash": row.signal_hash,
            "source": row.source, "reconciled": row.reconciled,
        }, as_of=row.data_as_of, evidence_status="observed")
    except (ManualPilotError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/pilots/{pilot_id}/finalize", status_code=201)
def finalize_pilot_report(pilot_id: str, req: PilotFinalizeRequest, db: Session = Depends(get_db)):
    try:
        pilot = db.get(ManualProspectivePilot, pilot_id)
        if pilot is None:
            raise ManualPilotError("manual_pilot_not_found")
        row = finalize_pilot(db, pilot_id, evidence=req.evidence, calendar=_pilot_calendar(pilot.start_date))
        return _envelope(_pilot(row), evidence_status=row.status)
    except (ManualPilotError, ValueError) as exc:
        raise _error(exc) from exc


def _authorization(row: Any) -> dict[str, Any]:
    return {
        "id": row.id, "release_id": row.release_id, "account_id": row.account_id,
        "status": row.status, "capital_limit": str(row.capital_limit),
        "max_order_notional": str(row.max_order_notional), "max_gross_exposure": str(row.max_gross_exposure),
        "max_single_weight": str(row.max_single_weight), "max_daily_items": row.max_daily_items,
        "max_daily_loss": str(row.max_daily_loss), "max_drawdown": str(row.max_drawdown),
        "valid_from": row.valid_from, "valid_until": row.valid_until,
        "first_fill_event_id": row.first_fill_event_id, "approved_by": row.approved_by,
    }


@router.post("/authorizations", status_code=201)
def add_authorization(req: AuthorizationRequest, db: Session = Depends(get_db)):
    try:
        row = create_authorization(db, **req.model_dump())
        return _envelope(_authorization(row), evidence_status=row.status)
    except (AuthorizationError, ValueError) as exc:
        raise _error(exc) from exc


@router.get("/accounts/{account_id}/authorizations")
def list_authorizations(account_id: str, db: Session = Depends(get_db)):
    rows = db.scalars(select(ManualExecutionAuthorization).where(
        ManualExecutionAuthorization.account_id == account_id,
    ).order_by(ManualExecutionAuthorization.created_at.desc())).all()
    return _envelope([_authorization(row) for row in rows])


@router.post("/authorizations/{authorization_id}/approve")
def approve_authorization_route(authorization_id: str, req: ApproveAuthorizationRequest, db: Session = Depends(get_db)):
    try:
        row = approve_authorization(db, authorization_id, approved_by=req.approved_by)
        return _envelope(_authorization(row), evidence_status=row.status)
    except (AuthorizationError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/accounts/{account_id}/plans/{plan_id}/items/{item_id}/confirm")
def confirm_item_route(account_id: str, plan_id: str, item_id: str, req: ConfirmItemRequest, db: Session = Depends(get_db)):
    try:
        plan = db.get(ManualExecutionPlan, plan_id)
        item = db.get(ManualExecutionItem, item_id)
        if plan is None or item is None or item.plan_id != plan_id or plan.account_id != account_id:
            raise ManualExecutionCycleError("manual_plan_item_not_found")
        row = confirm_plan_item(db, plan_id=plan_id, item_id=item_id, actor=req.actor)
        return _envelope({"id": row.id, "plan_id": row.plan_id, "item_id": row.item_id, "actor": row.actor, "confirmed_at": row.confirmed_at, "confirmation_hash": row.confirmation_hash}, evidence_status="user_confirmed")
    except (ManualExecutionCycleError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/accounts/{account_id}/plans/{plan_id}/items/{item_id}/fill", status_code=201)
def confirmed_fill_route(account_id: str, plan_id: str, item_id: str, req: ConfirmedFillRequest, db: Session = Depends(get_db)):
    try:
        item = db.get(ManualExecutionItem, item_id)
        if item is None or db.get(ManualExecutionPlan, plan_id) is None or item.plan_id != plan_id:
            raise ManualExecutionCycleError("manual_plan_item_not_found")
        plan = db.get(ManualExecutionPlan, plan_id)
        if plan.account_id != account_id:
            raise ManualExecutionCycleError("manual_account_not_found")
        calendar = _calendar_for(req.traded_at) if item.side == "buy" else None
        row = record_confirmed_fill(db, plan_id=plan_id, item_id=item_id, calendar=calendar, **req.model_dump())
        return _envelope({"id": row.id, "client_event_id": row.client_event_id, "item_id": row.item_id, "source": row.source, "event_type": row.event_type}, as_of=row.traded_at)
    except (ManualExecutionCycleError, ManualLedgerError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/accounts/{account_id}/plans/{plan_id}/items/{item_id}/unfilled")
def unfilled_item_route(account_id: str, plan_id: str, item_id: str, req: UnfilledItemRequest, db: Session = Depends(get_db)):
    try:
        plan = db.get(ManualExecutionPlan, plan_id)
        if plan is None or plan.account_id != account_id:
            raise ManualExecutionCycleError("manual_plan_item_not_found")
        from server.services.manual_execution_cycle import mark_item_unfilled
        row = mark_item_unfilled(db, plan_id=plan_id, item_id=item_id, reason=req.reason)
        return _envelope({"id": row.id, "plan_id": row.plan_id, "status": row.status, "reason_codes": row.reason_codes}, evidence_status="user_reported")
    except (ManualExecutionCycleError, ValueError) as exc:
        raise _error(exc) from exc


__all__ = ["router"]
