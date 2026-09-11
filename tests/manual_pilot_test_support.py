"""Shared synthetic H2d admission fixture.

The fixture performs the real SQLite/file holdout chain and only controls the
server clock and exchange calendar so admission tests remain deterministic.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from server.services.manual_evidence import advance_release_from_evidence
from server.services.manual_holdout_evaluation import freeze_holdout_evaluation, run_holdout_evaluation
from server.services import manual_pilot_admission as admission
from tests.manual_holdout_evaluation_test_support import build_holdout_case


def build_admitted_pilot(db, tmp_path, monkeypatch, *, maximum_daily_loss="0.02", admit: bool = True):
    case = build_holdout_case(db, tmp_path, monkeypatch)
    frozen = freeze_holdout_evaluation(db, **case["freeze_kwargs"])
    completed = run_holdout_evaluation(db, frozen.id, actor="pilot-fixture")
    holdout = advance_release_from_evidence(
        db,
        release_id=case["release"].id,
        target_status="holdout_passed",
        evidence_refs={"holdout_artifact_id": completed.result_artifact_id},
        actor="pilot-fixture",
        idempotency_key="pilot-fixture-holdout-promotion",
    )
    assert holdout.decision == "passed"

    started_at = datetime(2027, 4, 12, 1, 0, tzinfo=timezone.utc)
    start_date = started_at.astimezone(admission.SHANGHAI_TZ).date()
    expected = []
    cursor = start_date + timedelta(days=1)
    while len(expected) < 30:
        if cursor.weekday() < 5:
            expected.append(cursor)
        cursor += timedelta(days=1)
    report = {
        "calendar_version": "synthetic-engineering-calendar-v1",
        "source": "akshare:sina_trade_calendar",
        "content_hash": "c" * 64,
        "coverage_start": start_date.isoformat(),
        "coverage_end": expected[-1].isoformat(),
        "requested_start": start_date.isoformat(),
        "requested_end": expected[-1].isoformat(),
        "trading_days": [day.isoformat() for day in expected],
        "verified": True,
        "complete": True,
    }
    monkeypatch.setattr(admission, "_utc_now", lambda: started_at)
    monkeypatch.setattr(admission, "_calendar_snapshot", lambda _start: {
        "report": report, "dates": [day.isoformat() for day in expected],
        "source": report["source"], "content_hash": report["content_hash"],
        "coverage_start": report["coverage_start"], "coverage_end": report["coverage_end"],
    })
    if not admit:
        return {
            **case,
            "holdout_evaluation": holdout,
            "holdout_result": completed,
            "started_at": started_at,
            "first_session": date.fromisoformat(expected[0].isoformat()),
        }
    pilot = admission.start_paper_observation(
        db,
        release_id=case["release"].id,
        actor="pilot-fixture",
        idempotency_key="pilot-fixture-start",
        maximum_daily_loss=maximum_daily_loss,
    )
    binding = admission.verify_pilot_binding(db, pilot.id)
    return {
        **case,
        "holdout_evaluation": holdout,
        "holdout_result": completed,
        "pilot": pilot,
        "binding": binding,
        "started_at": started_at,
        "first_session": date.fromisoformat(expected[0].isoformat()),
    }
