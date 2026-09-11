"""M8 prospective-paper lifecycle tests, including the hard real-forward gate."""
from datetime import date
import hashlib

import pytest

from server.models.schema import ManualPilotObservation, ManualProspectivePilot, StrategyRelease
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


def _legacy_real_pilot(db_session, release, *, status="observing", report_hash=None):
    """Represent a pre-H2d row without using the retired creation API."""
    suffix = "passed" if status == "passed" else "observing"
    row = ManualProspectivePilot(
        id=f"legacy-real-pilot-{suffix}",
        pilot_key=f"legacy-real-pilot-key-{suffix}",
        release_id=release.id,
        strategy_fingerprint=release.strategy_fingerprint,
        start_date="2024-01-01",
        target_days=30,
        data_mode="real_forward",
        status=status,
        report_hash=report_hash,
        required_evidence="{}",
    )
    db_session.add(row)
    db_session.commit()
    return row


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
    with pytest.raises(ManualPilotError, match="pilot_requires_evidence_bound_start"):
        create_pilot(
            db_session, release_id=release.id, strategy_fingerprint="b" * 64,
            start_date="2024-01-01", data_mode="real_forward", pilot_key="pilot-real-1",
        )


def test_legacy_real_forward_self_reported_row_cannot_record_or_finalize(db_session):
    release = _release(db_session)
    pilot = _legacy_real_pilot(db_session, release)
    # This is the old false-positive shape: thirty reconciled, self-reported
    # days could previously be finalized as passed.  Keep the fixture so the
    # H2d regression proves that the old report is not re-admitted.
    day = date(2024, 1, 2)
    fake_days = []
    while len(fake_days) < 30:
        if WeekdayCalendar().is_trading_day(day):
            fake_days.append(day)
        day = date.fromordinal(day.toordinal() + 1)
    for index, observed_day in enumerate(fake_days):
        db_session.add(ManualPilotObservation(
            id=f"legacy-observation-{index}", pilot_id=pilot.id,
            observation_date=observed_day.isoformat(), data_as_of=observed_day.isoformat(),
            received_at=f"{observed_day.isoformat()}T08:00:00+00:00",
            input_hash=_hash(f"legacy-input-{index}"), signal_hash=_hash(f"legacy-signal-{index}"),
            observed_action="hold", reconciled=True, source="real_forward",
            idempotency_key=f"legacy-key-{index}",
        ))
    db_session.commit()
    with pytest.raises(ManualPilotError, match="requires_trusted_daily_checkpoint"):
        record_pilot_observation(
            db_session, pilot.id, observation_date="2024-01-02", data_as_of="2024-01-02",
            received_at="2024-01-02T08:00:00+00:00", input_hash=_hash("legacy-input"),
            signal_hash=_hash("legacy-signal"), observed_action="hold", reconciled=True,
            idempotency_key="legacy-observation", calendar=WeekdayCalendar(),
        )
    with pytest.raises(ManualPilotError, match="requires_trusted_daily_checkpoint"):
        finalize_pilot(db_session, pilot.id, evidence={}, calendar=WeekdayCalendar())

    # A historical row that already says ``passed`` remains an audit row; the
    # report-hash idempotency shortcut cannot turn it into trusted evidence.
    passed = _legacy_real_pilot(db_session, release, status="passed", report_hash=_hash("legacy-report"))
    with pytest.raises(ManualPilotError, match="requires_trusted_daily_checkpoint"):
        finalize_pilot(db_session, passed.id, evidence={}, calendar=WeekdayCalendar())


def test_real_forward_finalize_rejects_non_consecutive_observation_window(db_session):
    release = _release(db_session)
    with pytest.raises(ManualPilotError, match="pilot_requires_evidence_bound_start"):
        create_pilot(
            db_session, release_id=release.id, strategy_fingerprint="b" * 64,
            start_date="2024-01-01", data_mode="real_forward", pilot_key="pilot-real-gap",
        )


def test_finalize_rejects_malformed_numeric_evidence(db_session):
    release = _release(db_session)
    pilot = create_pilot(
        db_session, release_id=release.id, strategy_fingerprint="b" * 64,
        start_date="2024-01-01", data_mode="synthetic_engineering", pilot_key="pilot-bad-evidence",
    )
    with pytest.raises(ManualPilotError, match="p0_count_must_be_integer"):
        finalize_pilot(db_session, pilot.id, evidence={"p0_count": "not-an-int"}, calendar=WeekdayCalendar())


def test_real_forward_rejects_wrong_fingerprint_future_and_late_backfill(db_session):
    release = _release(db_session)
    with pytest.raises(ManualPilotError, match="fingerprint_mismatch"):
        create_pilot(
            db_session, release_id=release.id, strategy_fingerprint="c" * 64,
            start_date="2026-09-10", data_mode="real_forward", pilot_key="wrong-fingerprint",
        )
    with pytest.raises(ManualPilotError, match="pilot_requires_evidence_bound_start"):
        create_pilot(
            db_session, release_id=release.id, strategy_fingerprint="b" * 64,
            start_date="2026-09-10", data_mode="real_forward", pilot_key="future-guard",
        )
