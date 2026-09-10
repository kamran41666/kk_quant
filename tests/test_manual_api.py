"""M6 route contract tests for the loopback-only manual execution API."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from server.config import settings
from server.main import app
from server.models.database import Base, get_db


def test_manual_api_is_operator_protected_and_idempotent():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    previous_token = settings.operator_token
    settings.operator_token = "m6-test-token"
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            blocked = client.get("/api/v1/manual-trading/accounts")
            assert blocked.status_code == 401

            headers = {"X-Operator-Token": settings.operator_token, "Idempotency-Key": "account-create-001"}
            created = client.post(
                "/api/v1/manual-trading/accounts",
                headers=headers,
                json={"name": "M6 人工账户", "broker_label": "local"},
            )
            assert created.status_code == 201
            body = created.json()
            assert body["manual_execution"] is True
            assert body["broker_connected"] is False
            assert body["live_order_submission"] is False
            account_id = body["data"]["id"]

            repeated = client.post(
                "/api/v1/manual-trading/accounts",
                headers=headers,
                json={"name": "M6 人工账户", "broker_label": "local"},
            )
            assert repeated.status_code == 201
            assert repeated.json()["data"]["id"] == account_id

            cash = client.post(
                f"/api/v1/manual-trading/accounts/{account_id}/cash-events",
                headers={**headers, "Idempotency-Key": "cash-event-001"},
                json={
                    "event_type": "opening_balance",
                    "amount": "100000",
                    "occurred_at": "2024-01-02T08:00:00+08:00",
                },
            )
            assert cash.status_code == 201
            state = client.get(f"/api/v1/manual-trading/accounts/{account_id}/state", headers=headers)
            assert state.status_code == 200
            assert state.json()["data"]["cash"] == "100000.00000000"

            bypass = client.post(
                f"/api/v1/manual-trading/accounts/{account_id}/execution-events",
                headers=headers,
                json={
                    "client_event_id": "unplanned-fill-001", "event_type": "fill",
                    "code": "600000.SH", "side": "buy", "quantity": "100", "price": "10",
                    "traded_at": "2024-01-03T09:30:00+08:00",
                },
            )
            assert bypass.status_code == 409
            assert bypass.json()["detail"]["code"] == "economic_execution_requires_confirmed_plan_item"
            unchanged = client.get(f"/api/v1/manual-trading/accounts/{account_id}/state", headers=headers).json()["data"]
            assert unchanged["cash"] == "100000.00000000"
            assert unchanged["positions"] == {}

            routes = set(app.openapi()["paths"])
            assert "/api/v1/manual-trading/accounts/{account_id}/execution-events" in routes
            assert not any("submit" in route for route in routes if "manual-trading" in route)
    finally:
        app.dependency_overrides.pop(get_db, None)
        settings.operator_token = previous_token
        Base.metadata.drop_all(engine)
