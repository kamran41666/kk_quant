"""Engineering workflow demo API."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from server.models.database import get_db
from server.services.manual_workflow import (
    WorkflowConflict,
    WorkflowError,
    _run_data,
    advance_run,
    create_run,
    get_run,
    list_runs,
    project_overview,
)
from server.services.operator_auth import require_operator

router = APIRouter(prefix="/daily-workflow", tags=["daily-workflow"], dependencies=[Depends(require_operator)])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateRunRequest(StrictModel):
    seed: int = Field(default=7)


class AdvanceRequest(StrictModel):
    action: str = Field(pattern=r"^(research|observe|plan|confirm|fill|review|next_day)$")
    fill_mode: str = Field(default="full", pattern=r"^(full|partial|unfilled)$")
    expected_revision: int = Field(ge=0)
    actor: str = Field(default="demo-user", min_length=1, max_length=80)


def _envelope(data: Any, *, mode: str) -> dict[str, Any]:
    return {"data": data, "mode": mode, "live_authorized": False, "broker_connected": False}


def _error(exc: Exception) -> HTTPException:
    message = str(exc) or type(exc).__name__
    status = 409 if isinstance(exc, WorkflowConflict) or "conflict" in message else 400
    return HTTPException(status_code=status, detail={"type": type(exc).__name__, "code": message})


@router.get("/overview")
def overview(db: Session = Depends(get_db)):  # noqa: B008
    try:
        return _envelope(project_overview(db), mode="project_state")
    except (WorkflowError, ValueError) as exc:
        raise _error(exc) from exc


@router.get("/runs")
def runs(db: Session = Depends(get_db)):  # noqa: B008
    try:
        return _envelope(list_runs(db), mode="engineering_demo")
    except (WorkflowError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/runs", status_code=201)
def create_demo_run(req: CreateRunRequest, db: Session = Depends(get_db)):  # noqa: B008
    try:
        return _envelope({"run": _run_data(create_run(db, seed=req.seed))}, mode="engineering_demo")
    except (WorkflowError, ValueError) as exc:
        raise _error(exc) from exc


@router.get("/runs/{run_id}")
def read_run(run_id: str, db: Session = Depends(get_db)):  # noqa: B008
    try:
        row = get_run(db, run_id)
        return _envelope(_run_data(row), mode="engineering_demo")
    except (WorkflowError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/runs/{run_id}/advance")
def advance_demo_run(
    run_id: str,
    req: AdvanceRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=160),
    db: Session = Depends(get_db),  # noqa: B008
):
    try:
        return _envelope(advance_run(
            db, run_id, action=req.action, fill_mode=req.fill_mode,
            expected_revision=req.expected_revision, idempotency_key=idempotency_key,
            actor=req.actor,
        ), mode="engineering_demo")
    except (WorkflowError, ValueError) as exc:
        raise _error(exc) from exc


__all__ = ["router"]
