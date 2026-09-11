"""Research queue/worker cannot read periods reserved by managed holdouts."""

from datetime import date
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from server.models.database import Base
from server.models.schema import FactorCandidate, FactorExperiment, ManualHoldoutBinding
from server.services.factor_research import execute_experiment, queue_experiment, retry_experiment, _research_panel
from server.services.research_holdout import create_holdout_window


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def _reserved(db):
    window = create_holdout_window(
        db, dataset_id="dataset-v1", data_content_hash="a" * 64,
        start_date="2027-02-01", end_date="2027-02-10", policy_hash="b" * 64,
    )
    binding = ManualHoldoutBinding(
        window_id=window.id, release_id="release", release_hash="c" * 64,
        strategy_core_hash="d" * 64, portfolio_evaluation_id="evaluation",
        portfolio_evaluation_hash="e" * 64, protocol_json="{}", protocol_hash="b" * 64,
        binding_hash="f" * 64, idempotency_key="binding-key", request_hash="1" * 64,
        created_by="operator",
    )
    db.add(binding)
    db.commit()
    return window


def _candidate(db):
    row = FactorCandidate(
        name="queued-factor", expression_hash="1" * 64, expression_spec=json.dumps({
            "name": "queued-factor", "hypothesis": "test", "direction": 1,
            "role": "score", "source": "test", "expression": {"op": "close"},
        }), hypothesis="test", direction=1, role="score", source="test", status="registered",
    )
    db.add(row)
    db.commit()
    return row


def test_queue_rejects_reserved_period_but_allows_non_overlapping_period(tmp_path):
    engine, factory = _db()
    with factory() as db:
        _reserved(db)
        candidate = _candidate(db)
        manifest_dir = tmp_path / "data" / "research" / "dataset"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "manifest.json").write_text(json.dumps({
            "dataset_id": "dataset-v1", "content_hash": "a" * 64,
            "start_date": "2027-01-01", "end_date": "2027-12-31",
        }), encoding="utf-8")
        with pytest.raises(ValueError, match="factor_period_reserved_for_manual_holdout"):
            queue_experiment(db, candidate_id=candidate.id, dataset_id="dataset-v1",
                             start=date(2027, 2, 5), end=date(2027, 2, 6), data_root=tmp_path / "data")
        with pytest.raises(ValueError, match="factor_period_reserved_for_manual_holdout"):
            queue_experiment(db, candidate_id=candidate.id, dataset_id="dataset-v1",
                             start=date(2027, 3, 1), end=date(2027, 3, 5), data_root=tmp_path / "data")
        run, created = queue_experiment(db, candidate_id=candidate.id, dataset_id="dataset-v1",
                                        start=date(2027, 1, 1), end=date(2027, 1, 5), data_root=tmp_path / "data")
        assert created is True and run.status == "queued"
    engine.dispose()


def test_legacy_running_row_is_failed_before_economic_read(monkeypatch, tmp_path):
    engine, factory = _db()
    with factory() as db:
        _reserved(db)
        candidate = _candidate(db)
        row = FactorExperiment(
            candidate_id=candidate.id, dataset_id="dataset-v1", data_content_hash="a" * 64,
            start_date="2027-02-03", end_date="2027-02-04", forward_horizon=5,
            stage="training", status="running", attempt=1, lease_owner="worker",
        )
        db.add(row)
        db.commit()
        manifest_dir = tmp_path / "data" / "research" / "dataset"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "manifest.json").write_text(json.dumps({
            "dataset_id": "dataset-v1", "content_hash": "a" * 64,
            "start_date": "2027-01-01", "end_date": "2027-12-31",
        }), encoding="utf-8")
        monkeypatch.setattr("server.services.factor_research._research_panel", lambda *args: (_ for _ in ()).throw(AssertionError("economic data must not be read")))
        with pytest.raises(ValueError, match="factor_period_reserved_for_manual_holdout"):
            execute_experiment(db, row.id, worker_id="worker", data_root=tmp_path / "data")
        refreshed = db.get(FactorExperiment, row.id)
        assert refreshed.status == "failed"
        assert refreshed.lease_owner is None
        assert refreshed.error_code == "HoldoutError"
    engine.dispose()


def test_retry_uses_manifest_history_prefix(tmp_path):
    engine, factory = _db()
    with factory() as db:
        _reserved(db)
        candidate = _candidate(db)
        manifest_dir = tmp_path / "data" / "research" / "dataset"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "manifest.json").write_text(json.dumps({
            "dataset_id": "dataset-v1", "content_hash": "a" * 64,
            "start_date": "2027-01-01", "end_date": "2027-12-31",
        }), encoding="utf-8")
        row = FactorExperiment(
            candidate_id=candidate.id, dataset_id="dataset-v1", data_content_hash="a" * 64,
            start_date="2027-03-01", end_date="2027-03-05", forward_horizon=5,
            stage="training", status="failed", error_code="old",
        )
        db.add(row)
        db.commit()
        with pytest.raises(ValueError, match="factor_period_reserved_for_manual_holdout"):
            retry_experiment(db, row.id, data_root=tmp_path / "data")
        assert db.get(FactorExperiment, row.id).status == "failed"
    engine.dispose()


def test_retry_without_manifest_fails_closed(monkeypatch):
    engine, factory = _db()
    with factory() as db:
        candidate = _candidate(db)
        row = FactorExperiment(
            candidate_id=candidate.id, dataset_id="missing", data_content_hash="a" * 64,
            start_date="2027-03-01", end_date="2027-03-05", forward_horizon=5,
            stage="training", status="failed", error_code="old",
        )
        db.add(row)
        db.commit()
        with pytest.raises(KeyError, match="registered frozen research dataset not found"):
            retry_experiment(db, row.id)
        assert db.get(FactorExperiment, row.id).status == "failed"
    engine.dispose()


def test_research_panel_excludes_future_sentinel_rows(tmp_path):
    import pandas as pd

    daily = pd.DataFrame([
        {"code": "000001.SZ", "date": date(2027, 1, 2), "open": 10.0, "close": 10.0,
         "adjusted_close": 10.0, "is_suspended": False, "is_st": False, "signal": 1.0},
        {"code": "000001.SZ", "date": date(2027, 2, 1), "open": 999.0, "close": 999.0,
         "adjusted_close": 999.0, "is_suspended": False, "is_st": False, "signal": 999.0},
    ])
    securities = pd.DataFrame([{"code": "000001.SZ", "ipo_date": "2000-01-01", "out_date": None}])
    root = tmp_path / "research"
    root.mkdir()
    daily_path, security_path = root / "daily.parquet", root / "securities.parquet"
    daily.to_parquet(daily_path, index=False)
    securities.to_parquet(security_path, index=False)
    manifest = {"files": {"daily": {"path": str(daily_path), "sha256": ""}, "securities": {"path": str(security_path), "sha256": ""}}}
    from server.services.factor_research import _sha256
    manifest["files"]["daily"]["sha256"] = _sha256(daily_path)
    manifest["files"]["securities"]["sha256"] = _sha256(security_path)
    panel, _ = _research_panel(root / "manifest.json", manifest, {"signal"}, date(2027, 1, 31))
    assert list(panel.index.get_level_values("date")) == [date(2027, 1, 2)]
    assert panel["signal"].iloc[0] == 1.0


def test_file_sqlite_idempotent_queue_releases_registry_lock(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'queue.sqlite'}", connect_args={"timeout": 1})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    data = tmp_path / "data" / "research" / "dataset"
    data.mkdir(parents=True)
    (data / "manifest.json").write_text(json.dumps({
        "dataset_id": "dataset-v1", "content_hash": "a" * 64,
        "start_date": "2027-01-01", "end_date": "2027-12-31",
    }), encoding="utf-8")
    with factory() as first:
        candidate = _candidate(first)
        candidate_id = candidate.id
        queued, _ = queue_experiment(first, candidate_id=candidate.id, dataset_id="dataset-v1",
                                      start=date(2027, 1, 1), end=date(2027, 1, 5), data_root=tmp_path / "data")
    with factory() as second:
        duplicate, created = queue_experiment(second, candidate_id=candidate_id, dataset_id="dataset-v1",
                                              start=date(2027, 1, 1), end=date(2027, 1, 5), data_root=tmp_path / "data")
        assert created is False and duplicate.id == queued.id
        another, created = queue_experiment(second, candidate_id=candidate_id, dataset_id="dataset-v1",
                                            start=date(2027, 1, 10), end=date(2027, 1, 15), data_root=tmp_path / "data")
        assert created is True and another.id != queued.id
    engine.dispose()
