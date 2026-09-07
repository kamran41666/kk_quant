"""Phase 3 live-trading readiness and safety-control endpoints.

These endpoints manage a broker-neutral control plane.  They intentionally do
not submit orders: a reviewed broker adapter and user-provided sandbox are
required before a live gateway can be registered.
"""
from datetime import datetime
import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from server.models.database import get_db
from server.services.live_readiness import (
    cancel_draft,
    capabilities,
    confirm_draft,
    control_status,
    create_order_draft,
    enable_connection,
    list_audit_events,
    export_audit_events,
    list_connections,
    list_drafts,
    register_connection,
    rotate_connection_credential,
    set_kill_switch,
    test_connection,
)
from server.services.live_operations import create_backup, operations_status, verify_backup
from server.services.operator_auth import require_operator

router = APIRouter(prefix="/live", tags=["live-readiness"], dependencies=[Depends(require_operator)])


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BrokerConnectionRequest(_StrictModel):
    provider: str = Field(min_length=2, max_length=80)
    account_ref: str = Field(min_length=1, max_length=120)
    mode: str = Field(default="sandbox", pattern=r"^(sandbox|live)$")
    # Only an opaque env:/keychain: reference is accepted.  The secret itself
    # never crosses the API boundary or enters SQLite.
    credential_ref: Optional[str] = Field(default=None, max_length=160)


class KillSwitchRequest(_StrictModel):
    active: bool
    reason: str = Field(min_length=1, max_length=500)
    confirmation_phrase: Optional[str] = Field(default=None, max_length=80)


class LiveOrderDraftRequest(_StrictModel):
    connection_id: str = Field(min_length=1, max_length=36)
    paper_account_id: str = Field(min_length=1, max_length=36)
    idempotency_key: str = Field(min_length=8, max_length=160)
    code: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    side: str = Field(pattern=r"^(buy|sell)$")
    quantity: int = Field(gt=0, le=10_000_000)
    limit_price: float = Field(gt=0, le=10_000_000)
    price_source: str = Field(default="manual_input", min_length=1, max_length=80)
    price_as_of: Optional[datetime] = None
    price_freshness: str = Field(default="manual", pattern=r"^(manual|realtime|fresh|delayed|stale|unknown)$")

    @field_validator("quantity")
    @classmethod
    def a_share_lot(cls, value: int) -> int:
        if value % 100:
            raise ValueError("A-share drafts must use 100-share lots")
        return value


class ConfirmDraftRequest(_StrictModel):
    confirmation_phrase: str = Field(min_length=1, max_length=80)


class RotateCredentialReferenceRequest(_StrictModel):
    credential_ref: str = Field(min_length=3, max_length=160)


class CancelDraftRequest(_StrictModel):
    reason: str = Field(default="cancelled_by_user", min_length=1, max_length=200)


class BackupVerifyRequest(_StrictModel):
    algorithm: str = Field(min_length=1, max_length=20)
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: dict[str, Any]


def _value_error(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


@router.get("/capabilities")
def get_live_capabilities(db: Session = Depends(get_db)):
    return capabilities(db)


@router.get("/control")
def get_live_control(db: Session = Depends(get_db)):
    return control_status(db)


@router.post("/control/kill-switch")
def update_kill_switch(req: KillSwitchRequest, db: Session = Depends(get_db)):
    try:
        return set_kill_switch(db, active=req.active, reason=req.reason,
                               confirmation_phrase=req.confirmation_phrase)
    except ValueError as exc:
        raise _value_error(exc) from exc


@router.post("/connections")
def create_connection(req: BrokerConnectionRequest, db: Session = Depends(get_db)):
    try:
        return register_connection(db, provider=req.provider, account_ref=req.account_ref,
                                   mode=req.mode, credential_ref=req.credential_ref)
    except ValueError as exc:
        raise _value_error(exc) from exc


@router.get("/connections")
def get_connections(db: Session = Depends(get_db)):
    return list_connections(db)


@router.post("/connections/{connection_id}/test")
def check_connection(connection_id: str, db: Session = Depends(get_db)):
    try:
        return test_connection(db, connection_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/connections/{connection_id}/enable")
def activate_connection(connection_id: str, db: Session = Depends(get_db)):
    try:
        return enable_connection(db, connection_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise _value_error(exc) from exc


@router.post("/connections/{connection_id}/credential-ref/rotate")
def rotate_connection_credential_ref(connection_id: str,
                                     req: RotateCredentialReferenceRequest,
                                     db: Session = Depends(get_db)):
    try:
        return rotate_connection_credential(db, connection_id, credential_ref=req.credential_ref)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise _value_error(exc) from exc


@router.post("/drafts")
def create_draft(req: LiveOrderDraftRequest, db: Session = Depends(get_db)):
    try:
        return create_order_draft(
            db, connection_id=req.connection_id, paper_account_id=req.paper_account_id,
            idempotency_key=req.idempotency_key, code=req.code, side=req.side,
            quantity=req.quantity, limit_price=req.limit_price,
            price_source=req.price_source,
            price_as_of=req.price_as_of.isoformat() if req.price_as_of else None,
            price_freshness=req.price_freshness,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise _value_error(exc) from exc


@router.get("/drafts")
def get_drafts(limit: int = 50, db: Session = Depends(get_db)):
    return list_drafts(db, limit)


@router.post("/drafts/{draft_id}/confirm")
def confirm_live_draft(draft_id: str, req: ConfirmDraftRequest, db: Session = Depends(get_db)):
    try:
        return confirm_draft(db, draft_id, confirmation_phrase=req.confirmation_phrase)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise _value_error(exc) from exc


@router.post("/drafts/{draft_id}/cancel")
def cancel_live_draft(draft_id: str, req: CancelDraftRequest, db: Session = Depends(get_db)):
    try:
        return cancel_draft(db, draft_id, reason=req.reason)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise _value_error(exc) from exc


@router.get("/audit")
def get_audit_events(limit: int = 100, db: Session = Depends(get_db)):
    return list_audit_events(db, limit)


@router.get("/audit/export")
def export_audit_log(format: str = Query(default="json", pattern="^(json|csv)$"),
                     limit: int = Query(default=500, ge=1, le=500),
                     db: Session = Depends(get_db)):
    try:
        body, media_type = export_audit_events(db, limit=limit, format=format)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    extension = "json" if format == "json" else "csv"
    return Response(content=body, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="kk-quant-audit.{extension}"',
                             "Cache-Control": "no-store"})


@router.get("/ops/status")
def get_operations_status(db: Session = Depends(get_db)):
    return operations_status(db)


@router.get("/ops/backup")
def download_safe_backup(db: Session = Depends(get_db)):
    body = json.dumps(create_backup(db), ensure_ascii=False, sort_keys=True, indent=2)
    return Response(content=body, media_type="application/json; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="kk-quant-safe-backup.json"',
                             "Cache-Control": "no-store"})


@router.post("/ops/backup/verify")
def verify_safe_backup(req: BackupVerifyRequest):
    try:
        return verify_backup(req.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
