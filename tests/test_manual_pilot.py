"""M8 prospective-paper lifecycle tests, including the hard real-forward gate."""
from datetime import date, datetime, timedelta, timezone
import hashlib

import pytest

from server.models.schema import StrategyRelease
from server.services.manual_pilot import (
    ManualPilotError,
    create_pilot,
    finalize_pilot,
    record_pilot_observation,
)
from server.services.strategy_promotion import create_strategy_release


class WeekdayCalendar:
    def is_trading_day(self, value: date) -> bool:
        return value.weekday() < 5


def _release(db_session):
    release = create_strategy_release(
        db_session,
        strategy_key="manual-pilot-test",
        version="v1",
        bundle_hash="a" * 64,
        strategy_fingerprint="b" * 64,
        research_evidence={"source": "test"},
        execution_policy={"auto_submit": False},
        risk_policy={"paper_only": True},
    )
    release.status = "holdout_passed"
    db_session.commit()
    return release


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_synthetic_engineering_can_validate_code_but_never_pass_pilot(db_session):
    release = _release(db_session)
    pilot = create_pilot(
        db_session, release_id=release.id, strategy_fingerprint="b" * 64,
        start_date="2024-01-01", data_mode="synthetic_engineering",
    )
    record_pilot_observation(
        db_session, pilot.id, observation_date="2024-01-02", data_as_of="2024-01-02",
        received_at="2024-01-02T08:00:00+00:00", input_hash=_hash("c"), signal_hash=_hash("d"),
        observed_action="hold", reconciled=True, idempotency_key="pilot-synthetic-day-1", calendar=WeekdayCalendar(),
    )
    finalized = finalize_pilot(db_session, pilot.id, evidence={
        "research_passed": True, "portfolio_passed": True, "holdout_passed": True,
        "replay_consistent": True, "risk_rules_passed": True, "data_health_passed": True,
        "p0_count": 0, "p1_count": 0, "max_drawdown": "0", "daily_loss_breaches": 0,
    }, calendar=WeekdayCalendar())
    assert finalized.status == "failed"
    assert "synthetic_engineering_not_eligible_for_pass" in finalized.blocked_reason


def test_real_forward_data_arrival_and_exact_30_days_gate(db_session):
    release = _release(db_session)
    pilot = create_pilot(
        db_session, release_id=release.id, strategy_fingerprint="b" * 64,
        start_date="2024-01-01", data_mode="real_forward", pilot_key="pilot-real-1",
    )
    with pytest.raises(ManualPilotError, match="arrive_after"):
        record_pilot_observation(
            db_session, pilot.id, observation_date="2024-01-02", data_as_of="2024-01-02",
            received_at="2024-01-01T23:00:00+00:00", input_hash=_hash("e"), signal_hash=_hash("f"),
            observed_action="hold", reconciled=True, idempotency_key="pilot-real-bad", calendar=WeekdayCalendar(),
        )
    days = []
    current = date(2024, 1, 2)
    while len(days) < 30:
        if WeekdayCalendar().is_trading_day(current):
            days.append(current)
        current += timedelta(days=1)
    for index, day in enumerate(days):
        record_pilot_observation(
            db_session, pilot.id, observation_date=day, data_as_of=day,
            received_at=datetime(day.year, day.month, day.day, 8, tzinfo=timezone.utc),
            input_hash=_hash(format(index % 10, "x")), signal_hash=_hash(format((index + 1) % 10, "x")),
            observed_action="hold", reconciled=True, idempotency_key=f"pilot-real-{index}", calendar=WeekdayCalendar(),
        )
    finalized = finalize_pilot(db_session, pilot.id, evidence={
        "research_passed": True, "portfolio_passed": True, "holdout_passed": True,
        "replay_consistent": True, "risk_rules_passed": True, "data_health_passed": True,
        "p0_count": 0, "p1_count": 0, "max_drawdown": "0.10", "daily_loss_breaches": 0,
    }, calendar=WeekdayCalendar())
    assert finalized.status == "passed"
    assert finalized.report_hash and len(finalized.report_hash) == 64
    assert db_session.get(StrategyRelease, release.id).status == "holdout_passed"


def test_real_forward_finalize_rejects_non_consecutive_observation_window(db_session):
    release = _release(db_session)
    pilot = create_pilot(
        db_session, release_id=release.id, strategy_fingerprint="c" * 64,
        start_date="2024-01-01", data_mode="real_forward", pilot_key="pilot-real-gap",
    )
    days = []
    current = date(2024, 1, 2)
    while len(days) < 31:
        if WeekdayCalendar().is_trading_day(current):
            days.append(current)
        current += timedelta(days=1)
    for index, day in enumerate(days[:10] + days[11:]):
        record_pilot_observation(
            db_session, pilot.id, observation_date=day, data_as_of=day,
            received_at=datetime(day.year, day.month, day.day, 8, tzinfo=timezone.utc),
            input_hash=_hash(f"gap-input-{index}"), signal_hash=_hash(f"gap-signal-{index}"),
            observed_action="hold", reconciled=True, idempotency_key=f"pilot-gap-{index}", calendar=WeekdayCalendar(),
        )
    finalized = finalize_pilot(db_session, pilot.id, evidence={
        "research_passed": True, "portfolio_passed": True, "holdout_passed": True,
        "replay_consistent": True, "risk_rules_passed": True, "data_health_passed": True,
        "p0_count": 0, "p1_count": 0, "max_drawdown": "0", "daily_loss_breaches": 0,
    }, calendar=WeekdayCalendar())
    assert finalized.status == "failed"
    assert "exact_trading_day_sequence" in finalized.blocked_reason


def test_finalize_rejects_malformed_numeric_evidence(db_session):
    release = _release(db_session)
    pilot = create_pilot(
        db_session, release_id=release.id, strategy_fingerprint="d" * 64,
        start_date="2024-01-01", data_mode="synthetic_engineering", pilot_key="pilot-bad-evidence",
    )
    with pytest.raises(ManualPilotError, match="p0_count_must_be_integer"):
        finalize_pilot(db_session, pilot.id, evidence={"p0_count": "not-an-int"}, calendar=WeekdayCalendar())
