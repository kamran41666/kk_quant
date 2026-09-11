"""Run one bounded manual daily worker cycle.

Usage is deliberately cross-platform: ``python scripts/run_manual_daily_worker.py
--once``.  The worker does not create a broker client and does not synthesize
user fills, quotes, valuations, or orders.
"""
from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from quant_engine.data.calendar import TradingCalendar
from server.models.database import SessionLocal, init_db
from server.services.manual_scheduler import (
    ManualSchedulerError,
    block_job,
    complete_job,
    fail_job,
    heartbeat_job,
    lease_next_job,
    recover_expired_jobs,
)

logger = logging.getLogger("manual-daily-worker")
JobHandler = Callable[[Any, Any], Any]


def run_worker_cycle(
    db: Any,
    *,
    worker_id: str,
    handlers: dict[str, JobHandler] | None = None,
) -> dict[str, Any]:
    """Lease and process at most one job, returning a serializable summary."""
    recovered = recover_expired_jobs(db)
    job = lease_next_job(db, worker_id=worker_id)
    if job is None:
        return {"status": "idle", "recovered": recovered}
    lease_token = job.lease_token
    try:
        # Move leased -> running before invoking user code. The token captured
        # from this claim fences every transition made by this cycle.
        job = heartbeat_job(db, job.id, worker_id=worker_id, lease_token=lease_token)
    except ManualSchedulerError as exc:
        db.rollback()
        if "lease_lost" in str(exc):
            return {"status": "lease_lost", "job_id": job.id, "job_type": job.job_type, "recovered": recovered}
        raise
    handler = (handlers or {}).get(job.job_type)
    if handler is None:
        try:
            row = block_job(
                db, job.id, worker_id=worker_id, lease_token=lease_token,
                reason="manual_job_requires_explicit_user_input_or_reviewed_handler",
            )
        except ManualSchedulerError as exc:
            db.rollback()
            if "lease_lost" in str(exc):
                return {"status": "lease_lost", "job_id": job.id, "job_type": job.job_type, "recovered": recovered}
            raise
        return {"status": row.status, "job_id": row.id, "job_type": row.job_type, "recovered": recovered}
    try:
        result = handler(db, job)
        row = complete_job(db, job.id, worker_id=worker_id, lease_token=lease_token, result=result)
        return {"status": row.status, "job_id": row.id, "job_type": row.job_type, "result_hash": row.result_hash, "recovered": recovered}
    except Exception as exc:  # noqa: BLE001 - handler failures must be fenced and persisted
        # A handler may have staged business rows before raising. Roll those
        # back before the fenced failure transition so fail() cannot commit a
        # half-finished handler transaction along with the job state.
        db.rollback()
        try:
            row = fail_job(
                db, job.id, worker_id=worker_id, lease_token=lease_token,
                error=f"{type(exc).__name__}:{exc}", retry=True,
            )
        except ManualSchedulerError as failure:
            db.rollback()
            if "lease_lost" in str(failure):
                return {"status": "lease_lost", "job_id": job.id, "job_type": job.job_type, "recovered": recovered}
            raise
        return {"status": row.status, "job_id": row.id, "job_type": row.job_type, "error": row.blocked_reason, "recovered": recovered}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A-share manual daily durable worker")
    parser.add_argument("--worker-id", default="manual-worker-local")
    parser.add_argument("--once", action="store_true", help="process at most one job and exit")
    parser.add_argument("--interval-seconds", type=int, default=15)
    parser.add_argument("--enqueue-account", help="enqueue the five daily jobs for this account")
    parser.add_argument("--date", dest="run_date", default=datetime.now(UTC).date().isoformat())
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parser().parse_args()
    if args.interval_seconds < 1 or args.interval_seconds > 60:
        raise SystemExit("--interval-seconds must be between 1 and 60")
    init_db()
    calendar = TradingCalendar()
    if args.enqueue_account:
        day = date.fromisoformat(args.run_date)
        coverage = calendar.ensure_coverage(day, day)
        if not coverage.get("complete"):
            logger.error("TRADING_CALENDAR_UNAVAILABLE: %s", coverage)
            return 2
        from server.services.manual_scheduler import enqueue_daily_jobs
        with SessionLocal() as db:
            jobs = enqueue_daily_jobs(db, account_id=args.enqueue_account, run_date=day, calendar=calendar)
            logger.info("enqueued %d jobs for %s", len(jobs), day.isoformat())
    while True:
        with SessionLocal() as db:
            summary = run_worker_cycle(db, worker_id=args.worker_id)
        logger.info("manual worker: %s", summary)
        if args.once:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
