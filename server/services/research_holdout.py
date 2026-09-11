"""Sealed research holdout lifecycle with explicit access evidence."""
from __future__ import annotations

from datetime import date, datetime
import re
from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from server.models.schema import (
    ManualHoldoutBinding,
    ResearchHoldoutAccess,
    ResearchHoldoutRegistryLock,
    ResearchHoldoutWindow,
    manual_now_str,
    uuid4_str,
)


class HoldoutError(ValueError):
    pass


PREVIOUSLY_OPENED_INTERVALS = (
    (date(2015, 1, 1), date(2015, 12, 31)),
    (date(2019, 1, 1), date(2022, 12, 31)),
    (date(2023, 1, 1), date(2026, 8, 31)),
)


def _date(value: date | str) -> date:
    try:
        if isinstance(value, datetime):
            return value.date()
        return value if isinstance(value, date) else date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise HoldoutError("holdout_dates_must_be_iso") from exc


def _sha256(value: str, field: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise HoldoutError(f"{field}_must_be_sha256")


def _lock_registry(db: Session) -> None:
    """Acquire the single DB row before checking any holdout range.

    SQLite's ``OR IGNORE`` plus an update holds the write transaction until the
    caller commits.  Other backends use their native conflict clause where
    available; an unsupported backend fails closed rather than claiming range
    exclusion that it cannot provide.
    """
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        db.execute(insert(ResearchHoldoutRegistryLock).prefix_with("OR IGNORE").values(id=1, revision=0))
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        db.execute(pg_insert(ResearchHoldoutRegistryLock).values(id=1, revision=0).on_conflict_do_nothing(index_elements=["id"]))
    else:
        raise HoldoutError("holdout_registry_backend_unsupported")
    db.execute(
        update(ResearchHoldoutRegistryLock)
        .where(ResearchHoldoutRegistryLock.id == 1)
        .values(revision=ResearchHoldoutRegistryLock.revision + 1)
    )


def create_holdout_window(
    db: Session,
    *,
    dataset_id: str,
    data_content_hash: str,
    start_date: date | str,
    end_date: date | str,
    policy_hash: str,
    commit: bool = True,
) -> ResearchHoldoutWindow:
    _lock_registry(db)
    start, end = _date(start_date), _date(end_date)
    if start > end:
        raise HoldoutError("holdout_start_must_not_exceed_end")
    _sha256(data_content_hash, "data_content_hash")
    _sha256(policy_hash, "policy_hash")
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
    db.flush()
    if commit:
        db.commit()
        db.refresh(row)
    return row


def ensure_factor_period_unreserved(db: Session, start_date: date | str, end_date: date | str) -> None:
    """Reject factor work overlapping a sealed/opened managed holdout.

    The registry row is locked for the caller's transaction, so queue creation
    and managed holdout allocation cannot race around the same interval check.
    """
    _lock_registry(db)
    start, end = _date(start_date), _date(end_date)
    if start > end:
        raise HoldoutError("holdout_start_must_not_exceed_end")
    rows = db.execute(select(ResearchHoldoutWindow).join(
        ManualHoldoutBinding, ManualHoldoutBinding.window_id == ResearchHoldoutWindow.id,
    ).where(ResearchHoldoutWindow.status.in_(("sealed", "opened")))).scalars().all()
    for row in rows:
        row_start, row_end = _date(row.start_date), _date(row.end_date)
        if start <= row_end and end >= row_start:
            raise HoldoutError("factor_period_reserved_for_manual_holdout")


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
    if db.scalars(select(ManualHoldoutBinding.id).where(ManualHoldoutBinding.window_id == window_id)).first() is not None:
        raise HoldoutError("managed_holdout_requires_manual_service")
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
    if db.scalars(select(ManualHoldoutBinding.id).where(ManualHoldoutBinding.window_id == window_id)).first() is not None:
        raise HoldoutError("managed_holdout_requires_manual_service")
    if window.status == "completed":
        raise HoldoutError("completed_holdout_cannot_be_invalidated")
    window.status = "invalidated"
    window.invalidated_at = manual_now_str()
    window.invalidation_reason = str(reason)
    db.commit()
    db.refresh(window)
    return window


def complete_holdout(db: Session, window_id: str) -> ResearchHoldoutWindow:
    window = db.get(ResearchHoldoutWindow, window_id)
    if window is None:
        raise HoldoutError("holdout_not_found")
    if db.scalars(select(ManualHoldoutBinding.id).where(ManualHoldoutBinding.window_id == window_id)).first() is not None:
        raise HoldoutError("managed_holdout_requires_manual_service")
    if window.status == "invalidated":
        raise HoldoutError("holdout_invalidated")
    if window.status != "opened":
        raise HoldoutError("holdout_must_be_opened_before_completion")
    window.status = "completed"
    window.completed_at = manual_now_str()
    db.commit()
    db.refresh(window)
    return window


def holdout_accesses(db: Session, window_id: str) -> list[ResearchHoldoutAccess]:
    return list(db.scalars(select(ResearchHoldoutAccess).where(
        ResearchHoldoutAccess.window_id == window_id,
    ).order_by(ResearchHoldoutAccess.accessed_at.asc(), ResearchHoldoutAccess.id.asc())).all())


__all__ = [
    "HoldoutError", "create_holdout_window", "ensure_factor_period_unreserved",
    "access_holdout", "invalidate_holdout", "complete_holdout", "holdout_accesses",
]
