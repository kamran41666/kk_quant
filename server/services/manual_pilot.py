"""Strict prospective 30-trading-day paper-observation lifecycle."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Mapping, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.models.schema import ManualPilotObservation, ManualProspectivePilot, StrategyRelease, manual_now_str, uuid4_str


class ManualPilotError(ValueError):
    pass


class CalendarLike(Protocol):
    def is_trading_day(self, value: date) -> bool:
        ...


@dataclass(frozen=True)
class ManualProspectivePolicyV1:
    target_days: int = 30
    maximum_drawdown: Decimal = Decimal("0.20")
    maximum_daily_loss_breaches: int = 0
    policy_version: str = "manual-prospective-paper-v1"

    def __post_init__(self) -> None:
        if self.target_days != 30:
            raise ManualPilotError("manual_pilot_target_days_must_be_30")
        if self.maximum_daily_loss_breaches < 0:
            raise ManualPilotError("maximum_daily_loss_breaches_must_not_be_negative")
        value = self.maximum_drawdown if isinstance(self.maximum_drawdown, Decimal) else Decimal(str(self.maximum_drawdown))
        if not value.is_finite() or value < 0 or value > 1:
            raise ManualPilotError("maximum_drawdown_must_be_ratio")
        object.__setattr__(self, "maximum_drawdown", value)

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_days": self.target_days, "maximum_drawdown": str(self.maximum_drawdown),
            "maximum_daily_loss_breaches": self.maximum_daily_loss_breaches,
            "policy_version": self.policy_version,
        }

    @property
    def policy_hash(self) -> str:
        return _hash(self.as_dict())


def _date(value: date | datetime | str, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ManualPilotError(f"{field}_must_be_iso_date") from exc


def _received(value: str | datetime) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManualPilotError("received_at_must_be_iso_timestamp") from exc
    if parsed.tzinfo is None:
        raise ManualPilotError("received_at_requires_timezone")
    return parsed.astimezone(timezone.utc)


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _return(value: Any) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ManualPilotError("actual_return_must_be_numeric") from exc
    if not result.is_finite():
        raise ManualPilotError("actual_return_must_be_finite")
    return result.quantize(Decimal("0.0000000001"))


def _evidence_int(evidence: Mapping[str, Any], field: str, default: int) -> int:
    value = evidence.get(field, default)
    if isinstance(value, bool):
        raise ManualPilotError(f"{field}_must_be_integer")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ManualPilotError(f"{field}_must_be_integer") from exc
    if str(value).strip() != str(result) and not isinstance(value, int):
        raise ManualPilotError(f"{field}_must_be_integer")
    return result


def _evidence_decimal(evidence: Mapping[str, Any], field: str, default: str) -> Decimal:
    try:
        result = Decimal(str(evidence.get(field, default)))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ManualPilotError(f"{field}_must_be_numeric") from exc
    if not result.is_finite():
        raise ManualPilotError(f"{field}_must_be_finite")
    return result


def _expected_trading_days(start_date: date, target_days: int, calendar: CalendarLike) -> list[date]:
    expected: list[date] = []
    cursor = start_date + timedelta(days=1)
    # A 30-trading-day window should fit comfortably in this bound even with
    # exchange holidays. A bound keeps a broken calendar from looping forever.
    deadline = start_date + timedelta(days=3660)
    try:
        while cursor <= deadline and len(expected) < target_days:
            if calendar.is_trading_day(cursor):
                expected.append(cursor)
            cursor += timedelta(days=1)
    except Exception as exc:
        raise ManualPilotError("TRADING_CALENDAR_UNAVAILABLE") from exc
    if len(expected) != target_days:
        raise ManualPilotError("TRADING_CALENDAR_UNAVAILABLE")
    return expected


def create_pilot(
    db: Session,
    *,
    release_id: str,
    strategy_fingerprint: str,
    start_date: date | str,
    data_mode: str = "real_forward",
    pilot_key: str | None = None,
    policy: ManualProspectivePolicyV1 | None = None,
) -> ManualProspectivePilot:
    release = db.get(StrategyRelease, release_id)
    if release is None:
        raise ManualPilotError("strategy_release_not_found")
    if release.status not in {"holdout_passed", "paper_observing", "paper_passed", "manual_ready"}:
        raise ManualPilotError("release_not_ready_for_paper_observation")
    if len(str(strategy_fingerprint)) != 64:
        raise ManualPilotError("strategy_fingerprint_must_be_sha256")
    if data_mode not in {"real_forward", "synthetic_engineering"}:
        raise ManualPilotError("unsupported_manual_pilot_data_mode")
    day = _date(start_date, "start_date")
    policy = policy or ManualProspectivePolicyV1()
    key = str(pilot_key or f"{release_id}:{day.isoformat()}:{data_mode}")
    existing = db.scalars(select(ManualProspectivePilot).where(ManualProspectivePilot.pilot_key == key)).first()
    if existing:
        if existing.release_id != release_id or existing.start_date != day.isoformat() or existing.data_mode != data_mode:
            raise ManualPilotError("manual_pilot_idempotency_conflict")
        return existing
    row = ManualProspectivePilot(
        id=uuid4_str(), pilot_key=key, release_id=release_id, strategy_fingerprint=str(strategy_fingerprint),
        start_date=day.isoformat(), target_days=policy.target_days, data_mode=data_mode,
        required_evidence=json.dumps(policy.as_dict(), sort_keys=True), status="planned",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def record_pilot_observation(
    db: Session,
    pilot_id: str,
    *,
    observation_date: date | str,
    data_as_of: date | str,
    received_at: str | datetime,
    input_hash: str,
    signal_hash: str,
    observed_action: str,
    actual_return: Any = Decimal("0"),
    reconciled: bool = False,
    source: str | None = None,
    idempotency_key: str,
    notes: str | None = None,
    calendar: CalendarLike,
) -> ManualPilotObservation:
    pilot = db.get(ManualProspectivePilot, pilot_id)
    if pilot is None:
        raise ManualPilotError("manual_pilot_not_found")
    if pilot.status in {"passed", "failed", "blocked"}:
        raise ManualPilotError("manual_pilot_is_immutable")
    day, as_of = _date(observation_date, "observation_date"), _date(data_as_of, "data_as_of")
    if day <= _date(pilot.start_date, "pilot_start_date"):
        raise ManualPilotError("observation_must_arrive_after_pilot_start")
    if not calendar.is_trading_day(day):
        raise ManualPilotError("observation_date_not_trading_day")
    if as_of > day:
        raise ManualPilotError("data_as_of_after_observation_date")
    received = _received(received_at)
    if received.date() <= _date(pilot.start_date, "pilot_start_date") and pilot.data_mode == "real_forward":
        raise ManualPilotError("real_forward_data_must_arrive_after_pilot_start")
    expected_source = pilot.data_mode
    if (source or expected_source) != expected_source:
        raise ManualPilotError("pilot_observation_source_mismatch")
    if len(str(input_hash)) != 64 or len(str(signal_hash)) != 64:
        raise ManualPilotError("pilot_observation_hash_must_be_sha256")
    if not str(observed_action).strip():
        raise ManualPilotError("observed_action_required")
    key = str(idempotency_key).strip()
    if not key:
        raise ManualPilotError("pilot_observation_idempotency_required")
    existing = db.scalars(select(ManualPilotObservation).where(ManualPilotObservation.idempotency_key == key)).first()
    if existing:
        if existing.pilot_id != pilot_id or existing.observation_date != day.isoformat() or existing.input_hash != input_hash:
            raise ManualPilotError("pilot_observation_idempotency_conflict")
        return existing
    same_day = db.scalars(select(ManualPilotObservation).where(
        ManualPilotObservation.pilot_id == pilot_id, ManualPilotObservation.observation_date == day.isoformat(),
    )).first()
    if same_day:
        raise ManualPilotError("pilot_observation_date_already_recorded")
    row = ManualPilotObservation(
        id=uuid4_str(), pilot_id=pilot_id, observation_date=day.isoformat(), data_as_of=as_of.isoformat(),
        received_at=received.isoformat(), input_hash=input_hash, signal_hash=signal_hash,
        observed_action=str(observed_action).strip(), actual_return=_return(actual_return),
        reconciled=bool(reconciled), source=expected_source, idempotency_key=key, notes=notes,
    )
    db.add(row)
    pilot.status = "observing"
    db.flush()
    pilot.observation_days = db.query(ManualPilotObservation).filter(ManualPilotObservation.pilot_id == pilot_id).count()
    pilot.valid_days = db.query(ManualPilotObservation).filter(
        ManualPilotObservation.pilot_id == pilot_id, ManualPilotObservation.reconciled.is_(True),
    ).count()
    db.commit()
    db.refresh(row)
    return row


def finalize_pilot(
    db: Session,
    pilot_id: str,
    *,
    evidence: Mapping[str, Any],
    calendar: CalendarLike,
    policy: ManualProspectivePolicyV1 | None = None,
) -> ManualProspectivePilot:
    pilot = db.get(ManualProspectivePilot, pilot_id)
    if pilot is None:
        raise ManualPilotError("manual_pilot_not_found")
    if pilot.report_hash:
        return pilot
    policy = policy or ManualProspectivePolicyV1(target_days=pilot.target_days)
    observations = db.scalars(select(ManualPilotObservation).where(
        ManualPilotObservation.pilot_id == pilot_id,
    ).order_by(ManualPilotObservation.observation_date.asc())).all()
    pilot.observation_days = len(observations)
    pilot.valid_days = sum(1 for item in observations if item.reconciled)
    expected_days = _expected_trading_days(_date(pilot.start_date, "pilot_start_date"), pilot.target_days, calendar)
    observed_days = [_date(item.observation_date, "observation_date") for item in observations]
    required = {
        "research_passed": bool(evidence.get("research_passed")),
        "portfolio_passed": bool(evidence.get("portfolio_passed")),
        "holdout_passed": bool(evidence.get("holdout_passed")),
        "replay_consistent": bool(evidence.get("replay_consistent")),
        "risk_rules_passed": bool(evidence.get("risk_rules_passed")),
        "data_health_passed": bool(evidence.get("data_health_passed")),
        "all_days_reconciled": pilot.valid_days == pilot.target_days and len(observations) == pilot.target_days,
        "exact_trading_day_sequence": observed_days == expected_days,
        "no_p0_p1": _evidence_int(evidence, "p0_count", 1) == 0 and _evidence_int(evidence, "p1_count", 1) == 0,
        "daily_loss_breaches": _evidence_int(evidence, "daily_loss_breaches", 1) == policy.maximum_daily_loss_breaches,
        "drawdown_gate": _evidence_decimal(evidence, "max_drawdown", "1") <= policy.maximum_drawdown,
    }
    failed = [name for name, passed in required.items() if not passed]
    if pilot.data_mode == "synthetic_engineering":
        failed.append("synthetic_engineering_not_eligible_for_pass")
    if len(observations) < pilot.target_days:
        failed.append("insufficient_observation_days")
    passed = not failed
    report = {
        "pilot_id": pilot.id, "release_id": pilot.release_id, "data_mode": pilot.data_mode,
        "target_days": pilot.target_days, "observation_days": len(observations), "valid_days": pilot.valid_days,
        "checks": required, "failed": failed, "passed": passed,
        "observation_hashes": [item.input_hash for item in observations],
        "evidence": dict(evidence), "policy_hash": policy.policy_hash,
    }
    pilot.status = "passed" if passed else "failed"
    pilot.blocked_reason = None if passed else ";".join(failed)
    pilot.report_json = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    pilot.report_hash = _hash(report)
    pilot.completed_at = manual_now_str()
    db.commit()
    db.refresh(pilot)
    return pilot


__all__ = [
    "ManualPilotError", "ManualProspectivePolicyV1", "create_pilot",
    "record_pilot_observation", "finalize_pilot",
]
