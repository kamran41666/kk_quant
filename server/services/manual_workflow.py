"""Durable engineering workflow demo orchestration.

The service owns only ``workflow_run`` and ``workflow_action``.  It never
calls production promotion, manual-account, paper-order, or broker services.
"""
from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.models.database import ensure_savepoint_transaction
from server.models.manual_workflow import WorkflowAction, WorkflowRun
from server.models.schema import (
    FactorCandidate,
    FactorExperiment,
    ManualAccount,
    ManualDailyReview,
    ManualExecutionPlan,
    ManualProspectivePilot,
    StrategyRelease,
)


class WorkflowError(ValueError):
    pass


class WorkflowConflict(WorkflowError):
    pass


MODE = "engineering_demo"
ACTIONS = {"research", "observe", "plan", "confirm", "fill", "review", "next_day"}
FILL_MODES = {"full", "partial", "unfilled"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=_json_default)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not_json_serializable:{type(value).__name__}")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _runtime() -> Any:
    """Resolve the source-owned runtime lazily so server import stays safe."""
    try:
        module = importlib.import_module("quant_engine.trading.manual_daily_runtime")
    except ModuleNotFoundError as exc:
        if exc.name == "quant_engine.trading.manual_daily_runtime":
            raise WorkflowError("workflow_runtime_unavailable") from exc
        raise
    if not hasattr(module, "create_demo_state") or not hasattr(module, "advance_demo_state"):
        raise WorkflowError("workflow_runtime_contract_invalid")
    return module


def _state_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        result = dict(value)
    else:
        raise WorkflowError("workflow_runtime_state_invalid")
    try:
        _canonical(result)
    except (TypeError, ValueError) as exc:
        raise WorkflowError("workflow_runtime_state_not_serializable") from exc
    return result


def _runtime_transition(state: Mapping[str, Any], action: str, fill_mode: str) -> tuple[dict[str, Any], Any]:
    if action not in ACTIONS:
        raise WorkflowError("workflow_action_invalid")
    if fill_mode not in FILL_MODES:
        raise WorkflowError("workflow_fill_mode_invalid")
    next_state = _runtime().advance_demo_state(dict(state), action=action, fill_mode=fill_mode)
    next_state = _state_payload(next_state)
    result = {"action": action, "stage": next_state.get("stage"), "next_action": next_state.get("next_action")}
    return next_state, result


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _run_data(row: WorkflowRun) -> dict[str, Any]:
    try:
        state = json.loads(row.state_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WorkflowError("workflow_state_corrupt") from exc
    runtime_version = getattr(_runtime(), "VERSION", None)
    state_version = state.get("protocol_version") if isinstance(state, dict) else None
    return {
        "id": row.id, "mode": row.mode, "revision": row.revision, "state": state,
        "runtime_version": runtime_version, "legacy": bool(runtime_version and state_version != runtime_version),
        "created_at": row.created_at, "updated_at": row.updated_at,
    }


def _action_data(row: WorkflowAction) -> dict[str, Any]:
    try:
        result = json.loads(row.result_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WorkflowError("workflow_action_result_corrupt") from exc
    return {"id": row.id, "run_id": row.run_id, "key": row.key, "request_hash": row.request_hash, "from_revision": row.from_revision, "to_revision": row.to_revision, "result": result, "created_at": row.created_at}


def create_run(db: Session, *, seed: int = 7) -> WorkflowRun:
    try:
        seed_value = int(seed)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WorkflowError("workflow_seed_invalid") from exc
    try:
        state = _state_payload(_runtime().create_demo_state(seed=seed_value))
        row = WorkflowRun(mode=MODE, state_json=_canonical(state), revision=0)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    except WorkflowError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


def get_run(db: Session, run_id: str) -> WorkflowRun:
    row = db.get(WorkflowRun, run_id)
    if row is None:
        raise WorkflowError("workflow_run_not_found")
    return row


def list_runs(db: Session) -> list[dict[str, Any]]:
    return [_run_data(row) for row in db.scalars(select(WorkflowRun).order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())).all()]


def advance_run(
    db: Session,
    run_id: str,
    *,
    action: str,
    fill_mode: str = "full",
    expected_revision: int,
    idempotency_key: str,
    actor: str = "demo-user",
) -> dict[str, Any]:
    if action not in ACTIONS:
        raise WorkflowError("workflow_action_invalid")
    if fill_mode not in FILL_MODES:
        raise WorkflowError("workflow_fill_mode_invalid")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
        raise WorkflowError("workflow_expected_revision_invalid")
    key = str(idempotency_key).strip()
    if not key:
        raise WorkflowError("workflow_idempotency_key_required")
    actor = str(actor).strip() or "demo-user"
    request = {"run_id": run_id, "action": action, "fill_mode": fill_mode, "expected_revision": expected_revision, "actor": actor}
    request_hash = _hash(request)
    existing = db.scalars(select(WorkflowAction).where(WorkflowAction.key == key)).first()
    if existing is not None:
        if existing.request_hash != request_hash or existing.run_id != run_id:
            raise WorkflowConflict("workflow_idempotency_conflict")
        return {"run": _run_data(get_run(db, run_id)), "action": _action_data(existing), "idempotent": True}
    try:
        row = get_run(db, run_id)
        state = json.loads(row.state_json)
        if not isinstance(state, dict):
            raise WorkflowError("workflow_state_corrupt")
        runtime_version = getattr(_runtime(), "VERSION", None)
        if runtime_version and state.get("protocol_version") != runtime_version:
            raise WorkflowConflict("workflow_runtime_version_stale")
        if row.revision != expected_revision:
            raise WorkflowConflict("workflow_revision_conflict")
        next_state, result = _runtime_transition(state, action, fill_mode)
        result_json = _canonical(result)
        now = _iso_now()
        ensure_savepoint_transaction(db)
        with db.begin_nested():
            changed = db.execute(update(WorkflowRun).where(
                WorkflowRun.id == run_id, WorkflowRun.revision == expected_revision,
            ).values(state_json=_canonical(next_state), revision=expected_revision + 1, updated_at=now))
            if changed.rowcount != 1:
                raise WorkflowConflict("workflow_revision_conflict")
            action_row = WorkflowAction(
                run_id=run_id, key=key, request_hash=request_hash,
                from_revision=expected_revision, to_revision=expected_revision + 1,
                result_json=result_json, created_at=now,
            )
            db.add(action_row)
            db.flush()
        db.commit()
        db.refresh(action_row)
        return {"run": _run_data(get_run(db, run_id)), "action": _action_data(action_row), "idempotent": False}
    except WorkflowError:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        duplicate = db.scalars(select(WorkflowAction).where(WorkflowAction.key == key)).first()
        if duplicate is not None and duplicate.request_hash == request_hash and duplicate.run_id == run_id:
            return {"run": _run_data(get_run(db, run_id)), "action": _action_data(duplicate), "idempotent": True}
        raise WorkflowConflict("workflow_action_conflict") from exc
    except Exception:
        db.rollback()
        raise


def _latest(rows: list[Any], *, limit: int = 5) -> list[dict[str, Any]]:
    return [{"id": row.id, "title": getattr(row, "name", None) or getattr(row, "strategy_key", None) or getattr(row, "job_type", None) or row.__class__.__name__, "status": getattr(row, "status", None)} for row in rows[:limit]]


def project_overview(db: Session) -> dict[str, Any]:
    candidates = db.scalars(select(FactorCandidate).order_by(FactorCandidate.created_at.desc(), FactorCandidate.id.desc())).all()
    experiments = db.scalars(select(FactorExperiment).order_by(FactorExperiment.created_at.desc(), FactorExperiment.id.desc())).all()
    releases = db.scalars(select(StrategyRelease).order_by(StrategyRelease.created_at.desc(), StrategyRelease.id.desc())).all()
    pilots = db.scalars(select(ManualProspectivePilot).order_by(ManualProspectivePilot.created_at.desc(), ManualProspectivePilot.id.desc())).all()
    accounts = db.scalars(select(ManualAccount).order_by(ManualAccount.created_at.desc(), ManualAccount.id.desc())).all()
    plans = db.scalars(select(ManualExecutionPlan).order_by(ManualExecutionPlan.created_at.desc(), ManualExecutionPlan.id.desc())).all()
    reviews = db.scalars(select(ManualDailyReview).order_by(ManualDailyReview.created_at.desc(), ManualDailyReview.id.desc())).all()
    groups = {"candidates": candidates, "experiments": experiments, "releases": releases, "pilots": pilots, "accounts": accounts, "plans": plans, "reviews": reviews}
    blocks: list[dict[str, Any]] = []
    for name, rows in groups.items():
        for row in rows:
            status = getattr(row, "status", None)
            if status in {"blocked", "failed", "training_rejected", "validation_rejected", "research_blocked", "suspended", "reconcile", "pending"}:
                blocks.append({"module": name, "id": row.id, "status": status, "reason": getattr(row, "blocked_reason", None) or getattr(row, "rejection_reason", None)})
    return {
        "counts": {name: len(rows) for name, rows in groups.items()},
        "latest": {name: _latest(rows) for name, rows in groups.items()},
        "blocks": blocks,
        "entrypoints": [
            {"key": "research", "path": "/factors", "label": "因子研究"},
            {"key": "observation", "path": "/manual", "label": "纸面观察"},
            {"key": "planning", "path": "/manual", "label": "人工计划"},
            {"key": "workflow_demo", "path": "/daily-workflow", "label": "日频流程演示"},
        ],
    }


__all__ = ["WorkflowConflict", "WorkflowError", "advance_run", "create_run", "get_run", "list_runs", "project_overview"]
