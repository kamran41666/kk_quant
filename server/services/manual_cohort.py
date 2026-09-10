"""Create durable two-sleeve cohorts from a ready daily decision."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.models.schema import DailyDecision, ManualCohort, ManualExecutionAuthorization, StrategyRelease


class CohortError(ValueError):
    pass


def create_decision_cohorts(db: Session, decision_id: str, *, calendar: object) -> list[ManualCohort]:
    decision = db.get(DailyDecision, decision_id)
    if decision is None:
        raise CohortError("daily_decision_not_found")
    existing = list(db.scalars(select(ManualCohort).where(ManualCohort.decision_id == decision_id).order_by(ManualCohort.sleeve_index.asc())).all())
    if existing:
        return existing
    if decision.status != "ready":
        raise CohortError("daily_decision_must_be_ready")
    authorization = db.get(ManualExecutionAuthorization, decision.authorization_id)
    release = db.get(StrategyRelease, decision.release_id)
    if authorization is None or release is None:
        raise CohortError("decision_authorization_or_release_missing")
    signal_date = date.fromisoformat(decision.signal_date)
    try:
        entry = calendar.next_trading_day(signal_date)
        exit_date = calendar.next_trading_day(entry)
    except Exception as exc:
        raise CohortError("TRADING_CALENDAR_UNAVAILABLE") from exc
    latest = db.scalars(select(ManualCohort).where(
        ManualCohort.authorization_id == decision.authorization_id,
    ).order_by(ManualCohort.signal_date.desc(), ManualCohort.sleeve_index.desc())).first()
    sleeve_index = 0 if latest is None else (int(latest.sleeve_index) + 1) % 2
    occupied = db.scalars(select(ManualCohort).where(
        ManualCohort.authorization_id == decision.authorization_id,
        ManualCohort.sleeve_index == sleeve_index,
        ManualCohort.status.in_({"planned", "entering", "open", "exiting", "blocked"}),
    )).first()
    if occupied is not None:
        raise CohortError("manual_cohort_sleeve_still_occupied")
    budget = Decimal(str(authorization.capital_limit)) * Decimal("0.45")
    cohort = ManualCohort(
        id=f"{decision.id}-sleeve-{sleeve_index}", authorization_id=decision.authorization_id,
        decision_id=decision.id, signal_date=signal_date.isoformat(),
        planned_entry_date=entry.isoformat(), planned_exit_date=exit_date.isoformat(),
        sleeve_index=sleeve_index, budget=budget, status="planned",
    )
    db.add(cohort)
    db.commit()
    db.refresh(cohort)
    return [cohort]


__all__ = ["CohortError", "create_decision_cohorts"]
