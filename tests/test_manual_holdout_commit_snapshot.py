"""Commit-time fences for the complete holdout promotion snapshot."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from server.models.database import Base
from server.models.schema import (
    ManualHoldoutEvaluation,
    ResearchEvidenceArtifact,
    ResearchHoldoutWindow,
    StrategyPromotionEvaluation,
    StrategyRelease,
    manual_now_str,
    uuid4_str,
)
from server.services import manual_evidence
from server.services.manual_evidence import (
    ManualEvidenceError,
    ResolvedReleaseEvidence,
    advance_release_from_evidence,
    holdout_commit_snapshot,
)
from server.services.manual_holdout import create_manual_holdout
from tests.manual_holdout_evaluation_test_support import build_holdout_case


def _case(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    case = build_holdout_case(db, tmp_path, monkeypatch, profitable=True)
    return engine, factory, db, case


def _passed(snapshot):
    return ResolvedReleaseEvidence(
        "holdout_passed", {"fixture_gate": True}, {"holdout_artifact_id": "fixture"},
        "f" * 64, commit_snapshot=snapshot,
    )


def test_holdout_commit_rejects_binding_added_after_resolution(tmp_path, monkeypatch):
    engine, factory, db, case = _case(tmp_path, monkeypatch)
    def resolve_then_add(db_session, *, release_id, target_status, evidence_refs):
        snapshot = holdout_commit_snapshot(db_session, release_id)
        other = factory()
        try:
            create_manual_holdout(
                other, release_id=release_id, dataset_id="holdout-second",
                data_content_hash="b" * 64, start_date="2027-05-01", end_date="2027-05-10",
                actor="concurrent", idempotency_key="holdout-concurrent-binding",
            )
        finally:
            other.close()
        return _passed(snapshot)

    monkeypatch.setattr(manual_evidence, "resolve_release_evidence", resolve_then_add)
    try:
        with pytest.raises(ManualEvidenceError, match="holdout_commit_snapshot_changed"):
            advance_release_from_evidence(
                db, release_id=case["release"].id, target_status="holdout_passed",
                evidence_refs={"holdout_artifact_id": "fixture"}, actor="operator",
                idempotency_key="holdout-concurrent-promotion",
            )
        db.expire_all()
        assert db.get(StrategyRelease, case["release"].id).status == "portfolio_passed"
        assert db.scalars(select(StrategyPromotionEvaluation).where(
            StrategyPromotionEvaluation.release_id == case["release"].id,
            StrategyPromotionEvaluation.target_status == "holdout_passed",
            StrategyPromotionEvaluation.decision == "passed",
        )).first() is None
    finally:
        db.close()
        Base.metadata.drop_all(engine)


def test_holdout_commit_rejects_artifact_invalidated_after_resolution(tmp_path, monkeypatch):
    engine, factory, db, case = _case(tmp_path, monkeypatch)
    binding = case["binding"]
    window = db.get(ResearchHoldoutWindow, binding.window_id)
    window.status = "completed"
    window.completed_at = manual_now_str()
    evaluation_id = uuid4_str()
    artifact_id = uuid4_str()
    evaluation = ManualHoldoutEvaluation(
        id=evaluation_id, binding_id=binding.id, input_json="{}", input_hash="a" * 64,
        request_key="holdout-fake-evaluation", created_by="fixture", status="completed",
        result_artifact_id=artifact_id, completed_at=manual_now_str(), attempt_count=1,
    )
    artifact = ResearchEvidenceArtifact(
        id=artifact_id, kind="holdout_result", producer_protocol="fixture",
        producer_entity_type="manual_holdout_evaluation", producer_entity_id=evaluation_id,
        producer_attempt=1, status="verified", identity_json="{}", identity_hash="1" * 64,
        manifest_json="{}", manifest_hash="2" * 64, evidence_hash="3" * 64,
    )
    db.add_all([evaluation, artifact])
    db.commit()
    def resolve_then_invalidate(db_session, *, release_id, target_status, evidence_refs):
        snapshot = holdout_commit_snapshot(db_session, release_id)
        other = factory()
        try:
            row = other.get(ResearchEvidenceArtifact, artifact_id)
            row.status = "invalidated"
            other.commit()
        finally:
            other.close()
        return _passed(snapshot)

    monkeypatch.setattr(manual_evidence, "resolve_release_evidence", resolve_then_invalidate)
    try:
        with pytest.raises(ManualEvidenceError, match="holdout_commit_snapshot_changed"):
            advance_release_from_evidence(
                db, release_id=case["release"].id, target_status="holdout_passed",
                evidence_refs={"holdout_artifact_id": artifact_id}, actor="operator",
                idempotency_key="holdout-invalidated-promotion",
            )
        db.expire_all()
        assert db.get(StrategyRelease, case["release"].id).status == "portfolio_passed"
        assert db.scalars(select(StrategyPromotionEvaluation).where(
            StrategyPromotionEvaluation.release_id == case["release"].id,
            StrategyPromotionEvaluation.target_status == "holdout_passed",
            StrategyPromotionEvaluation.decision == "passed",
        )).first() is None
    finally:
        db.close()
        Base.metadata.drop_all(engine)


def test_holdout_commit_requires_snapshot_from_resolver(tmp_path, monkeypatch):
    engine, factory, db, case = _case(tmp_path, monkeypatch)

    def resolve_without_snapshot(db_session, *, release_id, target_status, evidence_refs):
        return ResolvedReleaseEvidence(
            "holdout_passed", {"fixture_gate": True}, dict(evidence_refs), "e" * 64,
        )

    monkeypatch.setattr(manual_evidence, "resolve_release_evidence", resolve_without_snapshot)
    try:
        with pytest.raises(ManualEvidenceError, match="holdout_commit_snapshot_required"):
            advance_release_from_evidence(
                db, release_id=case["release"].id, target_status="holdout_passed",
                evidence_refs={"holdout_artifact_id": "fixture"}, actor="operator",
                idempotency_key="holdout-missing-snapshot",
            )
        db.expire_all()
        assert db.get(StrategyRelease, case["release"].id).status == "portfolio_passed"
        assert db.scalars(select(StrategyPromotionEvaluation).where(
            StrategyPromotionEvaluation.release_id == case["release"].id,
            StrategyPromotionEvaluation.target_status == "holdout_passed",
        )).first() is None
    finally:
        db.close()
        Base.metadata.drop_all(engine)
