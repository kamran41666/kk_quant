"""H2d start-paper API tests against the complete H2c parent fixture."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from server.config import settings
from server.main import app
from server.models.database import Base, get_db
from server.models.schema import ManualExecutionAuthorization, ManualExecutionEvent, ManualPilotMarketReceipt, ManualProspectivePilot, StrategyRelease
from server.services import manual_pilot_admission
from server.services import manual_pilot_receipts
from tests.manual_holdout_evaluation_test_support import build_holdout_case


def _override(factory):
    def provide():
        session = factory()
        try:
            yield session
        finally:
            session.close()
    return provide


def _complete_h2c_parent(client, case, token):
    headers = {"X-Operator-Token": token}
    freeze_headers = {**headers, "Idempotency-Key": case["freeze_kwargs"]["idempotency_key"]}
    frozen = client.post(
        f"/api/v1/manual-trading/holdouts/{case['binding'].id}/evaluations",
        headers=freeze_headers,
        json={
            "actor": case["freeze_kwargs"]["actor"],
            "dataset_manifest_path": case["freeze_kwargs"]["dataset_manifest_path"],
            "benchmark_receipt_path": case["freeze_kwargs"]["benchmark_receipt_path"],
        },
    )
    assert frozen.status_code == 201, frozen.text
    evaluation_id = frozen.json()["data"]["id"]
    run = client.post(
        f"/api/v1/manual-trading/holdout-evaluations/{evaluation_id}/run",
        headers=headers,
        json={"actor": "holdout-fixture"},
    )
    assert run.status_code == 200, run.text
    artifact_id = run.json()["data"]["result_artifact_id"]
    promoted = client.post(
        f"/api/v1/manual-trading/releases/{case['release'].id}/promotions",
        headers={**headers, "Idempotency-Key": "h2d-parent-promotion"},
        json={
            "target_status": "holdout_passed",
            "evidence_refs": {"holdout_artifact_id": artifact_id},
            "actor": "holdout-fixture",
        },
    )
    assert promoted.status_code == 201, promoted.text
    assert promoted.json()["data"]["decision"] == "passed"


def _patch_admission_clock_and_calendar(monkeypatch):
    """Use a fully verified future calendar without adding an API test clock."""
    start_moment = datetime(2027, 4, 12, 0, 0, tzinfo=timezone.utc)
    first_day = date(2027, 4, 13)
    dates = []
    cursor = first_day
    while len(dates) < 30:
        if cursor.weekday() < 5:
            dates.append(cursor)
        cursor += timedelta(days=1)
    date_strings = [item.isoformat() for item in dates]
    report = {
        "complete": True,
        "verified": True,
        "source": "akshare:test-synthetic-verified-calendar",
        "content_hash": "c" * 64,
        "coverage_start": "2027-04-12",
        "coverage_end": date_strings[-1],
        "trading_days": date_strings,
    }
    monkeypatch.setattr(manual_pilot_admission, "_utc_now", lambda: start_moment)
    monkeypatch.setattr(manual_pilot_admission, "_calendar_snapshot", lambda _: {
        "report": report,
        "dates": date_strings,
        "source": report["source"],
        "content_hash": report["content_hash"],
        "coverage_start": report["coverage_start"],
        "coverage_end": report["coverage_end"],
    })


def test_start_paper_binds_real_h2c_parent_and_strict_readback(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    case = build_holdout_case(db, tmp_path, monkeypatch, profitable=True)
    token = "h2d-admission-api-token"
    previous_token = settings.operator_token
    settings.operator_token = token
    app.dependency_overrides[get_db] = _override(factory)
    headers = {"X-Operator-Token": token}
    try:
        with TestClient(app) as client:
            _complete_h2c_parent(client, case, token)
            _patch_admission_clock_and_calendar(monkeypatch)
            start_headers = {**headers, "Idempotency-Key": "h2d-start-paper-1"}
            started = client.post(
                f"/api/v1/manual-trading/releases/{case['release'].id}/start-paper",
                headers=start_headers,
                json={"actor": "operator", "maximum_daily_loss": "0.05"},
            )
            assert started.status_code == 201, started.text
            body = started.json()["data"]
            assert {"pilot_id", "binding_id", "calendar", "policy"} <= set(body)
            assert body["status"] == "observing"
            assert body["data_mode"] == "real_forward"
            assert body["policy"]["maximum_daily_loss"] == "0.05"
            assert body["start_date"] == "2027-04-12"
            assert body["expected_dates"][0] == "2027-04-13"
            assert len(body["expected_dates"]) == 30, body
            assert "manifest_path" not in started.text

            evidence = client.get(
                f"/api/v1/manual-trading/pilots/{body['pilot_id']}/evidence",
                headers=headers,
            )
            assert evidence.status_code == 200, evidence.text
            assert evidence.json()["data"]["binding_id"] == body["binding_id"]
            assert "source_path" not in evidence.text
            assert "absolute_path" not in evidence.text

            repeated = client.post(
                f"/api/v1/manual-trading/releases/{case['release'].id}/start-paper",
                headers=start_headers,
                json={"actor": "operator", "maximum_daily_loss": "0.05"},
            )
            assert repeated.status_code == 201, repeated.text
            assert repeated.json()["data"]["pilot_id"] == body["pilot_id"]

            conflict = client.post(
                f"/api/v1/manual-trading/releases/{case['release'].id}/start-paper",
                headers={**headers, "Idempotency-Key": "h2d-start-paper-2"},
                json={"actor": "operator", "maximum_daily_loss": "0.04"},
            )
            assert conflict.status_code == 409, conflict.text

            forbidden = client.post(
                f"/api/v1/manual-trading/releases/{case['release'].id}/start-paper",
                headers={**headers, "Idempotency-Key": "h2d-start-paper-x"},
                json={"actor": "operator", "maximum_daily_loss": "0.05", "start_date": "2026-01-01"},
            )
            assert forbidden.status_code == 422, forbidden.text
            missing_key = client.post(
                f"/api/v1/manual-trading/releases/{case['release'].id}/start-paper",
                headers=headers,
                json={"actor": "operator", "maximum_daily_loss": "0.05"},
            )
            assert missing_key.status_code == 422, missing_key.text
            unauthenticated = client.post(
                f"/api/v1/manual-trading/releases/{case['release'].id}/start-paper",
                headers={"Idempotency-Key": "h2d-start-paper-u"},
                json={"actor": "operator", "maximum_daily_loss": "0.05"},
            )
            assert unauthenticated.status_code == 401, unauthenticated.text
    finally:
        app.dependency_overrides.pop(get_db, None)
        settings.operator_token = previous_token
        db.close()
        Base.metadata.drop_all(engine)


def test_market_receipt_http_capture_readback_idempotency_and_strict_boundary(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    case = build_holdout_case(db, tmp_path, monkeypatch, profitable=True)
    token = "h2d-receipt-api-token"
    previous_token = settings.operator_token
    settings.operator_token = token
    app.dependency_overrides[get_db] = _override(factory)
    fetch_calls: list[tuple[list[str], date]] = []
    try:
            with TestClient(app) as client:
                _complete_h2c_parent(client, case, token)
            # The fixture's holdout read scope ends before this controlled
            # future start.  Keep the API contract free of a test clock while
            # making the server-owned clock deterministic in this test.
            _patch_admission_clock_and_calendar(monkeypatch)
            headers = {"X-Operator-Token": token, "Idempotency-Key": "h2d-receipt-start"}
            started = client.post(
                f"/api/v1/manual-trading/releases/{case['release'].id}/start-paper",
                headers=headers,
                json={"actor": "operator", "maximum_daily_loss": "0.05"},
            )
            assert started.status_code == 201, started.text
            pilot_id = started.json()["data"]["pilot_id"]
            expected_day = date.fromisoformat(started.json()["data"]["expected_dates"][0])
            assert expected_day == date(2027, 4, 13)
            fixed_now = datetime.combine(
                date(2027, 4, 13), time(16, 40), tzinfo=ZoneInfo("Asia/Shanghai"),
            ).astimezone(timezone.utc)

            def fake_now():
                return fixed_now

            def fake_fetch(codes, session_date):
                fetch_calls.append((list(codes), session_date))
                rows = []
                for code in codes:
                    rows.append({
                        "date": session_date.isoformat(), "code": code,
                        "open": "10", "high": "10.5", "low": "9.5", "close": "10.2",
                        "preclose": "10", "volume": "1000000", "amount": "10200000",
                        "turn": "0.1", "tradestatus": "1", "pctChg": "2", "isST": "0",
                    })
                return {"fields": list(manual_pilot_receipts.FIELDS), "rows": rows}

            monkeypatch.setattr(manual_pilot_receipts, "_utc_now", fake_now)
            monkeypatch.setattr(manual_pilot_receipts, "_fetch_provider_rows", fake_fetch)
            receipt_headers = {"X-Operator-Token": token, "Idempotency-Key": "h2d-market-receipt-1"}
            captured = client.post(
                f"/api/v1/manual-trading/pilots/{pilot_id}/market-receipts",
                headers=receipt_headers,
                json={"actor": "operator"},
            )
            assert captured.status_code == 201, captured.text
            receipt_data = captured.json()["data"]
            assert receipt_data["pilot_id"] == pilot_id
            assert "normalized_rows" not in captured.text
            assert "manifest_path" not in captured.text
            assert str(settings.result_dir) not in captured.text
            assert len(fetch_calls) == 1

            readback = client.get(
                f"/api/v1/manual-trading/pilot-market-receipts/{receipt_data['id']}",
                headers={"X-Operator-Token": token},
            )
            assert readback.status_code == 200, readback.text
            verified = readback.json()["data"]
            assert verified["verified"] is True
            assert verified["id"] == receipt_data["id"]
            assert "normalized_rows" not in readback.text
            assert "rows" not in verified
            assert "manifest_path" not in readback.text
            assert str(settings.result_dir) not in readback.text

            replay = client.post(
                f"/api/v1/manual-trading/pilots/{pilot_id}/market-receipts",
                headers=receipt_headers,
                json={"actor": "operator"},
            )
            assert replay.status_code == 201, replay.text
            assert replay.json()["data"]["id"] == receipt_data["id"]
            assert len(fetch_calls) == 1

            db.expire_all()
            pilot = db.get(ManualProspectivePilot, pilot_id)
            assert pilot is not None and pilot.status == "observing"
            release = db.get(StrategyRelease, case["release"].id)
            assert release is not None and release.status == "paper_observing"
            assert pilot.observation_days == 0
            assert db.query(ManualPilotMarketReceipt).count() == 1
            assert db.query(ManualExecutionAuthorization).count() == 0
            assert db.query(ManualExecutionEvent).count() == 0

            for field in ("date", "provider", "codes", "rows", "time"):
                rejected = client.post(
                    f"/api/v1/manual-trading/pilots/{pilot_id}/market-receipts",
                    headers={"X-Operator-Token": token, "Idempotency-Key": f"h2d-forbid-{field}"},
                    json={"actor": "operator", field: "forbidden"},
                )
                assert rejected.status_code == 422, (field, rejected.text)
            assert len(fetch_calls) == 1

            unauthenticated = client.post(
                f"/api/v1/manual-trading/pilots/{pilot_id}/market-receipts",
                headers={"Idempotency-Key": "h2d-receipt-no-auth"},
                json={"actor": "operator"},
            )
            assert unauthenticated.status_code == 401, unauthenticated.text
            assert len(fetch_calls) == 1
    finally:
        app.dependency_overrides.pop(get_db, None)
        settings.operator_token = previous_token
        db.close()
        Base.metadata.drop_all(engine)
