"""Strategy observation endpoints (strictly paper-only)."""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from server.models.database import get_db
from server.services.observation import (
    create_observation,
    get_observation,
    list_observation_events,
    list_observations,
    pause_observation,
    resume_observation,
    run_observation_tick,
    retry_rebalance_plan,
    start_observation,
    stop_observation,
)

router = APIRouter(prefix="/paper", tags=["paper-observations"])


class CreateObservationRequest(BaseModel):
    strategy_id: str = Field(min_length=1, max_length=36)
    idempotency_key: str = Field(min_length=8, max_length=160)
    duration_days: int = Field(description="Observation duration: exactly 7 or 30 calendar days")
    allocation_pct: float = Field(default=1.0, gt=0, le=1)
    allocated_capital: Optional[float] = Field(default=None, gt=0)
    auto_trade: bool = True
    start_date: Optional[date] = None

    @field_validator("duration_days")
    @classmethod
    def supported_duration(cls, value: int) -> int:
        if value not in {7, 30}:
            raise ValueError("duration_days must be 7 or 30")
        return value


class ObservationTickRequest(BaseModel):
    as_of: Optional[date] = None


def _domain_error(exc: Exception) -> HTTPException:
    message = str(exc)
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=message)
    return HTTPException(status_code=409, detail=message)


@router.post("/accounts/{account_id}/observations", status_code=201)
def create_paper_observation(account_id: str, req: CreateObservationRequest, db: Session = Depends(get_db)):
    try:
        return create_observation(db, account_id=account_id, **req.model_dump())
    except (KeyError, ValueError) as exc:
        raise _domain_error(exc) from exc


@router.get("/accounts/{account_id}/observations")
def get_paper_observations(account_id: str, db: Session = Depends(get_db)):
    try:
        return list_observations(db, account_id)
    except (KeyError, ValueError) as exc:
        raise _domain_error(exc) from exc


@router.get("/accounts/{account_id}/observations/{observation_id}")
def get_paper_observation(account_id: str, observation_id: str, db: Session = Depends(get_db)):
    try:
        return get_observation(db, account_id, observation_id)
    except (KeyError, ValueError) as exc:
        raise _domain_error(exc) from exc


@router.get("/accounts/{account_id}/observations/{observation_id}/events")
def get_paper_observation_events(account_id: str, observation_id: str, limit: int = 100, db: Session = Depends(get_db)):
    try:
        return list_observation_events(db, account_id, observation_id, limit=limit)
    except (KeyError, ValueError) as exc:
        raise _domain_error(exc) from exc


@router.post("/accounts/{account_id}/observations/{observation_id}/start")
def start_paper_observation(account_id: str, observation_id: str, db: Session = Depends(get_db)):
    try:
        return start_observation(db, account_id, observation_id)
    except (KeyError, ValueError) as exc:
        raise _domain_error(exc) from exc


@router.post("/accounts/{account_id}/observations/{observation_id}/pause")
def pause_paper_observation(account_id: str, observation_id: str, db: Session = Depends(get_db)):
    try:
        return pause_observation(db, account_id, observation_id)
    except (KeyError, ValueError) as exc:
        raise _domain_error(exc) from exc


@router.post("/accounts/{account_id}/observations/{observation_id}/resume")
def resume_paper_observation(account_id: str, observation_id: str, db: Session = Depends(get_db)):
    try:
        return resume_observation(db, account_id, observation_id)
    except (KeyError, ValueError) as exc:
        raise _domain_error(exc) from exc


@router.post("/accounts/{account_id}/observations/{observation_id}/stop")
def stop_paper_observation(account_id: str, observation_id: str, db: Session = Depends(get_db)):
    try:
        return stop_observation(db, account_id, observation_id)
    except (KeyError, ValueError) as exc:
        raise _domain_error(exc) from exc


@router.post("/accounts/{account_id}/observations/{observation_id}/tick")
def tick_paper_observation(account_id: str, observation_id: str, req: Optional[ObservationTickRequest] = None, db: Session = Depends(get_db)):
    try:
        return run_observation_tick(db, account_id=account_id, observation_id=observation_id, as_of=req.as_of if req else None)
    except (KeyError, ValueError) as exc:
        raise _domain_error(exc) from exc


@router.post("/accounts/{account_id}/observations/{observation_id}/plans/{plan_id}/retry")
def retry_paper_rebalance_plan(account_id: str, observation_id: str, plan_id: str, db: Session = Depends(get_db)):
    """Explicitly retry a blocked paper rebalance after operator review."""
    try:
        return retry_rebalance_plan(
            db, account_id=account_id, observation_id=observation_id, plan_id=plan_id,
        )
    except (KeyError, ValueError) as exc:
        raise _domain_error(exc) from exc
