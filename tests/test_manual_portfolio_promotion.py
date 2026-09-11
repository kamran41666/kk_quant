"""portfolio_passed resolver tests over real persisted pair evidence."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
from sqlalchemy.orm import Session

from quant_engine.backtest.manual_portfolio_artifacts import reconstruct_result
from quant_engine.backtest.manual_portfolio_evidence import (
    _ARROW_SCHEMAS,
    _hash,
    _json_entry,
    _parquet_entry,
    _table,
    calculate_portfolio_metrics,
    replay_portfolio_result,
)
from server.config import settings
from server.models.schema import FactorExperiment, ResearchEvidenceArtifact
from server.services import manual_portfolio_promotion
from server.services.manual_evidence import (
    _reverify_factor_artifact,
    advance_release_from_evidence,
)
from server.services.manual_portfolio_registration import register_manual_portfolio_pair
from server.services.strategy_promotion import create_strategy_release
from tests.manual_portfolio_test_support import build_portfolio_pair


def _rewrite_budget_consistently(fixture):
    """Rewrite only persisted cohort budgets, preserving an internally valid result."""
    directory = Path(fixture["baseline_dir"])
    manifest = json.loads((directory / "manifest.json").read_text())
    audit = json.loads((directory / "audit.json").read_text())
    signals = pq.read_table(directory / "signals.parquet")
    rows = signals.to_pylist()
    for row in rows:
        if row["cohort_budget"] is not None:
            row["cohort_budget"] = Decimal(str(row["cohort_budget"])) + 1
    pq.write_table(_table(rows, _ARROW_SCHEMAS["signals.parquet"]), directory / "signals.parquet", compression="zstd")
    tables = {name: pq.read_table(directory / name) for name in _ARROW_SCHEMAS}
    result = reconstruct_result(manifest["run_envelope"], manifest["input_manifest"], tables, audit)
    replay = replay_portfolio_result(result, fixture["sources"])
    metrics = calculate_portfolio_metrics(result)
    summary = json.loads((directory / "summary.json").read_text())
    summary.update({"result_hash": result.result_hash, "metrics": metrics, "quality_errors": result.quality_errors,
                    "accounting_replay_passed": replay["accounting_passed"],
                    "source_and_execution_passed": replay["source_and_execution_passed"]})
    (directory / "replay.json").write_text(json.dumps(replay, indent=2))
    (directory / "summary.json").write_text(json.dumps(summary, indent=2))
    manifest.update({"result_hash": result.result_hash, "metrics": metrics,
                     "replay_hash": replay["replay_hash"], "quality_errors": result.quality_errors})
    files = []
    for name, table in sorted(tables.items()):
        files.append(_parquet_entry(directory / name, name.removesuffix(".parquet"), table))
    files.extend(_json_entry(directory / name, name.removesuffix(".json")) for name in ("audit.json", "replay.json", "summary.json"))
    files.sort(key=lambda item: item["path"])
    # The manifest entry is self-referential only through its own hash, which
    # is intentionally excluded by the directory verifier's manifest hash.
    manifest["files"] = files
    manifest.pop("manifest_hash", None)
    manifest["manifest_hash"] = _hash(manifest)
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2))


def _release(db, fixture):
    bundle = fixture["bundle"]
    training = fixture["training_artifact"]
    validation = fixture["validation_artifact"]
    return create_strategy_release(
        db, strategy_key=bundle.strategy_key, version="portfolio-v1",
        bundle_hash=bundle.bundle_hash, strategy_fingerprint=bundle.strategy_core_hash,
        research_evidence={"training_artifact_id": training.id, "validation_artifact_id": validation.id},
        execution_policy=bundle.identity()["execution_policy"], risk_policy={"max_drawdown": "0.2"},
    )


def test_real_pair_promotes_and_replays_protocol(db_session, tmp_path, monkeypatch):
    fixture = build_portfolio_pair(db_session, tmp_path, profitable=True)
    monkeypatch.setattr(settings, "result_dir", str(fixture["results_root"]))
    pair = register_manual_portfolio_pair(
        db_session, fixture["baseline_dir"], fixture["stress_dir"],
        allowed_root=fixture["results_root"], source_root=fixture["source_root"],
    )
    release = _release(db_session, fixture)
    research = advance_release_from_evidence(
        db_session, release_id=release.id, target_status="research_passed",
        evidence_refs={"training_artifact_id": fixture["training_artifact"].id, "validation_artifact_id": fixture["validation_artifact"].id},
        actor="promotion-test", idempotency_key="portfolio-research",
    )
    assert research.decision == "passed"
    evaluation = advance_release_from_evidence(
        db_session, release_id=release.id, target_status="portfolio_passed",
        evidence_refs={"baseline_artifact_id": pair["baseline_artifact_id"], "stress_artifact_id": pair["stress_artifact_id"]},
        actor="promotion-test", idempotency_key="portfolio-success",
    )
    assert evaluation.decision == "passed"
    assert json.loads(evaluation.checks_json)["execution_protocol_reproduced"] is True
    assert evaluation.previous_evaluation_id == research.id
    assert evaluation.previous_evaluation_hash == research.evaluation_hash
    replayed = advance_release_from_evidence(
        db_session, release_id=release.id, target_status="portfolio_passed",
        evidence_refs={"baseline_artifact_id": pair["baseline_artifact_id"], "stress_artifact_id": pair["stress_artifact_id"]},
        actor="promotion-test", idempotency_key="portfolio-success",
    )
    assert replayed.id == evaluation.id


def test_non_profitable_real_pair_is_blocked(db_session, tmp_path, monkeypatch):
    fixture = build_portfolio_pair(db_session, tmp_path, profitable=False)
    monkeypatch.setattr(settings, "result_dir", str(fixture["results_root"]))
    pair = register_manual_portfolio_pair(
        db_session, fixture["baseline_dir"], fixture["stress_dir"],
        allowed_root=fixture["results_root"], source_root=fixture["source_root"],
    )
    release = _release(db_session, fixture)
    research = advance_release_from_evidence(
        db_session, release_id=release.id, target_status="research_passed",
        evidence_refs={"training_artifact_id": fixture["training_artifact"].id, "validation_artifact_id": fixture["validation_artifact"].id},
        actor="promotion-test", idempotency_key="blocked-research",
    )
    assert research.decision == "passed"
    evaluation = advance_release_from_evidence(
        db_session, release_id=release.id, target_status="portfolio_passed",
        evidence_refs={"baseline_artifact_id": pair["baseline_artifact_id"], "stress_artifact_id": pair["stress_artifact_id"]},
        actor="promotion-test", idempotency_key="portfolio-blocked-metrics",
    )
    assert evaluation.decision == "blocked"


def test_budget_rewrite_is_blocked_by_protocol_reproduction(db_session, tmp_path, monkeypatch):
    fixture = build_portfolio_pair(db_session, tmp_path, profitable=True)
    monkeypatch.setattr(settings, "result_dir", str(fixture["results_root"]))
    _rewrite_budget_consistently(fixture)
    pair = register_manual_portfolio_pair(
        db_session, fixture["baseline_dir"], fixture["stress_dir"],
        allowed_root=fixture["results_root"], source_root=fixture["source_root"],
    )
    release = _release(db_session, fixture)
    research = advance_release_from_evidence(
        db_session, release_id=release.id, target_status="research_passed",
        evidence_refs={"training_artifact_id": fixture["training_artifact"].id, "validation_artifact_id": fixture["validation_artifact"].id},
        actor="promotion-test", idempotency_key="budget-rewrite-research",
    )
    assert research.decision == "passed"
    evaluation = advance_release_from_evidence(
        db_session, release_id=release.id, target_status="portfolio_passed",
        evidence_refs={"baseline_artifact_id": pair["baseline_artifact_id"], "stress_artifact_id": pair["stress_artifact_id"]},
        actor="promotion-test", idempotency_key="budget-rewrite-portfolio",
    )
    assert evaluation.decision == "blocked"
    assert json.loads(evaluation.checks_json)["execution_protocol_reproduced"] is False


def test_tampered_research_chain_blocks_portfolio_gate(db_session, tmp_path, monkeypatch):
    fixture = build_portfolio_pair(db_session, tmp_path, profitable=True)
    monkeypatch.setattr(settings, "result_dir", str(fixture["results_root"]))
    pair = register_manual_portfolio_pair(
        db_session, fixture["baseline_dir"], fixture["stress_dir"],
        allowed_root=fixture["results_root"], source_root=fixture["source_root"],
    )
    release = _release(db_session, fixture)
    research = advance_release_from_evidence(
        db_session, release_id=release.id, target_status="research_passed",
        evidence_refs={"training_artifact_id": fixture["training_artifact"].id, "validation_artifact_id": fixture["validation_artifact"].id},
        actor="promotion-test", idempotency_key="tampered-research",
    )
    research.evaluation_hash = "0" * 64
    db_session.commit()
    evaluation = advance_release_from_evidence(
        db_session, release_id=release.id, target_status="portfolio_passed",
        evidence_refs={"baseline_artifact_id": pair["baseline_artifact_id"], "stress_artifact_id": pair["stress_artifact_id"]},
        actor="promotion-test", idempotency_key="tampered-research-portfolio",
    )
    assert evaluation.decision == "blocked"


def test_invalidated_upstream_artifact_blocks_portfolio_gate(db_session, tmp_path, monkeypatch):
    fixture = build_portfolio_pair(db_session, tmp_path, profitable=True)
    monkeypatch.setattr(settings, "result_dir", str(fixture["results_root"]))
    pair = register_manual_portfolio_pair(
        db_session, fixture["baseline_dir"], fixture["stress_dir"],
        allowed_root=fixture["results_root"], source_root=fixture["source_root"],
    )
    release = _release(db_session, fixture)
    research = advance_release_from_evidence(
        db_session, release_id=release.id, target_status="research_passed",
        evidence_refs={"training_artifact_id": fixture["training_artifact"].id, "validation_artifact_id": fixture["validation_artifact"].id},
        actor="promotion-test", idempotency_key="invalidated-research",
    )
    assert research.decision == "passed"
    fixture["validation_artifact"].status = "invalidated"
    db_session.commit()
    evaluation = advance_release_from_evidence(
        db_session, release_id=release.id, target_status="portfolio_passed",
        evidence_refs={"baseline_artifact_id": pair["baseline_artifact_id"], "stress_artifact_id": pair["stress_artifact_id"]},
        actor="promotion-test", idempotency_key="invalidated-portfolio",
    )
    assert evaluation.decision == "blocked"


def test_changed_factor_attempt_is_read_only_and_does_not_register_new_artifact(db_session, tmp_path, monkeypatch):
    fixture = build_portfolio_pair(db_session, tmp_path, profitable=True)
    artifact_count = db_session.query(ResearchEvidenceArtifact).count()
    experiment = db_session.get(FactorExperiment, fixture["training_artifact"].producer_entity_id)
    experiment.attempt += 1
    db_session.flush()

    def unexpected_commit():
        raise AssertionError("factor reverify must not commit when producer attempt changed")

    monkeypatch.setattr(db_session, "commit", unexpected_commit)
    assert _reverify_factor_artifact(db_session, fixture["training_artifact"]) is False
    assert db_session.query(ResearchEvidenceArtifact).count() == artifact_count
    db_session.rollback()


def test_artifact_invalidated_during_real_stress_reproduction_blocks(db_session, tmp_path, monkeypatch):
    fixture = build_portfolio_pair(db_session, tmp_path, profitable=True)
    monkeypatch.setattr(settings, "result_dir", str(fixture["results_root"]))
    pair = register_manual_portfolio_pair(
        db_session, fixture["baseline_dir"], fixture["stress_dir"],
        allowed_root=fixture["results_root"], source_root=fixture["source_root"],
    )
    release = _release(db_session, fixture)
    research = advance_release_from_evidence(
        db_session, release_id=release.id, target_status="research_passed",
        evidence_refs={"training_artifact_id": fixture["training_artifact"].id, "validation_artifact_id": fixture["validation_artifact"].id},
        actor="promotion-test", idempotency_key="mid-replay-research",
    )
    assert research.decision == "passed"
    original = manual_portfolio_promotion._reproduce_execution_protocol

    def reproduce_then_invalidate(artifact, source_root):
        result = original(artifact, source_root)
        if artifact.result.bundle.cost_scenario == "stress":
            with Session(bind=db_session.get_bind()) as other:
                row = other.get(ResearchEvidenceArtifact, fixture["validation_artifact"].id)
                row.status = "invalidated"
                other.commit()
        return result

    monkeypatch.setattr(manual_portfolio_promotion, "_reproduce_execution_protocol", reproduce_then_invalidate)
    evaluation = advance_release_from_evidence(
        db_session, release_id=release.id, target_status="portfolio_passed",
        evidence_refs={"baseline_artifact_id": pair["baseline_artifact_id"], "stress_artifact_id": pair["stress_artifact_id"]},
        actor="promotion-test", idempotency_key="mid-replay-portfolio",
    )
    assert evaluation.decision == "blocked"
    assert "portfolio_artifact_changed_during_resolution" in evaluation.checks_json
