"""H2d release-bound admission tests over the real H2c fixture chain."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import Column, Integer, String, Text, MetaData, Table, inspect, select, create_engine, insert
from sqlalchemy.orm import sessionmaker

from server.models.database import Base
from server.models import database as database_module
from server.models.schema import (
    ManualPilotBinding,
    ManualPilotMarketReceipt,
    ManualProspectivePilot,
    ResearchEvidenceArtifact,
    StrategyPromotionEvaluation,
    StrategyRelease,
)
from server.services.manual_backup import build_manual_backup, restore_manual_backup, write_manual_backup
from server.services.manual_pilot import ManualPilotError
from server.services import manual_pilot_admission as admission
from server.services.manual_pilot_admission import start_paper_observation, verify_pilot_binding
from tests.manual_pilot_test_support import build_admitted_pilot


def test_admission_start_and_verify_freezes_real_chain(db_session, tmp_path, monkeypatch):
    case = build_admitted_pilot(db_session, tmp_path, monkeypatch)
    result = case["binding"]
    assert case["pilot"].status == "observing"
    assert result["status"] == "observing"
    assert len(result["expected_dates"]) == 30
    assert result["expected_dates"][0] > result["start_date"]
    assert result["capital"]
    assert result["universe"] == ["600000.SH"]
    assert result["close_capture_not_before"] == "16:30:00"
    assert result["verified"] is True
    assert db_session.get(StrategyRelease, case["release"].id).status == "paper_observing"
    backup = build_manual_backup(db_session)
    assert backup["schema_version"] == "manual-backup-v5"
    assert backup["row_counts"][ManualPilotBinding.__tablename__] == 1
    assert backup["row_counts"][ManualPilotMarketReceipt.__tablename__] == 0
    backup_path = tmp_path / "manual-v5.json"
    write_manual_backup(db_session, backup_path)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    restored = sessionmaker(bind=engine)()
    try:
        restore_manual_backup(restored, backup_path)
        verified = verify_pilot_binding(restored, case["pilot"].id)
        assert verified["binding_id"] == case["binding"]["binding_id"]
    finally:
        restored.close()
        engine.dispose()


def test_admission_idempotency_and_same_release_restart_are_closed(db_session, tmp_path, monkeypatch):
    case = build_admitted_pilot(db_session, tmp_path, monkeypatch)
    replay = start_paper_observation(
        db_session, release_id=case["release"].id, actor="pilot-fixture",
        idempotency_key="pilot-fixture-start", maximum_daily_loss="0.0200",
    )
    assert replay.id == case["pilot"].id
    with pytest.raises(ManualPilotError, match="already_bound"):
        start_paper_observation(
            db_session, release_id=case["release"].id, actor="pilot-fixture",
            idempotency_key="pilot-fixture-start-second", maximum_daily_loss="0.02",
        )


def test_verify_rejects_tampered_parent_and_paper_evaluation(db_session, tmp_path, monkeypatch):
    case = build_admitted_pilot(db_session, tmp_path, monkeypatch)
    binding_row = db_session.scalars(select(ManualPilotBinding).where(ManualPilotBinding.pilot_id == case["pilot"].id)).first()
    assert binding_row is not None
    binding_row.protocol_json = json.dumps({**json.loads(binding_row.protocol_json), "capital": "1"}, sort_keys=True)
    db_session.commit()
    with pytest.raises(ManualPilotError, match="protocol_hash_mismatch"):
        verify_pilot_binding(db_session, case["pilot"].id)


def test_verify_rejects_invalidated_holdout_parent_and_same_key_replays_across_day(db_session, tmp_path, monkeypatch):
    case = build_admitted_pilot(db_session, tmp_path, monkeypatch)
    replay_clock = case["started_at"].replace(day=13)
    monkeypatch.setattr(admission, "_utc_now", lambda: replay_clock)
    replay = start_paper_observation(
        db_session, release_id=case["release"].id, actor="pilot-fixture",
        idempotency_key="pilot-fixture-start", maximum_daily_loss="0.02",
    )
    assert replay.id == case["pilot"].id
    artifact_row = db_session.get(ResearchEvidenceArtifact, case["holdout_result"].result_artifact_id)
    artifact_row.status = "invalidated"
    db_session.commit()
    with pytest.raises(ManualPilotError, match="holdout_result_artifact_invalid|holdout_result_artifact"):
        verify_pilot_binding(db_session, case["pilot"].id)


def test_admission_failure_rolls_back_all_three_rows_and_temporal_future_gate(db_session, tmp_path, monkeypatch):
    case = build_admitted_pilot(db_session, tmp_path, monkeypatch, admit=False)
    original_execute = db_session.execute

    def fail_release_cas(statement, *args, **kwargs):
        if "UPDATE STRATEGY_RELEASE SET STATUS" in str(statement).upper():
            raise RuntimeError("injected-after-flush")
        return original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "execute", fail_release_cas)
    with pytest.raises(RuntimeError, match="injected-after-flush"):
        start_paper_observation(
            db_session, release_id=case["release"].id, actor="pilot-fixture",
            idempotency_key="pilot-fixture-start", maximum_daily_loss="0.02",
        )
    assert db_session.query(ManualProspectivePilot).count() == 0
    assert db_session.query(ManualPilotBinding).count() == 0
    assert db_session.query(StrategyPromotionEvaluation).filter_by(target_status="paper_observing").count() == 0
    independent = sessionmaker(bind=db_session.get_bind())()
    try:
        assert independent.query(ManualProspectivePilot).count() == 0
        assert independent.get(StrategyRelease, case["release"].id).status == "holdout_passed"
    finally:
        independent.close()


def test_admission_rejects_start_inside_holdout_read_scope(db_session, tmp_path, monkeypatch):
    case = build_admitted_pilot(db_session, tmp_path, monkeypatch, admit=False)
    monkeypatch.setattr(admission, "_utc_now", lambda: case["started_at"].replace(day=9))
    with pytest.raises(ManualPilotError, match="pilot_start_must_follow_holdout_scope"):
        start_paper_observation(
            db_session, release_id=case["release"].id, actor="pilot-fixture",
            idempotency_key="pilot-fixture-start", maximum_daily_loss="0.02",
        )
    assert db_session.query(ManualProspectivePilot).count() == 0


def test_old_file_init_creates_new_tables_and_preserves_legacy_pilot(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite'}")
    legacy_metadata = MetaData()
    legacy = Table(
        "manual_prospective_pilot", legacy_metadata,
        Column("id", String(36), primary_key=True), Column("pilot_key", String(180), nullable=False),
        Column("release_id", String(36), nullable=False), Column("strategy_fingerprint", String(64), nullable=False),
        Column("start_date", String(10), nullable=False), Column("target_days", Integer, nullable=False),
        Column("data_mode", String(30), nullable=False), Column("status", String(20), nullable=False),
        Column("observation_days", Integer, nullable=False), Column("valid_days", Integer, nullable=False),
        Column("required_evidence", Text, nullable=False), Column("report_json", Text), Column("report_hash", String(64)),
        Column("blocked_reason", Text), Column("created_at", String(40), nullable=False), Column("completed_at", String(40)),
    )
    legacy.create(engine)
    with engine.begin() as conn:
        conn.execute(insert(legacy).values(
            id="legacy-pilot", pilot_key="legacy-key", release_id="legacy-release", strategy_fingerprint="b" * 64,
            start_date="2027-01-01", target_days=30, data_mode="synthetic_engineering", status="planned",
            observation_days=0, valid_days=0, required_evidence="{}", created_at="2027-01-01T00:00:00+00:00",
        ))
    monkeypatch.setattr(database_module, "engine", engine)
    database_module.init_db()
    database_module.init_db()
    names = set(inspect(engine).get_table_names())
    assert {"manual_pilot_binding", "manual_pilot_market_receipt"} <= names
    restored = sessionmaker(bind=engine)()
    try:
        assert restored.get(ManualProspectivePilot, "legacy-pilot") is not None
    finally:
        restored.close()
        engine.dispose()
