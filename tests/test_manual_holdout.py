"""H2c release-bound manual holdout registration and access tests."""
from __future__ import annotations

import json
import multiprocessing

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from server.config import settings
from server.models.database import Base
from server.models.schema import ManualHoldoutBinding, ResearchHoldoutAccess, ResearchHoldoutWindow
from server.services.manual_backup import restore_manual_backup, write_manual_backup
from server.services.manual_evidence import advance_release_from_evidence
from server.services.manual_holdout import (
    ManualHoldoutError,
    create_manual_holdout,
    invalidate_manual_holdout,
    open_manual_holdout,
)
from server.services import manual_holdout as manual_holdout_service
from server.services.manual_portfolio_registration import register_manual_portfolio_pair
from server.services.research_holdout import HoldoutError, access_holdout, complete_holdout, create_holdout_window
from tests.manual_portfolio_test_support import build_portfolio_pair
from tests.test_manual_portfolio_promotion import _release


def _process_create_window(db_path, suffix, barrier, queue):
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 10})
    factory = sessionmaker(bind=engine)
    db = factory()
    try:
        barrier.wait()
        try:
            create_holdout_window(
                db, dataset_id=f"dataset-{suffix}", data_content_hash=(suffix * 64)[:64],
                start_date="2027-07-01", end_date="2027-07-10", policy_hash="f" * 64,
            )
            queue.put("ok")
        except HoldoutError as exc:
            queue.put(str(exc))
    finally:
        db.close()
        engine.dispose()


def _ready(db, tmp_path, monkeypatch):
    fixture = build_portfolio_pair(db, tmp_path)
    monkeypatch.setattr(settings, "result_dir", str(fixture["results_root"]))
    pair = register_manual_portfolio_pair(
        db, fixture["baseline_dir"], fixture["stress_dir"],
        allowed_root=fixture["results_root"], source_root=fixture["source_root"],
    )
    release = _release(db, fixture)
    refs = {
        "training_artifact_id": fixture["training_artifact"].id,
        "validation_artifact_id": fixture["validation_artifact"].id,
    }
    assert advance_release_from_evidence(
        db, release_id=release.id, target_status="research_passed", evidence_refs=refs,
        actor="holdout-test", idempotency_key="holdout-research",
    ).decision == "passed"
    portfolio_refs = {"baseline_artifact_id": pair["baseline_artifact_id"], "stress_artifact_id": pair["stress_artifact_id"]}
    assert advance_release_from_evidence(
        db, release_id=release.id, target_status="portfolio_passed", evidence_refs=portfolio_refs,
        actor="holdout-test", idempotency_key="holdout-portfolio",
    ).decision == "passed"
    return fixture, release


def test_manual_holdout_preregistration_open_and_compatibility_bypass_rejected(db_session, tmp_path, monkeypatch):
    _, release = _ready(db_session, tmp_path, monkeypatch)
    binding = create_manual_holdout(
        db_session, release_id=release.id, dataset_id="holdout-future",
        data_content_hash="a" * 64, start_date="2027-02-01", end_date="2027-02-03",
        actor="operator", idempotency_key="holdout-create-1",
    )
    assert json.loads(binding.protocol_json)["protocol_version"] == "manual-holdout-preregistration-v1"
    first = open_manual_holdout(db_session, binding.id, actor="operator", purpose="authorized evaluation")
    second = open_manual_holdout(db_session, binding.id, actor="executor", purpose="second authorized read")
    assert first.result_exposed is True and first.binding_hash == binding.binding_hash
    assert len(first.payload_hash) == len(second.payload_hash) == 64
    assert db_session.get(ResearchHoldoutWindow, binding.window_id).status == "opened"
    with pytest.raises(HoldoutError, match="managed_holdout"):
        access_holdout(db_session, binding.window_id, accessed_by="compat", purpose="bypass")
    with pytest.raises(HoldoutError, match="managed_holdout"):
        complete_holdout(db_session, binding.window_id)


def test_manual_holdout_idempotency_and_invalidation_are_terminal(db_session, tmp_path, monkeypatch):
    _, release = _ready(db_session, tmp_path, monkeypatch)
    kwargs = dict(
        release_id=release.id, dataset_id="holdout-future", data_content_hash="b" * 64,
        start_date="2027-03-01", end_date="2027-03-03", actor="operator", idempotency_key="holdout-create-2",
    )
    binding = create_manual_holdout(db_session, **kwargs)
    assert create_manual_holdout(db_session, **kwargs).id == binding.id
    with pytest.raises(ManualHoldoutError, match="idempotency_conflict"):
        create_manual_holdout(db_session, **{**kwargs, "dataset_id": "renamed"})
    window = invalidate_manual_holdout(db_session, binding.id, reason="upstream evidence withdrawn")
    assert window.invalidation_reason == "upstream evidence withdrawn"
    with pytest.raises(ManualHoldoutError, match="invalidated"):
        open_manual_holdout(db_session, binding.id, actor="operator", purpose="late read")
    assert invalidate_manual_holdout(db_session, binding.id, reason="different reason").invalidation_reason == "upstream evidence withdrawn"


def test_manual_holdout_backup_round_trip_keeps_parent_and_access_hashes(db_session, tmp_path, monkeypatch):
    _, release = _ready(db_session, tmp_path, monkeypatch)
    binding = create_manual_holdout(
        db_session, release_id=release.id, dataset_id="holdout-future", data_content_hash="c" * 64,
        start_date="2027-04-01", end_date="2027-04-03", actor="operator", idempotency_key="holdout-create-3",
    )
    access = open_manual_holdout(db_session, binding.id, actor="operator", purpose="authorized evaluation")
    backup_path = tmp_path / "manual-v3.json"
    write_manual_backup(db_session, backup_path)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    restored = sessionmaker(bind=engine)()
    try:
        restore_manual_backup(restored, backup_path)
        copied = restored.get(ManualHoldoutBinding, binding.id)
        assert copied.binding_hash == binding.binding_hash
        assert restored.get(ResearchHoldoutAccess, access.id).payload_hash == access.payload_hash
        reopened = open_manual_holdout(restored, binding.id, actor="restored", purpose="recheck")
        assert reopened.binding_hash == binding.binding_hash
    finally:
        restored.close()
        Base.metadata.drop_all(engine)


def test_open_uses_fresh_cas_after_another_session_invalidates_and_keeps_first_timestamp(db_session, tmp_path, monkeypatch):
    _, release = _ready(db_session, tmp_path, monkeypatch)
    binding = create_manual_holdout(
        db_session, release_id=release.id, dataset_id="holdout-future", data_content_hash="d" * 64,
        start_date="2027-05-01", end_date="2027-05-03", actor="operator", idempotency_key="holdout-create-4",
    )
    first = open_manual_holdout(db_session, binding.id, actor="operator", purpose="first")
    opened_at = db_session.get(ResearchHoldoutWindow, binding.window_id).opened_at
    other = sessionmaker(bind=db_session.get_bind())()
    try:
        invalidate_manual_holdout(other, binding.id, reason="concurrent stop")
        with pytest.raises(ManualHoldoutError, match="invalidated|changed"):
            open_manual_holdout(db_session, binding.id, actor="operator", purpose="stale cache")
    finally:
        other.close()
    assert first.result_exposed is True
    assert opened_at == db_session.get(ResearchHoldoutWindow, binding.window_id).opened_at


def test_binding_insert_failure_rolls_back_window_and_binding(db_session, tmp_path, monkeypatch):
    _, release = _ready(db_session, tmp_path, monkeypatch)
    original = manual_holdout_service.create_holdout_window

    def fail_after_window(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected binding failure")

    monkeypatch.setattr(manual_holdout_service, "create_holdout_window", fail_after_window)
    with pytest.raises(RuntimeError, match="injected"):
        create_manual_holdout(
            db_session, release_id=release.id, dataset_id="holdout-future", data_content_hash="e" * 64,
            start_date="2027-06-01", end_date="2027-06-03", actor="operator", idempotency_key="holdout-create-5",
        )
    assert db_session.query(ManualHoldoutBinding).count() == 0
    assert db_session.query(ResearchHoldoutWindow).count() == 0


def test_sqlite_registry_serializes_overlapping_window_creation(tmp_path):
    db_path = tmp_path / "holdout-lock.sqlite"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 10})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_process_create_window, args=(str(db_path), suffix, barrier, queue)) for suffix in ("a", "b")]
    for process in processes:
        process.start()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    results = [queue.get(timeout=3) for _ in processes]
    check = factory()
    try:
        assert results.count("ok") == 1
        assert any("already_registered" in result for result in results)
        assert check.query(ResearchHoldoutWindow).count() == 1
    finally:
        check.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_invalid_parent_artifact_blocks_open_but_allows_invalidation(db_session, tmp_path, monkeypatch):
    fixture, release = _ready(db_session, tmp_path, monkeypatch)
    binding = create_manual_holdout(
        db_session, release_id=release.id, dataset_id="holdout-future", data_content_hash="f" * 64,
        start_date="2027-08-01", end_date="2027-08-03", actor="operator", idempotency_key="holdout-create-6",
    )
    fixture["validation_artifact"].status = "invalidated"
    db_session.commit()
    with pytest.raises(ManualHoldoutError):
        open_manual_holdout(db_session, binding.id, actor="operator", purpose="must block")
    invalidated = invalidate_manual_holdout(db_session, binding.id, reason="parent artifact invalidated")
    assert invalidated.status == "invalidated"
