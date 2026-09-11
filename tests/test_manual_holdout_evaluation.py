"""H2c economic evaluation registration and persistence boundaries."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import pytest

from server.models.database import Base
from server.models.schema import ManualHoldoutEvaluation, ResearchEvidenceArtifact, ResearchHoldoutAccess, ResearchHoldoutWindow
from server.services.manual_backup import build_manual_backup
from server.services.manual_holdout_evaluation import (
    ManualHoldoutEvaluationError,
    freeze_holdout_evaluation,
    run_holdout_evaluation,
    verify_completed_holdout,
    _hash,
)
from server.services.manual_holdout import ManualHoldoutError
from tests.manual_holdout_evaluation_test_support import build_holdout_case


def test_freeze_reads_only_metadata_and_reserves_full_normalized_scope(db_session, tmp_path, monkeypatch):
    case = build_holdout_case(db_session, tmp_path, monkeypatch)
    row = freeze_holdout_evaluation(db_session, **case["freeze_kwargs"])
    assert row.status == "pending"
    assert row.read_scope_start == "2027-01-04"
    assert row.read_scope_end == "2027-04-09"
    payload = json.loads(row.input_json)
    assert payload["technical_policy"]["technical_exit_tail_v1"] == 20
    assert len(payload["dataset"]["data_content_hash"]) == 64
    assert db_session.get(ResearchHoldoutWindow, case["binding"].window_id).status == "sealed"


def test_freeze_identity_and_request_key_are_immutable(db_session, tmp_path, monkeypatch):
    case = build_holdout_case(db_session, tmp_path, monkeypatch)
    first = freeze_holdout_evaluation(db_session, **case["freeze_kwargs"])
    assert freeze_holdout_evaluation(db_session, **case["freeze_kwargs"]).id == first.id
    changed = dict(case["freeze_kwargs"], benchmark_receipt_path="holdout-source/other.json")
    with pytest.raises(ManualHoldoutEvaluationError):
        freeze_holdout_evaluation(db_session, **changed)


def test_backup_v4_contains_holdout_evaluation_metadata(db_session, tmp_path, monkeypatch):
    case = build_holdout_case(db_session, tmp_path, monkeypatch)
    freeze_holdout_evaluation(db_session, **case["freeze_kwargs"])
    backup = build_manual_backup(db_session)
    assert backup["schema_version"] == "manual-backup-v4"
    assert backup["row_counts"][ManualHoldoutEvaluation.__tablename__] == 1


def test_engine_sees_committed_access_from_an_independent_session(tmp_path, monkeypatch):
    engine_db = create_engine(f"sqlite:///{tmp_path / 'independent.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine_db)
    db_session = sessionmaker(bind=engine_db)()
    case = build_holdout_case(db_session, tmp_path, monkeypatch)
    row = freeze_holdout_evaluation(db_session, **case["freeze_kwargs"])
    import server.services.manual_holdout_engine as engine
    original = engine.run_holdout_engine
    seen: dict[str, str] = {}

    def checked(*args, **kwargs):
        other = db_session.get_bind()
        from sqlalchemy.orm import sessionmaker
        session = sessionmaker(bind=other)()
        try:
            current = session.get(ManualHoldoutEvaluation, row.id)
            access = session.get(ResearchHoldoutAccess, current.access_id)
            window = session.get(ResearchHoldoutWindow, case["binding"].window_id)
            assert access is not None and window.status == "opened"
            seen["access"] = access.id
        finally:
            session.close()
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "run_holdout_engine", checked)
    try:
        completed = run_holdout_evaluation(db_session, row.id, actor="runner")
        assert completed.status == "completed" and seen["access"] == completed.access_id
    finally:
        db_session.close()
        engine_db.dispose()


def test_failed_attempt_keeps_open_scope_and_fixed_input_retries_once(db_session, tmp_path, monkeypatch):
    case = build_holdout_case(db_session, tmp_path, monkeypatch)
    row = freeze_holdout_evaluation(db_session, **case["freeze_kwargs"])
    import server.services.manual_holdout_engine as engine
    original = engine.run_holdout_engine
    calls = {"count": 0}

    def fail_once(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("technical failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "run_holdout_engine", fail_once)
    with pytest.raises(RuntimeError, match="technical failure"):
        run_holdout_evaluation(db_session, row.id, actor="runner")
    db_session.refresh(row)
    assert row.status == "failed"
    assert db_session.get(ResearchHoldoutWindow, case["binding"].window_id).status == "opened"
    first_access_count = db_session.query(ResearchHoldoutAccess).filter_by(window_id=case["binding"].window_id).count()
    completed = run_holdout_evaluation(db_session, row.id, actor="runner")
    assert completed.status == "completed" and completed.attempt_count == 2
    assert db_session.query(ResearchHoldoutAccess).filter_by(window_id=case["binding"].window_id).count() == first_access_count + 1


def test_completed_retry_does_not_start_engine_or_add_access(db_session, tmp_path, monkeypatch):
    case = build_holdout_case(db_session, tmp_path, monkeypatch)
    row = freeze_holdout_evaluation(db_session, **case["freeze_kwargs"])
    completed = run_holdout_evaluation(db_session, row.id, actor="runner")
    count = db_session.query(ResearchHoldoutAccess).filter_by(window_id=case["binding"].window_id).count()
    import server.services.manual_holdout_engine as engine
    monkeypatch.setattr(engine, "run_holdout_engine", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("engine rerun")))
    again = run_holdout_evaluation(db_session, row.id, actor="runner")
    assert again.id == completed.id and again.attempt_count == 1
    assert db_session.query(ResearchHoldoutAccess).filter_by(window_id=case["binding"].window_id).count() == count


def test_expired_token_cannot_mark_failed(db_session, tmp_path, monkeypatch):
    case = build_holdout_case(db_session, tmp_path, monkeypatch)
    row = freeze_holdout_evaluation(db_session, **case["freeze_kwargs"])
    row.status, row.lease_token = "running", "old-token"
    row.lease_until = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    db_session.commit()
    from server.services.manual_holdout_evaluation import _mark_failed
    _mark_failed(db_session, row.id, "old-token", RuntimeError("late"))
    db_session.refresh(row)
    assert row.status == "running" and row.lease_token == "old-token"


def test_expired_old_token_cannot_complete_after_new_token_takeover(db_session, tmp_path, monkeypatch):
    case = build_holdout_case(db_session, tmp_path, monkeypatch)
    row = freeze_holdout_evaluation(db_session, **case["freeze_kwargs"])
    import server.services.manual_holdout_engine as engine
    original = engine.run_holdout_engine

    def takeover(*args, **kwargs):
        row.lease_token = "new-token"
        row.lease_until = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        db_session.commit()
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "run_holdout_engine", takeover)
    with pytest.raises(ManualHoldoutEvaluationError, match="lease_lost"):
        run_holdout_evaluation(db_session, row.id, actor="runner")
    db_session.refresh(row)
    assert row.status == "running" and row.lease_token == "new-token"


def test_freeze_parquet_reads_date_columns_only_for_holdout_metadata(db_session, tmp_path, monkeypatch):
    case = build_holdout_case(db_session, tmp_path, monkeypatch)
    import server.services.manual_holdout_evaluation as service
    import pyarrow.parquet as parquet
    original = parquet.read_table
    observed: list[tuple[str, object]] = []

    def spy(path, *args, **kwargs):
        if "holdout-source" in str(path):
            observed.append((str(path), kwargs.get("columns")))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(service.pq, "read_table", spy)
    freeze_holdout_evaluation(db_session, **case["freeze_kwargs"])
    assert observed and all(columns == ["date"] for _, columns in observed)


def test_parent_invalidation_midrun_leaves_no_verified_artifact(db_session, tmp_path, monkeypatch):
    case = build_holdout_case(db_session, tmp_path, monkeypatch)
    row = freeze_holdout_evaluation(db_session, **case["freeze_kwargs"])
    import server.services.manual_holdout_engine as engine
    original = engine.run_holdout_engine

    def invalidate_after(*args, **kwargs):
        result = original(*args, **kwargs)
        case["parent_fixture"]["validation_artifact"].status = "invalidated"
        db_session.commit()
        return result

    monkeypatch.setattr(engine, "run_holdout_engine", invalidate_after)
    with pytest.raises(ManualHoldoutError, match="research_passed_evaluation_checks_not_passed"):
        run_holdout_evaluation(db_session, row.id, actor="runner")
    assert db_session.query(ResearchEvidenceArtifact).filter_by(producer_entity_id=row.id).count() == 0
    assert db_session.get(ResearchHoldoutWindow, case["binding"].window_id).status == "opened"


def test_tampered_external_anchor_and_identity_are_rejected(db_session, tmp_path, monkeypatch):
    case = build_holdout_case(db_session, tmp_path, monkeypatch)
    row = freeze_holdout_evaluation(db_session, **case["freeze_kwargs"])
    completed = run_holdout_evaluation(db_session, row.id, actor="runner")
    artifact = db_session.get(ResearchEvidenceArtifact, completed.result_artifact_id)
    original_identity = json.loads(artifact.identity_json)
    tampered_identity = dict(original_identity, derived_strategy_core_hash="0" * 64)
    artifact.identity_json = json.dumps(tampered_identity, sort_keys=True, separators=(",", ":"))
    artifact.identity_hash = _hash(tampered_identity)
    artifact.evidence_hash = _hash({"identity_hash": artifact.identity_hash, "manifest_hash": artifact.manifest_hash})
    db_session.commit()
    with pytest.raises(ManualHoldoutEvaluationError, match="holdout_result_identity_mismatch"):
        verify_completed_holdout(db_session, completed.result_artifact_id)
    artifact.identity_json = json.dumps(original_identity, sort_keys=True, separators=(",", ":"))
    artifact.identity_hash = _hash(original_identity)
    artifact.evidence_hash = _hash({"identity_hash": artifact.identity_hash, "manifest_hash": artifact.manifest_hash})
    db_session.commit()
    saved_manifest = json.loads(artifact.manifest_json)
    assert len(saved_manifest["top_level"]) == 4
    assert sum(len(item["files"]) for item in saved_manifest["scenarios"].values()) + 1 == 25
    saved_manifest["scenarios"]["baseline"]["files"][0]["sha256"] = "0" * 64
    artifact.manifest_json = json.dumps(saved_manifest, sort_keys=True, separators=(",", ":"))
    artifact.manifest_hash = _hash(saved_manifest)
    artifact.evidence_hash = _hash({"identity_hash": artifact.identity_hash, "manifest_hash": artifact.manifest_hash})
    db_session.commit()
    with pytest.raises(ManualHoldoutEvaluationError, match="holdout_result_manifest_anchor_mismatch"):
        verify_completed_holdout(db_session, completed.result_artifact_id)
