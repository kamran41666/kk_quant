"""Recoverable scheduler primitives for the manual daily cycle.

The worker is intentionally a short-lived process. Durable rows are claimed
with a database compare-and-set (CAS), and every mutation is fenced by the
lease owner, a random lease token, and the unexpired lease deadline.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from server.models.database import ensure_savepoint_transaction
from server.models.schema import ManualDailyJob


class ManualSchedulerError(ValueError):
    pass


class TradingDayLike(Protocol):
    def is_trading_day(self, value: date) -> bool:
        ...


_ACTIVE_STATUSES = ("leased", "running")


def _as_date(value: date | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ManualSchedulerError("run_date_must_be_iso_date") from exc


def _as_utc(value: datetime | str | None = None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ManualSchedulerError("scheduler_time_must_be_iso_timestamp") from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _retryable_database_error(exc: OperationalError) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in ("database is locked", "database is busy", "serialization", "deadlock"))


def _database_error(exc: OperationalError) -> ManualSchedulerError:
    return ManualSchedulerError("scheduler_database_unavailable")


def _validate_lease_seconds(lease_seconds: int) -> None:
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
        raise ManualSchedulerError("lease_seconds_must_be_integer")
    if lease_seconds < 10:
        raise ManualSchedulerError("lease_seconds_too_short")


def _validate_lease_token(lease_token: str) -> str:
    token = str(lease_token).strip()
    if not token:
        raise ManualSchedulerError("lease_token_required")
    return token


def _hash_result(result: Any) -> str:
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_job(db: Session, job_id: str) -> ManualDailyJob:
    row = db.get(ManualDailyJob, job_id)
    if row is None:
        raise ManualSchedulerError("manual_job_not_found")
    return row


def _refresh_job(db: Session, job_id: str) -> ManualDailyJob:
    row = _load_job(db, job_id)
    db.refresh(row)
    return row


def enqueue_daily_jobs(
    db: Session,
    *,
    account_id: str,
    run_date: date | str,
    calendar: TradingDayLike,
    job_types: Sequence[str] = ("open_plan", "close_plan", "valuation", "reconcile", "review"),
    release_id: str | None = None,
    authorization_id: str | None = None,
    decision_id: str | None = None,
) -> list[ManualDailyJob]:
    day = _as_date(run_date)
    if not calendar.is_trading_day(day):
        return []
    allowed = {"open_plan", "close_plan", "valuation", "reconcile", "review"}
    requested = list(dict.fromkeys(job_types))
    if not requested or any(item not in allowed for item in requested):
        raise ManualSchedulerError("unsupported_manual_job_type")

    rows: list[ManualDailyJob] = []
    for job_type in requested:
        key = f"manual:{account_id}:{day.isoformat()}:{job_type}:{decision_id or '-'}"
        existing = db.scalars(select(ManualDailyJob).where(ManualDailyJob.job_key == key)).first()
        if existing is not None:
            rows.append(existing)
            continue
        row = ManualDailyJob(
            job_key=key, account_id=account_id, release_id=release_id,
            authorization_id=authorization_id, decision_id=decision_id,
            run_date=day.isoformat(), job_type=job_type, status="pending",
        )
        # The unique constraint is the concurrency boundary. A savepoint
        # lets a losing enqueue race recover by reading the winner's row.
        try:
            ensure_savepoint_transaction(db)
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            row = db.scalars(select(ManualDailyJob).where(ManualDailyJob.job_key == key)).one()
        rows.append(row)
    db.commit()
    for row in rows:
        db.refresh(row)
    return rows


def _claimable_clause(now_text: str):
    return or_(
        ManualDailyJob.status == "pending",
        ManualDailyJob.status.in_(_ACTIVE_STATUSES),
    ) & or_(
        ManualDailyJob.status == "pending",
        ManualDailyJob.lease_until <= now_text,
    )


def lease_next_job(
    db: Session,
    *,
    worker_id: str,
    now: datetime | str | None = None,
    lease_seconds: int = 120,
) -> ManualDailyJob | None:
    owner = str(worker_id).strip()
    if not owner:
        raise ManualSchedulerError("worker_id_required")
    _validate_lease_seconds(lease_seconds)
    moment = _as_utc(now)
    now_text = moment.isoformat()
    until_text = (moment + timedelta(seconds=lease_seconds)).isoformat()

    # A failed UPDATE means another process won this candidate. Re-read the
    # ordered queue and try the next eligible candidate until one is claimed.
    retries = 0
    while True:
        candidate = db.scalars(
            select(ManualDailyJob)
            .where(_claimable_clause(now_text))
            .order_by(ManualDailyJob.run_date.asc(), ManualDailyJob.created_at.asc(), ManualDailyJob.id.asc())
            .limit(1)
        ).first()
        if candidate is None:
            return None
        lease_token = uuid4().hex
        statement = (
            update(ManualDailyJob)
            .where(ManualDailyJob.id == candidate.id, _claimable_clause(now_text))
            .values(
                status="leased", lease_owner=owner, lease_token=lease_token,
                lease_until=until_text, heartbeat_at=now_text,
                started_at=func.coalesce(ManualDailyJob.started_at, now_text),
                attempt_count=ManualDailyJob.attempt_count + 1,
                updated_at=now_text,
            )
        )
        try:
            result = db.execute(statement)
            if result.rowcount != 1:
                db.rollback()
                continue
            db.commit()
        except OperationalError as exc:
            db.rollback()
            if _retryable_database_error(exc) and retries < 3:
                retries += 1
                time.sleep(0.01 * retries)
                continue
            raise _database_error(exc) from exc
        return _refresh_job(db, candidate.id)


def _fenced_update(
    db: Session,
    job_id: str,
    *,
    worker_id: str,
    lease_token: str,
    now: datetime,
    values: dict[str, Any],
) -> ManualDailyJob:
    token = _validate_lease_token(lease_token)
    now_text = now.isoformat()
    statement = (
        update(ManualDailyJob)
        .where(
            ManualDailyJob.id == job_id,
            ManualDailyJob.lease_owner == str(worker_id).strip(),
            ManualDailyJob.lease_token == token,
            ManualDailyJob.status.in_(_ACTIVE_STATUSES),
            ManualDailyJob.lease_until.is_not(None),
            ManualDailyJob.lease_until > now_text,
        )
        .values(**values)
    )
    retries = 0
    while True:
        try:
            result = db.execute(statement)
            if result.rowcount != 1:
                db.rollback()
                _load_job(db, job_id)
                raise ManualSchedulerError("lease_lost")
            db.commit()
            break
        except OperationalError as exc:
            db.rollback()
            if _retryable_database_error(exc) and retries < 3:
                retries += 1
                time.sleep(0.01 * retries)
                continue
            raise _database_error(exc) from exc
    return _refresh_job(db, job_id)


def heartbeat_job(
    db: Session,
    job_id: str,
    *,
    worker_id: str,
    lease_token: str,
    now: datetime | str | None = None,
    lease_seconds: int = 120,
) -> ManualDailyJob:
    _validate_lease_seconds(lease_seconds)
    moment = _as_utc(now)
    return _fenced_update(
        db, job_id, worker_id=worker_id, lease_token=lease_token, now=moment,
        values={
            "status": "running", "heartbeat_at": moment.isoformat(),
            "lease_until": (moment + timedelta(seconds=lease_seconds)).isoformat(),
            "updated_at": moment.isoformat(),
        },
    )


def complete_job(
    db: Session, job_id: str, *, worker_id: str, lease_token: str,
    result: Any = None, now: datetime | str | None = None,
) -> ManualDailyJob:
    moment = _as_utc(now)
    timestamp = moment.isoformat()
    return _fenced_update(
        db, job_id, worker_id=worker_id, lease_token=lease_token, now=moment,
        values={
            "status": "succeeded", "result_hash": _hash_result(result),
            "completed_at": timestamp, "lease_owner": None,
            "lease_until": None, "lease_token": None, "updated_at": timestamp,
        },
    )


def block_job(
    db: Session, job_id: str, *, worker_id: str, lease_token: str,
    reason: str, now: datetime | str | None = None,
) -> ManualDailyJob:
    clean_reason = str(reason).strip()
    if not clean_reason:
        raise ManualSchedulerError("blocked_reason_required")
    moment = _as_utc(now)
    timestamp = moment.isoformat()
    return _fenced_update(
        db, job_id, worker_id=worker_id, lease_token=lease_token, now=moment,
        values={
            "status": "blocked", "blocked_reason": clean_reason,
            "completed_at": timestamp, "lease_owner": None,
            "lease_until": None, "lease_token": None, "updated_at": timestamp,
        },
    )


def fail_job(
    db: Session, job_id: str, *, worker_id: str, lease_token: str,
    error: str, retry: bool = True, now: datetime | str | None = None,
) -> ManualDailyJob:
    moment = _as_utc(now)
    timestamp = moment.isoformat()
    return _fenced_update(
        db, job_id, worker_id=worker_id, lease_token=lease_token, now=moment,
        values={
            "status": "pending" if retry else "failed",
            "blocked_reason": str(error).strip() or "manual_job_failed",
            "completed_at": None if retry else timestamp,
            "lease_owner": None, "lease_until": None, "lease_token": None,
            "updated_at": timestamp,
        },
    )


def recover_expired_jobs(db: Session, *, now: datetime | str | None = None) -> int:
    now_text = _as_utc(now).isoformat()
    statement = (
        update(ManualDailyJob)
        .where(
            ManualDailyJob.status.in_(_ACTIVE_STATUSES),
            ManualDailyJob.lease_until.is_not(None),
            ManualDailyJob.lease_until <= now_text,
        )
        .values(
            status="pending", lease_owner=None, lease_until=None,
            lease_token=None, heartbeat_at=None, updated_at=now_text,
        )
    )
    try:
        result = db.execute(statement)
        db.commit()
    except OperationalError as exc:
        db.rollback()
        raise _database_error(exc) from exc
    return int(result.rowcount or 0)


__all__ = [
    "ManualSchedulerError",
    "block_job",
    "complete_job",
    "enqueue_daily_jobs",
    "fail_job",
    "heartbeat_job",
    "lease_next_job",
    "recover_expired_jobs",
]
