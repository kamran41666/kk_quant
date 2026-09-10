"""H2 database-backed research artifact and promotion resolution tests."""
from __future__ import annotations

from datetime import date
import json

import pandas as pd
import pytest

from quant_engine.factor.expression import FactorExpressionSpec
from server.models.schema import FactorCandidate, FactorExperiment, StrategyPromotionEvaluation
from server.services.manual_evidence import (
    ManualEvidenceError,
    advance_release_from_evidence,
    register_factor_experiment_artifact,
)
from server.services.strategy_promotion import create_strategy_release


LABEL = {
    "label_id": "manual-daily-label-v1", "signal_phase": "close",
    "entry_offset": 1, "entry_phase": "open", "exit_offset": 2,
    "exit_phase": "close", "adjusted_prices": True,
}


def _candidate(db):
    spec = FactorExpressionSpec.from_dict({
        "name": "manual_evidence_factor", "expression": {"field": "close"},
        "direction": 1, "role": "rank", "hypothesis": "evidence test",
    })
    stored_spec = spec.as_dict()
    for derived in ("expression_hash", "required_fields", "lookback"):
        stored_spec.pop(derived)
    row = FactorCandidate(
        id="candidate-evidence", name=spec.name, expression_hash=spec.expression_hash,
        expression_spec=json.dumps(stored_spec, sort_keys=True), hypothesis=spec.hypothesis,
        direction=spec.direction, role=spec.role, source="test", status="validation_passed",
    )
    db.add(row)
    db.commit()
    return row


def _experiment(db, tmp_path, candidate, *, stage, start, end, decision):
    experiment_id = f"{stage}-experiment"
    directory = tmp_path / experiment_id
    directory.mkdir()
    report = {
        "protocol_version": "factor-experiment-v2", "candidate_id": candidate.id,
        "expression_hash": candidate.expression_hash, "dataset_id": "manual-dataset-v1",
        "data_content_hash": "d" * 64, "start_date": start, "end_date": end,
        "forward_horizon": 1, "label_spec": LABEL, "stage": stage,
        "evaluation_policy": {}, "decision": decision,
    }
    for name in ("factor_values.parquet", "ic_series.parquet", "quantile_returns.parquet"):
        pd.DataFrame({"date": [date.fromisoformat(start)], "value": [1.0]}).to_parquet(directory / name, index=False)
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    row = FactorExperiment(
        id=experiment_id, candidate_id=candidate.id, dataset_id="manual-dataset-v1",
        data_content_hash="d" * 64, start_date=start, end_date=end,
        forward_horizon=1, stage=stage, evaluation_policy="{}",
        label_spec=json.dumps(LABEL, sort_keys=True), status="completed", attempt=1,
        result_json=json.dumps(report, sort_keys=True), artifact_dir=str(directory),
    )
    db.add(row)
    db.commit()
    return row


def _release(db, training_artifact, validation_artifact, suffix="1"):
    refs = {
        "training_artifact_id": training_artifact.id,
        "validation_artifact_id": validation_artifact.id,
    }
    return create_strategy_release(
        db, strategy_key=f"manual-evidence-{suffix}", version="v1",
        bundle_hash=suffix * 64, strategy_fingerprint=("a" if suffix != "a" else "b") * 64,
        research_evidence=refs, execution_policy={"auto_submit": False},
        risk_policy={"max_drawdown": "0.2"},
    )


def test_worker_artifacts_are_rehashed_and_research_promotion_is_append_only(db_session, tmp_path):
    candidate = _candidate(db_session)
    training = _experiment(
        db_session, tmp_path, candidate, stage="training", start="2027-01-01",
        end="2027-03-31", decision="training_passed",
    )
    validation = _experiment(
        db_session, tmp_path, candidate, stage="validation", start="2027-04-01",
        end="2027-06-30", decision="validation_passed",
    )
    training_artifact = register_factor_experiment_artifact(db_session, training.id, allowed_root=tmp_path)
    validation_artifact = register_factor_experiment_artifact(db_session, validation.id, allowed_root=tmp_path)
    release = _release(db_session, training_artifact, validation_artifact)
    evaluation = advance_release_from_evidence(
        db_session, release_id=release.id, target_status="research_passed",
        evidence_refs=json.loads(release.research_evidence), actor="test-operator",
        idempotency_key="promotion-research-001",
    )
    assert evaluation.decision == "passed"
    assert db_session.get(type(release), release.id).status == "research_passed"
    assert db_session.query(StrategyPromotionEvaluation).count() == 1
    assert advance_release_from_evidence(
        db_session, release_id=release.id, target_status="research_passed",
        evidence_refs=json.loads(release.research_evidence), actor="test-operator",
        idempotency_key="promotion-research-001",
    ).id == evaluation.id


def test_tampered_artifact_and_unsupported_portfolio_gate_remain_blocked(db_session, tmp_path):
    candidate = _candidate(db_session)
    training = _experiment(db_session, tmp_path, candidate, stage="training", start="2027-01-01", end="2027-02-01", decision="training_passed")
    validation = _experiment(db_session, tmp_path, candidate, stage="validation", start="2027-03-01", end="2027-04-01", decision="validation_passed")
    training_artifact = register_factor_experiment_artifact(db_session, training.id, allowed_root=tmp_path)
    validation_artifact = register_factor_experiment_artifact(db_session, validation.id, allowed_root=tmp_path)
    release = _release(db_session, training_artifact, validation_artifact, suffix="2")
    (tmp_path / training.id / "factor_values.parquet").write_bytes(b"tampered")
    evaluation = advance_release_from_evidence(
        db_session, release_id=release.id, target_status="research_passed",
        evidence_refs=json.loads(release.research_evidence), actor="test-operator",
        idempotency_key="promotion-research-tampered",
    )
    assert evaluation.decision == "blocked"
    assert db_session.get(type(release), release.id).status == "draft"

    clean_release = _release(db_session, training_artifact, validation_artifact, suffix="3")
    clean_release.status = "research_passed"
    db_session.commit()
    unsupported = advance_release_from_evidence(
        db_session, release_id=clean_release.id, target_status="portfolio_passed",
        evidence_refs={}, actor="test-operator", idempotency_key="promotion-portfolio-unsupported",
    )
    assert unsupported.decision == "blocked"
    assert db_session.get(type(clean_release), clean_release.id).status == "research_passed"


def test_promotion_rejects_copied_booleans_and_idempotency_conflicts(db_session, tmp_path):
    candidate = _candidate(db_session)
    training = _experiment(db_session, tmp_path, candidate, stage="training", start="2027-01-01", end="2027-02-01", decision="training_passed")
    validation = _experiment(db_session, tmp_path, candidate, stage="validation", start="2027-03-01", end="2027-04-01", decision="validation_passed")
    ta = register_factor_experiment_artifact(db_session, training.id, allowed_root=tmp_path)
    va = register_factor_experiment_artifact(db_session, validation.id, allowed_root=tmp_path)
    release = _release(db_session, ta, va, suffix="4")
    copied = advance_release_from_evidence(
        db_session, release_id=release.id, target_status="research_passed",
        evidence_refs={"training_passed": "true", "validation_passed": "true"},
        actor="test-operator", idempotency_key="promotion-bool-copy",
    )
    assert copied.decision == "blocked"
    assert "artifact_ids" in copied.checks_json
    passed = advance_release_from_evidence(
        db_session, release_id=release.id, target_status="research_passed",
        evidence_refs=json.loads(release.research_evidence), actor="test-operator",
        idempotency_key="promotion-idempotent",
    )
    assert passed.decision == "passed"
    with pytest.raises(ManualEvidenceError, match="idempotency_conflict"):
        advance_release_from_evidence(
            db_session, release_id=release.id, target_status="research_passed",
            evidence_refs={"training_artifact_id": ta.id, "validation_artifact_id": "different"},
            actor="test-operator", idempotency_key="promotion-idempotent",
        )
