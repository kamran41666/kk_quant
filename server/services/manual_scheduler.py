"""Recoverable scheduler primitives for the manual daily cycle.

The worker is intentionally a short-lived process.  It leases durable rows,
does one bounded job, heartbeats while working, and exits; no broker client or
daemon thread is created here.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import threading
from typing import Any, Protocol, Sequence

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from server.models.schema import ManualDailyJob, manual_now_str


class ManualSchedulerError(ValueError):
    pass


class TradingDayLike(Protocol):
    def is_trading_day(self, value: date) -> bool:
        ...


_JOB_LOCK = threading.RLock()


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
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ManualSchedulerError("scheduler_time_must_be_iso_timestamp") from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _hash_result(result: Any) -> str:
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    with _JOB_LOCK:
        rows: list[ManualDailyJob] = []
        for job_type in requested:
            key = f"manual:{account_id}:{day.isoformat()}:{job_type}:{decision_id or '-'}"
            existing = db.scalars(select(ManualDailyJob).where(ManualDailyJob.job_key == key)).first()
            if existing:
                rows.append(existing)
                continue
            row = ManualDailyJob(
                job_key=key, account_id=account_id, release_id=release_id,
                authorization_id=authorization_id, decision_id=decision_id,
                run_date=day.isoformat(), job_type=job_type, status="pending",
            )
            db.add(row)
            rows.append(row)
        db.commit()
        for row in rows:
            db.refresh(row)
        return rows


def _owned_job(db: Session, job_id: str, worker_id: str) -> ManualDailyJob:
    row = db.get(ManualDailyJob, job_id)
    if row is None:
        raise ManualSchedulerError("manual_job_not_found")
    if row.lease_owner != worker_id or row.status not in {"leased", "running"}:
        raise ManualSchedulerError("manual_job_lease_not_owned")
    return row


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
    if lease_seconds < 10:
        raise ManualSchedulerError("lease_seconds_too_short")
    moment = _as_utc(now)
    now_text = moment.isoformat()
    until_text = (moment + timedelta(seconds=lease_seconds)).isoformat()
    with _JOB_LOCK:
        candidate = db.scalars(
            select(ManualDailyJob)
            .where(
                or_(
                    ManualDailyJob.status == "pending",
                    ManualDailyJob.status.in_(["leased", "running"]),
                ),
                or_(ManualDailyJob.lease_until.is_(None), ManualDailyJob.lease_until <= now_text),
            )
            .order_by(ManualDailyJob.run_date.asc(), ManualDailyJob.created_at.asc(), ManualDailyJob.id.asc())
            .limit(1)
        ).first()
        if candidate is None:
            return None
        candidate.status = "leased"
        candidate.lease_owner = owner
        candidate.lease_until = until_text
        candidate.heartbeat_at = now_text
        candidate.started_at = candidate.started_at or now_text
        candidate.attempt_count = int(candidate.attempt_count or 0) + 1
        candidate.updated_at = manual_now_str()
        db.commit()
        db.refresh(candidate)
        return candidate


def heartbeat_job(
    db: Session,
    job_id: str,
    *,
    worker_id: str,
    now: datetime | str | None = None,
    lease_seconds: int = 120,
) -> ManualDailyJob:
    moment = _as_utc(now)
    with _JOB_LOCK:
        row = _owned_job(db, job_id, worker_id)
        if row.lease_until and row.lease_until <= moment.isoformat():
            raise ManualSchedulerError("manual_job_lease_expired")
        row.status = "running"
        row.heartbeat_at = moment.isoformat()
        row.lease_until = (moment + timedelta(seconds=lease_seconds)).isoformat()
        row.updated_at = manual_now_str()
        db.commit()
        db.refresh(row)
        return row


def complete_job(db: Session, job_id: str, *, worker_id: str, result: Any = None) -> ManualDailyJob:
    with _JOB_LOCK:
        row = _owned_job(db, job_id, worker_id)
        row.status = "succeeded"
        row.result_hash = _hash_result(result)
        row.completed_at = manual_now_str()
        row.lease_owner = None
        row.lease_until = None
        row.updated_at = manual_now_str()
        db.commit()
        db.refresh(row)
        return row


def block_job(db: Session, job_id: str, *, worker_id: str, reason: str) -> ManualDailyJob:
    with _JOB_LOCK:
        row = _owned_job(db, job_id, worker_id)
        if not str(reason).strip():
            raise ManualSchedulerError("blocked_reason_required")
        row.status = "blocked"
        row.blocked_reason = str(reason).strip()
        row.completed_at = manual_now_str()
        row.lease_owner = None
        row.lease_until = None
        row.updated_at = manual_now_str()
        db.commit()
        db.refresh(row)
        return row


def fail_job(db: Session, job_id: str, *, worker_id: str, error: str, retry: bool = True) -> ManualDailyJob:
    with _JOB_LOCK:
        row = _owned_job(db, job_id, worker_id)
        row.status = "pending" if retry else "failed"
        row.blocked_reason = str(error).strip() or "manual_job_failed"
        row.completed_at = None if retry else manual_now_str()
        row.lease_owner = None
        row.lease_until = None
        row.updated_at = manual_now_str()
        db.commit()
        db.refresh(row)
        return row


def recover_expired_jobs(db: Session, *, now: datetime | str | None = None) -> int:
    now_text = _as_utc(now).isoformat()
    with _JOB_LOCK:
        rows = db.scalars(select(ManualDailyJob).where(
            ManualDailyJob.status.in_(["leased", "running"]),
            ManualDailyJob.lease_until.is_not(None),
            ManualDailyJob.lease_until <= now_text,
        )).all()
        for row in rows:
            row.status = "pending"
            row.lease_owner = None
            row.lease_until = None
            row.heartbeat_at = None
            row.updated_at = manual_now_str()
        db.commit()
        return len(rows)


__all__ = [
    "ManualSchedulerError", "enqueue_daily_jobs", "lease_next_job", "heartbeat_job",
    "complete_job", "block_job", "fail_job", "recover_expired_jobs",
]
