"""HTTP boundary tests for manual portfolio evidence registration."""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from server.config import settings
from server.main import app
from server.models.database import Base, get_db
from server.models.schema import (
    ManualAccount, PaperAccount, PaperFill, PaperOrder, ResearchEvidenceArtifact,
    StrategyPromotionEvaluation, StrategyRelease,
)
from tests.manual_portfolio_test_support import build_portfolio_pair


def _client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    return engine


def test_portfolio_pair_requires_operator_and_strict_relative_paths():
    engine = _client()
    previous_token = settings.operator_token
    settings.operator_token = "portfolio-api-test-token"
    headers = {"X-Operator-Token": settings.operator_token}
    try:
        with TestClient(app) as client:
            assert client.post(
                "/api/v1/manual-trading/portfolio-pairs",
                json={"baseline_path": "baseline", "stress_path": "stress"},
            ).status_code == 401

            for payload in (
                {"baseline_path": "baseline", "stress_path": "stress", "allowed_root": "/tmp"},
                {"baseline_path": "/tmp/baseline", "stress_path": "stress"},
                {"baseline_path": "baseline/../x", "stress_path": "stress"},
                {"baseline_path": "C:\\evidence\\baseline", "stress_path": "stress"},
                {"baseline_path": "baseline", "stress_path": ""},
            ):
                response = client.post(
                    "/api/v1/manual-trading/portfolio-pairs",
                    headers=headers,
                    json=payload,
                )
                assert response.status_code == 422, response.text
    finally:
        app.dependency_overrides.pop(get_db, None)
        settings.operator_token = previous_token
        Base.metadata.drop_all(engine)


def test_portfolio_pair_real_registration_idempotency_and_evidence_readback(tmp_path):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = factory()
    pair = build_portfolio_pair(db, tmp_path)

    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    previous_token, previous_result_dir = settings.operator_token, settings.result_dir
    settings.operator_token = "portfolio-api-real-token"
    settings.result_dir = str(pair["results_root"])
    app.dependency_overrides[get_db] = override_get_db
    headers = {"X-Operator-Token": settings.operator_token}
    try:
        before = {
            "artifacts": db.query(ResearchEvidenceArtifact).count(),
            "releases": db.query(StrategyRelease).count(),
            "evaluations": db.query(StrategyPromotionEvaluation).count(),
            "accounts": db.query(ManualAccount).count(),
            "paper_accounts": db.query(PaperAccount).count(),
            "paper_orders": db.query(PaperOrder).count(),
            "paper_fills": db.query(PaperFill).count(),
        }
        with TestClient(app) as client:
            first = client.post(
                "/api/v1/manual-trading/portfolio-pairs", headers=headers,
                json={"baseline_path": "baseline", "stress_path": "stress"},
            )
            assert first.status_code == 201, first.text
            first_data = first.json()["data"]
            assert first.json()["evidence_status"] == "registered_not_promoted"
            assert set(first_data) == {"pair_hash", "baseline_artifact_id", "stress_artifact_id"}
            assert db.query(ResearchEvidenceArtifact).count() == before["artifacts"] + 2

            second = client.post(
                "/api/v1/manual-trading/portfolio-pairs", headers=headers,
                json={"baseline_path": "baseline", "stress_path": "stress"},
            )
            assert second.status_code == 201, second.text
            assert second.json()["data"] == first_data
            assert db.query(ResearchEvidenceArtifact).count() == before["artifacts"] + 2
            assert db.query(StrategyRelease).count() == before["releases"]
            assert db.query(ManualAccount).count() == before["accounts"]
            assert db.query(PaperAccount).count() == before["paper_accounts"]
            assert db.query(PaperOrder).count() == before["paper_orders"]
            assert db.query(PaperFill).count() == before["paper_fills"]

            release = client.post(
                "/api/v1/manual-trading/releases", headers={**headers, "Idempotency-Key": "api-release-001"},
                json={
                    "strategy_key": pair["bundle"].strategy_key, "version": "api-v1",
                    "bundle_hash": pair["bundle"].bundle_hash,
                    "strategy_fingerprint": pair["bundle"].strategy_core_hash,
                    "training_artifact_id": pair["training_artifact"].id,
                    "validation_artifact_id": pair["validation_artifact"].id,
                    "execution_policy": pair["bundle"].as_dict()["execution_policy"],
                    "risk_policy": {"max_drawdown": "0.2"},
                },
            )
            assert release.status_code == 201, release.text
            release_id = release.json()["data"]["id"]
            research_refs = {
                "training_artifact_id": pair["training_artifact"].id,
                "validation_artifact_id": pair["validation_artifact"].id,
            }
            research = client.post(
                f"/api/v1/manual-trading/releases/{release_id}/promotions",
                headers={**headers, "Idempotency-Key": "api-research-001"},
                json={"target_status": "research_passed", "evidence_refs": research_refs, "actor": "api-test"},
            )
            assert research.status_code == 201, research.text
            assert research.json()["data"]["decision"] == "passed", research.json()
            research_data = research.json()["data"]
            portfolio = client.post(
                f"/api/v1/manual-trading/releases/{release_id}/promotions",
                headers={**headers, "Idempotency-Key": "api-portfolio-001"},
                json={
                    "target_status": "portfolio_passed",
                    "evidence_refs": {
                        "baseline_artifact_id": first_data["baseline_artifact_id"],
                        "stress_artifact_id": first_data["stress_artifact_id"],
                    },
                    "actor": "api-test",
                },
            )
            assert portfolio.status_code == 201, portfolio.text
            assert portfolio.json()["data"]["decision"] == "passed", portfolio.json()
            assert portfolio.json()["data"]["evaluation_hash"]
            release_read = client.get(f"/api/v1/manual-trading/releases/{release_id}", headers=headers)
            assert release_read.status_code == 200, release_read.text
            assert release_read.json()["data"]["status"] == "portfolio_passed", release_read.json()
            evidence = client.get(f"/api/v1/manual-trading/releases/{release_id}/evidence", headers=headers)
            assert evidence.status_code == 200, evidence.text
            body = evidence.json()["data"]
            assert {item["id"] for item in body["artifacts"]} >= {
                first_data["baseline_artifact_id"], first_data["stress_artifact_id"],
            }
            evaluation = body["evaluations"][-1]
            assert evaluation["previous_evaluation_id"] == research_data["id"]
            assert evaluation["previous_evaluation_hash"] == research_data["evaluation_hash"]
            assert evaluation["evaluation_hash"]
        after = {
            "releases": db.query(StrategyRelease).count(),
            "accounts": db.query(ManualAccount).count(),
            "paper_accounts": db.query(PaperAccount).count(),
            "paper_orders": db.query(PaperOrder).count(),
            "paper_fills": db.query(PaperFill).count(),
        }
        assert after["releases"] == before["releases"] + 1
        assert after["accounts"] == before["accounts"]
        assert after["paper_accounts"] == before["paper_accounts"]
        assert after["paper_orders"] == before["paper_orders"]
        assert after["paper_fills"] == before["paper_fills"]
    finally:
        app.dependency_overrides.pop(get_db, None)
        settings.operator_token, settings.result_dir = previous_token, previous_result_dir
        db.close()
        Base.metadata.drop_all(engine)
