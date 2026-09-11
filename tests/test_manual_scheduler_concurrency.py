"""Process and independent-connection checks for manual scheduler fencing."""
from __future__ import annotations

import multiprocessing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from scripts.run_manual_daily_worker import run_worker_cycle
from server.models.database import Base
from server.models.schema import ManualDailyJob
from server.services.manual_scheduler import (
    ManualSchedulerError,
    block_job,
    complete_job,
    enqueue_daily_jobs,
    fail_job,
    heartbeat_job,
    lease_next_job,
    recover_expired_jobs,
)


class Calendar:
    def is_trading_day(self, value):
        return value.weekday() < 5


def _session_factory(path: str):
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    return engine, sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _lease_child(path: str, worker_id: str, barrier, queue) -> None:
    engine, factory = _session_factory(path)
    try:
        with factory() as db:
            barrier.wait(timeout=10)
            job = lease_next_job(
                db,
                worker_id=worker_id,
                now="2024-01-05T00:00:00+00:00",
                lease_seconds=30,
            )
            queue.put(None if job is None else (job.id, job.lease_owner, job.lease_token, job.attempt_count))
    finally:
        engine.dispose()


def _recover_child(path: str, barrier, queue) -> None:
    engine, factory = _session_factory(path)
    try:
        with factory() as db:
            barrier.wait(timeout=10)
            queue.put(("recover", recover_expired_jobs(db, now="2024-01-05T00:00:11+00:00")))
    finally:
        engine.dispose()


def _claim_after_expiry_child(path: str, barrier, queue) -> None:
    engine, factory = _session_factory(path)
    try:
        with factory() as db:
            barrier.wait(timeout=10)
            job = lease_next_job(
                db,
                worker_id="new-worker",
                now="2024-01-05T00:00:11+00:00",
                lease_seconds=30,
            )
            queue.put(None if job is None else ("claim", job.lease_owner, job.lease_token, job.attempt_count))
    finally:
        engine.dispose()


def _run_pair(path: Path, target, *args):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    queue = context.Queue()
    processes = [context.Process(target=target, args=(str(path), *args, barrier, queue)) for _ in range(2)]
    for process in processes:
        process.start()
    values = [queue.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    return values


def test_two_processes_cas_claim_exactly_one_job(tmp_path):
    path = tmp_path / "scheduler.sqlite"
    engine, factory = _session_factory(str(path))
    Base.metadata.create_all(bind=engine)
    with factory() as db:
        enqueue_daily_jobs(db, account_id="account", run_date="2024-01-05", calendar=Calendar(), job_types=("valuation",))
    engine.dispose()

    values = _run_pair(path, _lease_child, "worker")
    claimed = [value for value in values if value is not None]
    assert len(claimed) == 1
    assert claimed[0][1] == "worker"
    assert len(claimed[0][2]) == 32
    assert claimed[0][3] == 1


def test_expired_same_worker_token_is_fenced_and_new_attempt_wins(tmp_path):
    path = tmp_path / "scheduler.sqlite"
    engine, factory = _session_factory(str(path))
    Base.metadata.create_all(bind=engine)
    with factory() as db:
        jobs = enqueue_daily_jobs(db, account_id="account", run_date="2024-01-05", calendar=Calendar(), job_types=("valuation",))
        old = lease_next_job(db, worker_id="same-worker", now="2024-01-05T00:00:00+00:00", lease_seconds=10)
        assert old is not None
        old_token = old.lease_token
        new = lease_next_job(db, worker_id="same-worker", now="2024-01-05T00:00:11+00:00", lease_seconds=30)
        assert new is not None and old_token != new.lease_token
        for action in (
            lambda: heartbeat_job(db, old.id, worker_id="same-worker", lease_token=old_token, now="2024-01-05T00:00:12+00:00"),
            lambda: complete_job(db, old.id, worker_id="same-worker", lease_token=old_token, now="2024-01-05T00:00:12+00:00"),
            lambda: fail_job(db, old.id, worker_id="same-worker", lease_token=old_token, error="old", now="2024-01-05T00:00:12+00:00"),
            lambda: block_job(db, old.id, worker_id="same-worker", lease_token=old_token, reason="old", now="2024-01-05T00:00:12+00:00"),
        ):
            with pytest.raises(ManualSchedulerError, match="lease_lost"):
                action()
        db.expire_all()
        row = db.get(ManualDailyJob, jobs[0].id)
        assert row is not None
        assert (row.lease_owner, row.lease_token, row.attempt_count) == ("same-worker", new.lease_token, 2)
    engine.dispose()


def test_recovery_and_new_claim_race_does_not_overwrite_new_lease(tmp_path):
    path = tmp_path / "scheduler.sqlite"
    engine, factory = _session_factory(str(path))
    Base.metadata.create_all(bind=engine)
    with factory() as db:
        enqueue_daily_jobs(db, account_id="account", run_date="2024-01-05", calendar=Calendar(), job_types=("valuation",))
        first = lease_next_job(db, worker_id="old-worker", now="2024-01-05T00:00:00+00:00", lease_seconds=10)
        assert first is not None
    engine.dispose()

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    queue = context.Queue()
    processes = [
        context.Process(target=_recover_child, args=(str(path), barrier, queue)),
        context.Process(target=_claim_after_expiry_child, args=(str(path), barrier, queue)),
    ]
    for process in processes:
        process.start()
    values = [queue.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert any(value[0] == "claim" for value in values)

    engine, factory = _session_factory(str(path))
    with factory() as db:
        row = db.scalars(select(ManualDailyJob)).one()
        assert (row.status, row.lease_owner, row.attempt_count) == ("leased", "new-worker", 2)
    engine.dispose()


def test_worker_callback_paths_rollback_staged_failure_and_surface_lease_loss(tmp_path):
    path = tmp_path / "scheduler.sqlite"
    engine, factory = _session_factory(str(path))
    Base.metadata.create_all(bind=engine)
    with factory() as db:
        jobs = enqueue_daily_jobs(db, account_id="account", run_date="2024-01-05", calendar=Calendar(), job_types=("valuation",))
        result = run_worker_cycle(db, worker_id="worker", handlers={"valuation": lambda _db, _job: {"ok": True}})
        assert result["status"] == "succeeded"
        row = db.get(ManualDailyJob, jobs[0].id)
        assert row is not None and row.attempt_count == 1
    engine.dispose()

    path = tmp_path / "worker_fail.sqlite"
    engine, factory = _session_factory(str(path))
    Base.metadata.create_all(bind=engine)
    with factory() as db:
        jobs = enqueue_daily_jobs(db, account_id="account-2", run_date="2024-01-05", calendar=Calendar(), job_types=("valuation",))

        def raising_handler(handler_db, _job):
            handler_db.add(ManualDailyJob(job_key="staged-only", account_id="account-2", run_date="2024-01-05", job_type="valuation"))
            raise RuntimeError("boom")

        result = run_worker_cycle(db, worker_id="worker", handlers={"valuation": raising_handler})
        assert result["status"] == "pending"
        assert db.scalar(select(func.count()).select_from(ManualDailyJob).where(ManualDailyJob.job_key == "staged-only")) == 0
        row = db.get(ManualDailyJob, jobs[0].id)
        assert row is not None and row.attempt_count == 1
    engine.dispose()

    path = tmp_path / "worker_loss.sqlite"
    engine, factory = _session_factory(str(path))
    Base.metadata.create_all(bind=engine)
    with factory() as db:
        jobs = enqueue_daily_jobs(db, account_id="account-3", run_date="2024-01-05", calendar=Calendar(), job_types=("valuation",))

        def steal_handler(_db, _job):
            other_engine, other_factory = _session_factory(str(path))
            try:
                with other_factory() as other_db:
                    claimed = lease_next_job(
                        other_db,
                        worker_id="new-worker",
                        now=datetime.now(UTC) + timedelta(seconds=121),
                        lease_seconds=30,
                    )
                    assert claimed is not None
            finally:
                other_engine.dispose()
            return {"ok": True}

        result = run_worker_cycle(db, worker_id="worker", handlers={"valuation": steal_handler})
        assert result["status"] == "lease_lost"
        db.expire_all()
        row = db.get(ManualDailyJob, jobs[0].id)
        assert row is not None and row.lease_owner == "new-worker" and row.attempt_count == 2
    engine.dispose()


def test_database_failures_exit_once_and_recovery_does_not_claim_success(tmp_path, monkeypatch):
    missing_path = tmp_path / "missing.sqlite"
    engine, factory = _session_factory(str(missing_path))
    with factory() as db:
        with pytest.raises(OperationalError, match="no such table"):
            lease_next_job(db, worker_id="worker", now="2024-01-05T00:00:00+00:00")
        db.rollback()
        with pytest.raises(ManualSchedulerError, match="scheduler_database_unavailable"):
            recover_expired_jobs(db, now="2024-01-05T00:00:00+00:00")
    engine.dispose()

    path = tmp_path / "injected.sqlite"
    engine, factory = _session_factory(str(path))
    Base.metadata.create_all(bind=engine)
    with factory() as db:
        calls = 0

        def fail_once(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise OperationalError("injected non-lock failure", {}, RuntimeError("boom"))

        monkeypatch.setattr(db, "execute", fail_once)
        with pytest.raises(ManualSchedulerError, match="scheduler_database_unavailable"):
            recover_expired_jobs(db, now="2024-01-05T00:00:00+00:00")
        assert calls == 1
    engine.dispose()
