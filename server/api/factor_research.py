"""Restricted factor candidate and persistent experiment endpoints."""
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from quant_engine.factor.expression import FactorExpressionError
from server.models.database import get_db
from server.models.schema import FactorCandidate, FactorExperiment
from server.services.factor_research import (
    generate_candidates,
    list_candidates,
    list_experiments,
    queue_experiment,
    research_memory,
    register_candidate,
    retry_experiment,
    serialize_candidate,
    serialize_experiment,
)


router = APIRouter(prefix="/factor-research", tags=["factor-research"])


class CandidateRequest(BaseModel):
    name: str = Field(min_length=3, max_length=80)
    hypothesis: str = Field(min_length=1, max_length=1000)
    direction: int
    role: str
    expression: dict[str, Any]
    source: str = Field(default="human", min_length=1, max_length=120)
    parent_hash: str | None = None
    protocol_version: str = "1.0"


class ExperimentRequest(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=36)
    dataset_id: str = Field(min_length=1, max_length=160)
    start_date: date
    end_date: date
    forward_horizon: int = Field(default=5, ge=1, le=60)
    stage: str = Field(default="training", pattern=r"^(training|validation|holdout)$")
    evaluation_policy: dict[str, Any] = Field(default_factory=dict)


class GenerateRequest(BaseModel):
    generator: str = Field(default="template-v1", min_length=1, max_length=80)
    limit: int = Field(default=4, ge=1, le=50)


def _domain_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))


@router.post("/candidates", status_code=201)
def create_candidate(req: CandidateRequest, db: Session = Depends(get_db)):
    try:
        row, created = register_candidate(db, req.model_dump())
        return {"created": created, "candidate": serialize_candidate(row)}
    except (FactorExpressionError, ValueError) as exc:
        raise _domain_error(exc) from exc


@router.get("/candidates")
def get_candidates(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    return {"data": list_candidates(db, limit=limit)}


@router.post("/candidates/generate")
def generate_factor_candidates(
    req: GenerateRequest,
    db: Session = Depends(get_db),
):
    try:
        return generate_candidates(
            db, generator_name=req.generator, limit=req.limit
        )
    except (KeyError, ValueError) as exc:
        raise _domain_error(exc) from exc


@router.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: str, db: Session = Depends(get_db)):
    row = db.get(FactorCandidate, candidate_id)
    if row is None:
        raise HTTPException(status_code=404, detail="factor candidate not found")
    return serialize_candidate(row)


@router.post("/experiments", status_code=201)
def create_experiment(req: ExperimentRequest, db: Session = Depends(get_db)):
    try:
        row, created = queue_experiment(
            db,
            candidate_id=req.candidate_id,
            dataset_id=req.dataset_id,
            start=req.start_date,
            end=req.end_date,
            forward_horizon=req.forward_horizon,
            stage=req.stage,
            evaluation_policy=req.evaluation_policy,
        )
        return {"created": created, "experiment": serialize_experiment(row)}
    except (KeyError, ValueError) as exc:
        raise _domain_error(exc) from exc


@router.get("/experiments")
def get_experiments(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    return {"data": list_experiments(db, limit=limit)}


@router.get("/memory")
def get_research_memory(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    return research_memory(db, limit=limit)


@router.get("/experiments/{experiment_id}")
def get_experiment(experiment_id: str, db: Session = Depends(get_db)):
    row = db.get(FactorExperiment, experiment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="factor experiment not found")
    return serialize_experiment(row)


@router.post("/experiments/{experiment_id}/retry")
def retry_failed_experiment(experiment_id: str, db: Session = Depends(get_db)):
    try:
        return serialize_experiment(retry_experiment(db, experiment_id))
    except (KeyError, ValueError) as exc:
        raise _domain_error(exc) from exc
