"""H2c evaluation API checks against real source files and SQLite."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from server.config import settings
from server.main import app
from server.models.database import get_db
from server.models.database import Base
from tests.manual_holdout_evaluation_test_support import build_holdout_case


def test_holdout_evaluation_api_freezes_strict_input_and_runs_real_case(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db_session = factory()
    case = build_holdout_case(db_session, tmp_path, monkeypatch, profitable=True)

    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    previous_token = settings.operator_token
    settings.operator_token = "holdout-evaluation-api-token"
    app.dependency_overrides[get_db] = override_get_db
    headers = {"X-Operator-Token": settings.operator_token, "Idempotency-Key": case["freeze_kwargs"]["idempotency_key"]}
    try:
        with TestClient(app) as client:
            response = client.post(
                f"/api/v1/manual-trading/holdouts/{case['binding'].id}/evaluations",
                headers=headers,
                json={
                    "actor": case["freeze_kwargs"]["actor"],
                    "dataset_manifest_path": case["freeze_kwargs"]["dataset_manifest_path"],
                    "benchmark_receipt_path": case["freeze_kwargs"]["benchmark_receipt_path"],
                },
            )
            assert response.status_code == 201, response.text
            metadata = response.json()["data"]
            assert metadata["status"] == "pending"
            assert "input_json" not in metadata
            assert str(case["dataset_manifest_path"]) not in response.text

            strict = client.post(
                f"/api/v1/manual-trading/holdouts/{case['binding'].id}/evaluations",
                headers={**headers, "Idempotency-Key": "holdout-evaluation-strict"},
                json={
                    "actor": "operator",
                    "dataset_manifest_path": case["freeze_kwargs"]["dataset_manifest_path"],
                    "benchmark_receipt_path": case["freeze_kwargs"]["benchmark_receipt_path"],
                    "passed": True,
                },
            )
            assert strict.status_code == 422, strict.text
            evaluation_id = metadata["id"]
            run = client.post(
                f"/api/v1/manual-trading/holdout-evaluations/{evaluation_id}/run",
                headers={"X-Operator-Token": settings.operator_token},
                json={"actor": "holdout-fixture"},
            )
            assert run.status_code == 200, run.text
            assert run.json()["data"]["status"] == "completed"
            evidence = client.get(
                f"/api/v1/manual-trading/holdout-evaluations/{evaluation_id}/evidence",
                headers={"X-Operator-Token": settings.operator_token},
            )
            assert evidence.status_code == 200, evidence.text
            assert evidence.json()["data"]["metrics"].keys() == {"baseline", "stress"}
            artifact_id = run.json()["data"]["result_artifact_id"]
            promotion = client.post(
                f"/api/v1/manual-trading/releases/{case['release'].id}/promotions",
                headers={"X-Operator-Token": settings.operator_token, "Idempotency-Key": "holdout-promotion-api"},
                json={
                    "target_status": "holdout_passed",
                    "evidence_refs": {"holdout_artifact_id": artifact_id},
                    "actor": "holdout-fixture",
                },
            )
            assert promotion.status_code == 201, promotion.text
            assert promotion.json()["data"]["decision"] == "passed"
            db_session.expire_all()
            assert db_session.get(type(case["release"]), case["release"].id).status == "holdout_passed"
            evidence_after_promotion = client.get(
                f"/api/v1/manual-trading/holdout-evaluations/{evaluation_id}/evidence",
                headers={"X-Operator-Token": settings.operator_token},
            )
            assert evidence_after_promotion.status_code == 200, evidence_after_promotion.text
            assert evidence_after_promotion.json()["data"]["hashes"] == evidence.json()["data"]["hashes"]
            release_evidence = client.get(
                f"/api/v1/manual-trading/releases/{case['release'].id}/evidence",
                headers={"X-Operator-Token": settings.operator_token},
            )
            assert release_evidence.status_code == 200, release_evidence.text
            body = release_evidence.json()["data"]
            assert any(item["id"] == artifact_id and item["kind"] == "holdout_result" for item in body["artifacts"])
            assert body["evaluations"][-1]["target_status"] == "holdout_passed"
    finally:
        app.dependency_overrides.pop(get_db, None)
        settings.operator_token = previous_token
        db_session.close()
        Base.metadata.drop_all(engine)


def test_negative_completed_holdout_is_verified_but_promotion_blocked(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db_session = factory()
    case = build_holdout_case(db_session, tmp_path, monkeypatch, profitable=False)

    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    previous_token = settings.operator_token
    settings.operator_token = "holdout-negative-api-token"
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            headers = {"X-Operator-Token": settings.operator_token}
            frozen = client.post(
                f"/api/v1/manual-trading/holdouts/{case['binding'].id}/evaluations",
                headers={**headers, "Idempotency-Key": "holdout-negative-evaluation"},
                json={"actor": "negative", "dataset_manifest_path": case["freeze_kwargs"]["dataset_manifest_path"], "benchmark_receipt_path": case["freeze_kwargs"]["benchmark_receipt_path"]},
            )
            assert frozen.status_code == 201, frozen.text
            evaluation_id = frozen.json()["data"]["id"]
            run = client.post(
                f"/api/v1/manual-trading/holdout-evaluations/{evaluation_id}/run",
                headers=headers, json={"actor": "negative"},
            )
            assert run.status_code == 200, run.text
            assert run.json()["data"]["status"] == "completed"
            artifact_id = run.json()["data"]["result_artifact_id"]
            evidence = client.get(
                f"/api/v1/manual-trading/holdout-evaluations/{evaluation_id}/evidence", headers=headers,
            )
            assert evidence.status_code == 200, evidence.text
            from server.models.schema import ResearchHoldoutAccess
            access = db_session.query(ResearchHoldoutAccess).filter_by(window_id=case["binding"].window_id).first()
            access.payload_hash = "0" * 64
            db_session.commit()
            tampered = client.get(
                f"/api/v1/manual-trading/holdout-evaluations/{evaluation_id}/evidence", headers=headers,
            )
            assert tampered.status_code == 409, tampered.text
            blocked = client.post(
                f"/api/v1/manual-trading/releases/{case['release'].id}/promotions",
                headers={**headers, "Idempotency-Key": "holdout-negative-promotion"},
                json={"target_status": "holdout_passed", "evidence_refs": {"holdout_artifact_id": artifact_id}, "actor": "negative"},
            )
            assert blocked.status_code == 201, blocked.text
            assert blocked.json()["data"]["decision"] == "blocked"
            db_session.expire_all()
            assert db_session.get(type(case["release"]), case["release"].id).status == "portfolio_passed"
    finally:
        app.dependency_overrides.pop(get_db, None)
        settings.operator_token = previous_token
        db_session.close()
        Base.metadata.drop_all(engine)


def test_holdout_promotion_cannot_select_one_window_from_pending_set(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db_session = factory()
    case = build_holdout_case(db_session, tmp_path, monkeypatch, profitable=True)

    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    previous_token = settings.operator_token
    settings.operator_token = "holdout-set-api-token"
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            headers = {"X-Operator-Token": settings.operator_token}
            frozen = client.post(
                f"/api/v1/manual-trading/holdouts/{case['binding'].id}/evaluations",
                headers={**headers, "Idempotency-Key": "holdout-set-evaluation"},
                json={"actor": "set", "dataset_manifest_path": case["freeze_kwargs"]["dataset_manifest_path"], "benchmark_receipt_path": case["freeze_kwargs"]["benchmark_receipt_path"]},
            )
            assert frozen.status_code == 201, frozen.text
            evaluation_id = frozen.json()["data"]["id"]
            run = client.post(
                f"/api/v1/manual-trading/holdout-evaluations/{evaluation_id}/run",
                headers=headers, json={"actor": "set"},
            )
            assert run.status_code == 200, run.text
            artifact_id = run.json()["data"]["result_artifact_id"]
            second = client.post(
                "/api/v1/manual-trading/holdouts",
                headers={**headers, "Idempotency-Key": "holdout-set-second"},
                json={"release_id": case["release"].id, "dataset_id": "holdout-second", "data_content_hash": "b" * 64, "start_date": "2027-05-01", "end_date": "2027-05-10", "actor": "set"},
            )
            assert second.status_code == 201, second.text
            blocked = client.post(
                f"/api/v1/manual-trading/releases/{case['release'].id}/promotions",
                headers={**headers, "Idempotency-Key": "holdout-set-promotion"},
                json={"target_status": "holdout_passed", "evidence_refs": {"holdout_artifact_id": artifact_id}, "actor": "set"},
            )
            assert blocked.status_code == 201, blocked.text
            assert blocked.json()["data"]["decision"] == "blocked"
            db_session.expire_all()
            assert db_session.get(type(case["release"]), case["release"].id).status == "portfolio_passed"
    finally:
        app.dependency_overrides.pop(get_db, None)
        settings.operator_token = previous_token
        db_session.close()
        Base.metadata.drop_all(engine)
