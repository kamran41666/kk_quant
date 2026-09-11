"""Engineering daily-workflow API and persistence contract tests."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from server.config import settings
from server.main import app
from server.models.database import Base, get_db
from server.models.manual_workflow import WorkflowAction
from server.models.schema import (
    ManualAccount,
    ManualExecutionPlan,
    ManualProspectivePilot,
    StrategyRelease,
)
from server.services.manual_workflow import (
    WorkflowConflict,
    advance_run,
    create_run,
    get_run,
)


def _db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def test_service_runs_full_demo_and_persists_without_real_rows():
    engine, factory = _db()
    db = factory()
    try:
        run = create_run(db, seed=7)
        actions = ["research", "observe", "plan", "confirm", "fill", "review", "next_day", "confirm", "fill", "review"]
        revision = 0
        for index, action in enumerate(actions):
            result = advance_run(
                db, run.id, action=action, fill_mode="partial" if action == "fill" else "full",
                expected_revision=revision, idempotency_key=f"workflow-{index}",
            )
            revision += 1
            assert result["run"]["revision"] == revision
            assert result["action"]["result"]["action"] == action
            if action == "fill":
                replay_fill = advance_run(
                    db, run.id, action=action, fill_mode="partial",
                    expected_revision=revision - 1, idempotency_key=f"workflow-{index}",
                )
                assert replay_fill["idempotent"] is True
                assert replay_fill["run"]["state"]["fills"] == result["run"]["state"]["fills"]
        fills = len(result["run"]["state"]["fills"])
        repeated = advance_run(
            db, run.id, action="review", fill_mode="full", expected_revision=revision - 1,
            idempotency_key="workflow-9",
        )
        assert repeated["idempotent"] is True
        assert len(repeated["run"]["state"]["fills"]) == fills
        assert db.query(WorkflowAction).filter_by(run_id=run.id).count() == len(actions)
        assert db.query(StrategyRelease).count() == 0
        assert db.query(ManualProspectivePilot).count() == 0
        assert db.query(ManualAccount).count() == 0
        assert db.query(ManualExecutionPlan).count() == 0
        db.close()
        recovered = factory()
        assert get_run(recovered, run.id).revision == len(actions)
        recovered.close()
    finally:
        engine.dispose()


def test_daily_workflow_api_exposes_overview_and_full_actions():
    engine, factory = _db()

    def override_get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    previous = settings.operator_token
    settings.operator_token = "workflow-token"
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            headers = {"X-Operator-Token": settings.operator_token}
            overview = client.get("/api/v1/daily-workflow/overview", headers=headers)
            assert overview.status_code == 200
            assert overview.json()["mode"] == "project_state"
            created = client.post("/api/v1/daily-workflow/runs", headers=headers, json={"seed": 7})
            assert created.status_code == 201
            run_id = created.json()["data"]["run"]["id"]
            revision = 0
            for index, action in enumerate(["research", "observe", "plan", "confirm", "fill", "review", "next_day"]):
                response = client.post(
                    f"/api/v1/daily-workflow/runs/{run_id}/advance",
                    headers={**headers, "Idempotency-Key": f"api-workflow-{index}"},
                    json={"action": action, "fill_mode": "full", "expected_revision": revision},
                )
                assert response.status_code == 200, response.text
                revision += 1
            duplicate = client.post(
                f"/api/v1/daily-workflow/runs/{run_id}/advance",
                headers={**headers, "Idempotency-Key": "api-workflow-6"},
                json={"action": "next_day", "fill_mode": "full", "expected_revision": 6},
            )
            assert duplicate.status_code == 200 and duplicate.json()["data"]["idempotent"] is True
            stale = client.post(
                f"/api/v1/daily-workflow/runs/{run_id}/advance",
                headers={**headers, "Idempotency-Key": "api-workflow-stale"},
                json={"action": "next_day", "fill_mode": "full", "expected_revision": 0},
            )
            assert stale.status_code == 409
            readback = client.get(f"/api/v1/daily-workflow/runs/{run_id}", headers=headers)
            assert readback.status_code == 200
            assert readback.json()["data"]["revision"] == revision
    finally:
        app.dependency_overrides.pop(get_db, None)
        settings.operator_token = previous
        engine.dispose()


def test_old_runtime_run_is_readable_legacy_but_cannot_advance():
    engine, factory = _db()
    db = factory()
    try:
        run = create_run(db, seed=7)
        state = json.loads(run.state_json)
        state["protocol_version"] = "manual-daily-runtime-v1-legacy"
        run.state_json = json.dumps(state, sort_keys=True)
        db.commit()
        assert get_run(db, run.id).revision == 0
        from server.services.manual_workflow import _run_data
        assert _run_data(get_run(db, run.id))["legacy"] is True
        try:
            advance_run(db, run.id, action="research", expected_revision=0, idempotency_key="legacy-run-advance")
        except WorkflowConflict as exc:
            assert str(exc) == "workflow_runtime_version_stale"
        else:
            raise AssertionError("legacy runtime run advanced")
    finally:
        db.close()
        engine.dispose()
