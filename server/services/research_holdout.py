"""Sealed research holdout lifecycle with explicit access evidence."""
from __future__ import annotations

from datetime import date
from sqlalchemy import select
from sqlalchemy.orm import Session

from server.models.schema import ResearchHoldoutAccess, ResearchHoldoutWindow, manual_now_str, uuid4_str


class HoldoutError(ValueError):
    pass


PREVIOUSLY_OPENED_INTERVALS = (
    (date(2015, 1, 1), date(2015, 12, 31)),
    (date(2019, 1, 1), date(2022, 12, 31)),
    (date(2023, 1, 1), date(2026, 8, 31)),
)


def _date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def create_holdout_window(
    db: Session,
    *,
    dataset_id: str,
    data_content_hash: str,
    start_date: date | str,
    end_date: date | str,
    policy_hash: str,
) -> ResearchHoldoutWindow:
    start, end = _date(start_date), _date(end_date)
    if start > end:
        raise HoldoutError("holdout_start_must_not_exceed_end")
    if len(data_content_hash) != 64 or len(policy_hash) != 64:
        raise HoldoutError("holdout_hashes_must_be_sha256")
    if any(start <= opened_end and end >= opened_start for opened_start, opened_end in PREVIOUSLY_OPENED_INTERVALS):
        raise HoldoutError("holdout_range_was_already_exposed")
    for existing in db.scalars(select(ResearchHoldoutWindow)).all():
        existing_start, existing_end = _date(existing.start_date), _date(existing.end_date)
        if start <= existing_end and end >= existing_start:
            raise HoldoutError("holdout_range_already_registered")
    row = ResearchHoldoutWindow(
        id=uuid4_str(), dataset_id=dataset_id, data_content_hash=data_content_hash,
        start_date=start.isoformat(), end_date=end.isoformat(), policy_hash=policy_hash,
        status="sealed",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def access_holdout(
    db: Session,
    window_id: str,
    *,
    accessed_by: str,
    purpose: str,
    result_exposed: bool = True,
) -> ResearchHoldoutAccess:
    window = db.get(ResearchHoldoutWindow, window_id)
    if window is None:
        raise HoldoutError("holdout_not_found")
    if window.status in {"invalidated", "completed"}:
        raise HoldoutError(f"holdout_{window.status}")
    now = manual_now_str()
    if window.status == "sealed":
        window.status = "opened"
        window.opened_at = now
    access = ResearchHoldoutAccess(
        id=uuid4_str(), window_id=window_id, accessed_at=now,
        accessed_by=str(accessed_by), purpose=str(purpose), result_exposed=bool(result_exposed),
    )
    db.add(access)
    db.commit()
    db.refresh(access)
    return access


def invalidate_holdout(db: Session, window_id: str, *, reason: str) -> ResearchHoldoutWindow:
    window = db.get(ResearchHoldoutWindow, window_id)
    if window is None:
        raise HoldoutError("holdout_not_found")
    if window.status == "completed":
        raise HoldoutError("completed_holdout_cannot_be_invalidated")
    window.status = "invalidated"
    window.invalidated_at = manual_now_str()
    db.commit()
    db.refresh(window)
    return window


def complete_holdout(db: Session, window_id: str) -> ResearchHoldoutWindow:
    window = db.get(ResearchHoldoutWindow, window_id)
    if window is None:
        raise HoldoutError("holdout_not_found")
    if window.status == "invalidated":
        raise HoldoutError("holdout_invalidated")
    if window.status != "opened":
        raise HoldoutError("holdout_must_be_opened_before_completion")
    window.status = "completed"
    db.commit()
    db.refresh(window)
    return window


def holdout_accesses(db: Session, window_id: str) -> list[ResearchHoldoutAccess]:
    return list(db.scalars(select(ResearchHoldoutAccess).where(
        ResearchHoldoutAccess.window_id == window_id,
    ).order_by(ResearchHoldoutAccess.accessed_at.asc(), ResearchHoldoutAccess.id.asc())).all())


__all__ = ["HoldoutError", "create_holdout_window", "access_holdout", "invalidate_holdout", "complete_holdout", "holdout_accesses"]
