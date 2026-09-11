"""M7 durable lease, recovery and idempotency tests."""
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import event, select

from server.models.schema import ManualDailyJob
from server.services.manual_scheduler import (
    block_job,
    complete_job,
    enqueue_daily_jobs,
    fail_job,
    heartbeat_job,
    lease_next_job,
    recover_expired_jobs,
)


class Calendar:
    def is_trading_day(self, value: date) -> bool:
        return value.weekday() < 5


def test_daily_jobs_are_trading_day_bound_and_idempotent(db_session):
    assert enqueue_daily_jobs(db_session, account_id="account-1", run_date="2024-01-06", calendar=Calendar()) == []
    jobs = enqueue_daily_jobs(db_session, account_id="account-1", run_date="2024-01-05", calendar=Calendar())
    repeated = enqueue_daily_jobs(db_session, account_id="account-1", run_date="2024-01-05", calendar=Calendar())
    assert len(jobs) == 5
    assert [job.id for job in repeated] == [job.id for job in jobs]


def test_enqueue_savepoint_failure_rolls_back_the_whole_batch(db_session):
    def reject_second(_mapper, _connection, target):
        if target.job_type == "close_plan":
            raise RuntimeError("injected second enqueue failure")

    event.listen(ManualDailyJob, "before_insert", reject_second)
    try:
        with pytest.raises(RuntimeError, match="injected second enqueue failure"):
            enqueue_daily_jobs(
                db_session,
                account_id="account-batch",
                run_date="2024-01-05",
                calendar=Calendar(),
                job_types=("valuation", "close_plan"),
            )
    finally:
        event.remove(ManualDailyJob, "before_insert", reject_second)
    db_session.rollback()
    assert db_session.scalars(select(ManualDailyJob)).all() == []


def test_expired_lease_can_be_recovered_without_duplicate_job(db_session):
    jobs = enqueue_daily_jobs(db_session, account_id="account-2", run_date="2024-01-05", calendar=Calendar(), job_types=("valuation",))
    first = lease_next_job(db_session, worker_id="worker-a", now=datetime(2024, 1, 5, 0, 0, tzinfo=UTC), lease_seconds=10)
    assert first is not None and first.id == jobs[0].id
    first_token = first.lease_token
    assert heartbeat_job(db_session, first.id, worker_id="worker-a", lease_token=first_token, now="2024-01-05T00:00:05+00:00", lease_seconds=20).status == "running"
    assert recover_expired_jobs(db_session, now="2024-01-05T00:00:30+00:00") == 1
    second = lease_next_job(db_session, worker_id="worker-b", now="2024-01-05T00:00:31+00:00", lease_seconds=30)
    assert second is not None and second.id == first.id and second.attempt_count == 2
    assert fail_job(db_session, second.id, worker_id="worker-b", lease_token=second.lease_token, error="temporary", retry=True, now="2024-01-05T00:00:32+00:00").status == "pending"
    third = lease_next_job(db_session, worker_id="worker-c", now="2024-01-05T00:01:00+00:00", lease_seconds=30)
    assert third is not None
    completed = complete_job(db_session, third.id, worker_id="worker-c", lease_token=third.lease_token, result={"ok": True}, now="2024-01-05T00:01:01+00:00")
    assert completed.status == "succeeded"
    assert completed.result_hash and len(completed.result_hash) == 64


def test_blocked_job_requires_reason_and_cannot_be_claimed_again(db_session):
    enqueue_daily_jobs(db_session, account_id="account-3", run_date="2024-01-05", calendar=Calendar(), job_types=("review",))
    job = lease_next_job(db_session, worker_id="worker-a", now="2024-01-05T00:00:00+00:00")
    assert job is not None
    blocked = block_job(db_session, job.id, worker_id="worker-a", lease_token=job.lease_token, reason="manual_valuation_required", now="2024-01-05T00:00:01+00:00")
    assert blocked.status == "blocked"
    assert lease_next_job(db_session, worker_id="worker-b", now="2024-01-05T00:03:00+00:00") is None
