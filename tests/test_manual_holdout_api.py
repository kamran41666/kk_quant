"""HTTP boundary checks for H2c holdout pre-registration and access facts."""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from server.config import settings
from server.main import app
from server.models.database import Base, get_db
from server.models.schema import ManualHoldoutBinding, ResearchHoldoutAccess, ResearchHoldoutWindow
from server.api.manual_trading import router as manual_trading_router
from tests.manual_portfolio_test_support import build_portfolio_pair


def test_holdout_api_requires_operator_and_strict_requests():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    def override_get_db():
        with factory() as db:
            yield db

    previous = settings.operator_token
    settings.operator_token = "holdout-api-token"
    app.dependency_overrides[get_db] = override_get_db
    headers = {"X-Operator-Token": settings.operator_token}
    try:
        with TestClient(app) as client:
            assert client.post("/api/v1/manual-trading/holdouts", json={}).status_code == 401
            response = client.post(
                "/api/v1/manual-trading/holdouts", headers=headers,
                json={
                    "release_id": "r", "dataset_id": "d", "data_content_hash": "0" * 64,
                    "start_date": "2027-02-01", "end_date": "2027-02-10", "actor": "operator",
                    "policy_hash": "1" * 64,
                },
            )
            assert response.status_code == 422, response.text
            routes = {route.path for route in manual_trading_router.routes}
            assert "/manual-trading/holdouts/{binding_id}/complete" not in routes
    finally:
        app.dependency_overrides.pop(get_db, None)
        settings.operator_token = previous
        Base.metadata.drop_all(engine)


def test_holdout_real_database_binding_metadata_access_and_invalidation(tmp_path):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    pair = build_portfolio_pair(db, tmp_path)

    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    previous_token, previous_root = settings.operator_token, settings.result_dir
    settings.operator_token, settings.result_dir = "holdout-real-token", str(pair["results_root"])
    app.dependency_overrides[get_db] = override_get_db
    headers = {"X-Operator-Token": settings.operator_token}
    try:
        with TestClient(app) as client:
            registered = client.post("/api/v1/manual-trading/portfolio-pairs", headers=headers, json={"baseline_path": "baseline", "stress_path": "stress"})
            assert registered.status_code == 201, registered.text
            pair_data = registered.json()["data"]
            release = client.post("/api/v1/manual-trading/releases", headers={**headers, "Idempotency-Key": "holdout-release-1"}, json={
                "strategy_key": pair["bundle"].strategy_key, "version": "holdout-v1",
                "bundle_hash": pair["bundle"].bundle_hash, "strategy_fingerprint": pair["bundle"].strategy_core_hash,
                "training_artifact_id": pair["training_artifact"].id, "validation_artifact_id": pair["validation_artifact"].id,
                "execution_policy": pair["bundle"].as_dict()["execution_policy"], "risk_policy": {"max_drawdown": "0.2"},
            })
            assert release.status_code == 201, release.text
            release_id = release.json()["data"]["id"]
            research = client.post(f"/api/v1/manual-trading/releases/{release_id}/promotions", headers={**headers, "Idempotency-Key": "holdout-research-1"}, json={
                "target_status": "research_passed", "evidence_refs": {"training_artifact_id": pair["training_artifact"].id, "validation_artifact_id": pair["validation_artifact"].id}, "actor": "operator",
            })
            assert research.status_code == 201, research.text
            portfolio = client.post(f"/api/v1/manual-trading/releases/{release_id}/promotions", headers={**headers, "Idempotency-Key": "holdout-portfolio-1"}, json={
                "target_status": "portfolio_passed", "evidence_refs": {"baseline_artifact_id": pair_data["baseline_artifact_id"], "stress_artifact_id": pair_data["stress_artifact_id"]}, "actor": "operator",
            })
            assert portfolio.status_code == 201, portfolio.text
            payload = {"release_id": release_id, "dataset_id": "holdout-dataset-v1", "data_content_hash": "a" * 64, "start_date": "2027-02-01", "end_date": "2027-02-10", "actor": "operator"}
            created = client.post("/api/v1/manual-trading/holdouts", headers={**headers, "Idempotency-Key": "holdout-create-1"}, json=payload)
            assert created.status_code == 201, created.text
            metadata = created.json()["data"]
            assert metadata["status"] == "sealed"
            assert "protocol_json" not in metadata
            assert client.get(f"/api/v1/manual-trading/holdouts/{metadata['binding_id']}", headers=headers).json()["data"]["status"] == "sealed"
            assert db.query(ResearchHoldoutAccess).count() == 0
            repeated = client.post("/api/v1/manual-trading/holdouts", headers={**headers, "Idempotency-Key": "holdout-create-1"}, json=payload)
            assert repeated.status_code == 201 and repeated.json()["data"] == metadata
            accessed = client.post(f"/api/v1/manual-trading/holdouts/{metadata['binding_id']}/access", headers=headers, json={"actor": "operator", "purpose": "validation"})
            assert accessed.status_code == 201, accessed.text
            assert accessed.json()["evidence_status"] == "access_recorded_only"
            assert accessed.json()["data"]["payload_hash"]
            assert db.query(ResearchHoldoutAccess).count() == 1
            invalidated = client.post(f"/api/v1/manual-trading/holdouts/{metadata['binding_id']}/invalidate", headers=headers, json={"reason": "test stop"})
            assert invalidated.status_code == 200, invalidated.text
            denied = client.post(f"/api/v1/manual-trading/holdouts/{metadata['binding_id']}/access", headers=headers, json={"actor": "operator", "purpose": "late"})
            assert denied.status_code == 409, denied.text
    finally:
        app.dependency_overrides.pop(get_db, None)
        settings.operator_token, settings.result_dir = previous_token, previous_root
        db.close()
        Base.metadata.drop_all(engine)
