"""Database registration tests for the v2 manual portfolio pair."""
from __future__ import annotations

import json
import shutil

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from server.models.schema import FactorCandidate, ResearchEvidenceArtifact
from server.services.manual_evidence import ManualEvidenceError
from server.services.manual_portfolio_registration import (
    register_manual_portfolio_pair,
    reverify_registered_portfolio_pair,
)
from quant_engine.backtest.manual_portfolio_evidence import _hash
from tests.manual_portfolio_test_support import build_portfolio_pair


def _registered_pair_fixture(db_session, tmp_path, profitable: bool = True):
    fixture = build_portfolio_pair(db_session, tmp_path, profitable=profitable)
    fixture["pair"] = register_manual_portfolio_pair(
        db_session, fixture["baseline_dir"], fixture["stress_dir"],
        allowed_root=tmp_path / "results", source_root=tmp_path,
    )
    return fixture


def test_register_pair_idempotence_and_reverify(db_session, tmp_path):
    fixture = _registered_pair_fixture(db_session, tmp_path)
    assert db_session.query(ResearchEvidenceArtifact).filter(
        ResearchEvidenceArtifact.kind.in_(("manual_portfolio_baseline", "manual_portfolio_stress")),
    ).count() == 2
    assert register_manual_portfolio_pair(
        db_session, fixture["baseline_dir"], fixture["stress_dir"],
        allowed_root=tmp_path / "results", source_root=tmp_path,
    ) == fixture["pair"]
    result = reverify_registered_portfolio_pair(
        db_session, fixture["pair"]["baseline_artifact_id"], fixture["pair"]["stress_artifact_id"],
        allowed_root=tmp_path / "results", source_root=tmp_path,
    )
    assert result["pair_hash"] == fixture["pair"]["pair_hash"]


def test_registration_rejects_invalidated_factor(db_session, tmp_path):
    fixture = _registered_pair_fixture(db_session, tmp_path)
    fixture["validation_artifact"].status = "invalidated"
    db_session.commit()
    with pytest.raises(ManualEvidenceError, match="factor_artifact"):
        register_manual_portfolio_pair(
            db_session, fixture["baseline_dir"], fixture["stress_dir"],
            allowed_root=tmp_path / "results", source_root=tmp_path,
        )


def test_second_artifact_flush_rolls_back_pair_and_preserves_pending_work(db_session, tmp_path):
    fixture = build_portfolio_pair(db_session, tmp_path)
    pending = FactorCandidate(
        id="pending-registration-candidate", name="pending_registration_candidate",
        expression_hash="c" * 64, expression_spec="{}", hypothesis="pending",
        direction=1, role="rank", source="test", status="registered",
    )
    db_session.add(pending)

    def reject_stress(_mapper, _connection, target):
        if target.kind == "manual_portfolio_stress":
            raise IntegrityError("forced second artifact failure", {}, RuntimeError("forced"))

    event.listen(ResearchEvidenceArtifact, "before_insert", reject_stress)
    try:
        with pytest.raises(ManualEvidenceError, match="unique_conflict|registration_failed"):
            register_manual_portfolio_pair(
                db_session, fixture["baseline_dir"], fixture["stress_dir"],
                allowed_root=tmp_path / "results", source_root=tmp_path,
            )
    finally:
        event.remove(ResearchEvidenceArtifact, "before_insert", reject_stress)
    assert db_session.query(ResearchEvidenceArtifact).filter(
        ResearchEvidenceArtifact.kind.in_(("manual_portfolio_baseline", "manual_portfolio_stress")),
    ).count() == 0
    db_session.commit()
    assert db_session.get(FactorCandidate, pending.id) is not None


def test_existing_half_pair_is_rejected_without_repair(db_session, tmp_path):
    fixture = _registered_pair_fixture(db_session, tmp_path)
    stress = db_session.get(ResearchEvidenceArtifact, fixture["pair"]["stress_artifact_id"])
    db_session.delete(stress)
    db_session.commit()
    with pytest.raises(ManualEvidenceError, match="half_registered"):
        register_manual_portfolio_pair(
            db_session, fixture["baseline_dir"], fixture["stress_dir"],
            allowed_root=tmp_path / "results", source_root=tmp_path,
        )
    assert db_session.get(ResearchEvidenceArtifact, fixture["pair"]["baseline_artifact_id"]) is not None


def test_summary_tamper_is_rejected_by_audit(db_session, tmp_path):
    fixture = build_portfolio_pair(db_session, tmp_path)
    summary = fixture["baseline_dir"] / "summary.json"
    payload = summary.read_text(encoding="utf-8").replace('"promotion_eligible": false', '"promotion_eligible": true')
    summary.write_text(payload, encoding="utf-8")
    with pytest.raises(ManualEvidenceError, match="pair_audit_failed"):
        register_manual_portfolio_pair(
            db_session, fixture["baseline_dir"], fixture["stress_dir"],
            allowed_root=tmp_path / "results", source_root=tmp_path,
        )


def test_reverify_rejects_baseline_and_stress_from_different_pairs(db_session, tmp_path):
    first = _registered_pair_fixture(db_session, tmp_path)
    second_baseline = tmp_path / "results" / "second-baseline"
    second_stress = tmp_path / "results" / "second-stress"
    shutil.copytree(first["baseline_dir"], second_baseline)
    shutil.copytree(first["stress_dir"], second_stress)
    for directory, generated_at in (
        (second_baseline, "2099-01-01T00:00:00+00:00"),
        (second_stress, "2099-01-01T00:00:01+00:00"),
    ):
        path = directory / "manifest.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["generated_at"] = generated_at
        payload["manifest_hash"] = _hash({key: value for key, value in payload.items() if key != "manifest_hash"})
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    second = register_manual_portfolio_pair(
        db_session, second_baseline, second_stress,
        allowed_root=tmp_path / "results", source_root=tmp_path,
    )
    assert db_session.query(ResearchEvidenceArtifact).filter(
        ResearchEvidenceArtifact.kind.in_(("manual_portfolio_baseline", "manual_portfolio_stress")),
    ).count() == 4
    with pytest.raises(ManualEvidenceError, match="pair_mismatch"):
        reverify_registered_portfolio_pair(
            db_session, first["pair"]["baseline_artifact_id"], second["stress_artifact_id"],
            allowed_root=tmp_path / "results", source_root=tmp_path,
        )
    assert db_session.query(ResearchEvidenceArtifact).filter(
        ResearchEvidenceArtifact.kind.in_(("manual_portfolio_baseline", "manual_portfolio_stress")),
    ).count() == 4


def test_verified_baseline_can_be_reused_with_new_stress_attempt(db_session, tmp_path):
    first = _registered_pair_fixture(db_session, tmp_path)
    second_stress = tmp_path / "results" / "second-stress-only"
    shutil.copytree(first["stress_dir"], second_stress)
    path = second_stress / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["generated_at"] = "2099-01-02T00:00:01+00:00"
    payload["manifest_hash"] = _hash({key: value for key, value in payload.items() if key != "manifest_hash"})
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    second = register_manual_portfolio_pair(
        db_session, first["baseline_dir"], second_stress,
        allowed_root=tmp_path / "results", source_root=tmp_path,
    )
    assert second["pair_hash"] != first["pair"]["pair_hash"]
    assert db_session.query(ResearchEvidenceArtifact).filter(
        ResearchEvidenceArtifact.kind.in_(("manual_portfolio_baseline", "manual_portfolio_stress")),
    ).count() == 4


def test_rewritten_registered_directory_is_content_conflict(db_session, tmp_path):
    first = _registered_pair_fixture(db_session, tmp_path)
    path = first["baseline_dir"] / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["generated_at"] = "2099-01-03T00:00:01+00:00"
    payload["manifest_hash"] = _hash({key: value for key, value in payload.items() if key != "manifest_hash"})
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(ManualEvidenceError, match="existing_artifact_conflict"):
        register_manual_portfolio_pair(
            db_session, first["baseline_dir"], first["stress_dir"],
            allowed_root=tmp_path / "results", source_root=tmp_path,
        )
