import pytest
from fastapi import HTTPException

from server.api import sandbox as sandbox_api
from server.api.sandbox import CreateSandboxSessionRequest, SandboxFaultRequest, SandboxOrderRequest
from server.services import sandbox_execution
from quant_engine.trading.sandbox import SandboxInjectedFailure


@pytest.fixture(autouse=True)
def clean_sandbox_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_execution, "_SESSION_DIR", tmp_path / "sandbox_sessions")
    sandbox_execution.clear_sessions(purge=True)
    yield
    sandbox_execution.clear_sessions(purge=True)


def test_service_flow_matches_api_contract_and_restart_keeps_cursor():
    session = sandbox_execution.create_session(initial_cash=10_000, initial_fill_ratio=0)
    session_id = session["id"]
    sandbox_execution.set_price(session_id, code="000001.SZ", price=10)
    accepted = sandbox_execution.submit_order(
        session_id, intent_id="service-order-001", code="000001.SZ",
        side="buy", quantity=100,
    )
    assert accepted["status"] == "accepted"
    assert accepted["event_cursor"] == "1"
    assert sandbox_execution.list_orders(session_id)[0]["status"] == "accepted"
    sandbox_execution.clear_sessions()
    recovered = sandbox_execution.get_session(session_id)
    assert recovered["snapshot"]["cash"] == pytest.approx(10_000)
    assert recovered["event_cursor"] == 1
    restored = sandbox_execution.restart_session(session_id)
    assert restored["event_cursor"] == 1
    assert sandbox_execution.list_events(session_id, since="0")[0]["intent_id"] == "service-order-001"


def test_api_order_model_forbids_non_lot_orders_and_extra_fields():
    with pytest.raises(Exception):
        SandboxOrderRequest.model_validate({
            "intent_id": "api-order-001", "code": "000001.SZ",
            "side": "buy", "quantity": 101,
        })
    with pytest.raises(Exception):
        SandboxOrderRequest.model_validate({
            "intent_id": "api-order-002", "code": "000001.SZ",
            "side": "buy", "quantity": 100, "secret": "never",
        })
    with pytest.raises(Exception):
        SandboxOrderRequest.model_validate({
            "intent_id": "bad id!", "code": "000001.SZ",
            "side": "buy", "quantity": 100,
        })


def test_persistence_failure_does_not_publish_or_diverge_session(monkeypatch):
    original_persist = sandbox_execution._persist

    def fail_persist(*args, **kwargs):
        raise sandbox_execution.SandboxPersistenceError("disk-failure")

    monkeypatch.setattr(sandbox_execution, "_persist", fail_persist)
    with pytest.raises(sandbox_execution.SandboxPersistenceError):
        sandbox_execution.create_session(initial_cash=10_000)
    assert sandbox_execution.list_sessions() == []

    monkeypatch.setattr(sandbox_execution, "_persist", original_persist)
    session = sandbox_execution.create_session(initial_cash=10_000)
    session_id = session["id"]
    monkeypatch.setattr(sandbox_execution, "_persist", fail_persist)
    with pytest.raises(sandbox_execution.SandboxPersistenceError):
        sandbox_execution.set_price(session_id, code="000001.SZ", price=10)

    # The failed mutation is rolled back in memory and the old checkpoint is
    # still authoritative after clearing the process-local registry.
    monkeypatch.setattr(sandbox_execution, "_persist", original_persist)
    sandbox_execution.clear_sessions()
    rejected = sandbox_execution.submit_order(
        session_id, intent_id="rollback-order-001", code="000001.SZ",
        side="buy", quantity=100,
    )
    assert rejected["status"] == "rejected"
    assert rejected["reason"] == "price_unavailable"


def test_corrupt_checkpoint_is_not_exposed_as_internal_error():
    session = sandbox_execution.create_session(initial_cash=10_000)
    path = sandbox_execution._session_path(session["id"])
    path.write_text('{"cash":10000,"positions":[],"prices":{}}', encoding="utf-8")
    sandbox_execution.clear_sessions()
    with pytest.raises(sandbox_execution.SandboxSessionCorruptedError,
                       match="sandbox session is corrupted"):
        sandbox_execution.get_session(session["id"])

    for route in (sandbox_api.get_sandbox_orders, sandbox_api.get_sandbox_events):
        with pytest.raises(HTTPException) as raised:
            route(session["id"])
        assert raised.value.status_code == 503
        assert raised.value.detail == "sandbox_persistence_unavailable"


def test_api_maps_persistence_failure_to_service_unavailable(monkeypatch):
    def fail_create(**kwargs):
        raise sandbox_execution.SandboxPersistenceError("disk-failure")

    monkeypatch.setattr(sandbox_api, "create_session", fail_create)
    with pytest.raises(HTTPException) as raised:
        sandbox_api.create_sandbox_session(CreateSandboxSessionRequest(initial_cash=10_000))
    assert raised.value.status_code == 503
    assert raised.value.detail == "sandbox_persistence_unavailable"


def test_session_directory_failure_is_mapped_to_persistence_error(monkeypatch):
    path_type = type(sandbox_execution._SESSION_DIR)
    original_mkdir = path_type.mkdir

    def fail_mkdir(path, *args, **kwargs):
        if path == sandbox_execution._SESSION_DIR:
            raise OSError("directory-unwritable")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(path_type, "mkdir", fail_mkdir)
    with pytest.raises(sandbox_execution.SandboxPersistenceError):
        sandbox_execution.create_session(initial_cash=10_000)


def test_fault_injection_is_persisted_and_restored_across_process_cache_reset():
    session = sandbox_execution.create_session(initial_cash=10_000)
    session_id = session["id"]
    updated = sandbox_execution.set_fault(session_id, operation="submit", active=True)
    assert updated["faults"] == {"submit": True}
    sandbox_execution.clear_sessions()
    recovered = sandbox_execution.get_session(session_id)
    assert recovered["faults"] == {"submit": True}
    with pytest.raises(SandboxInjectedFailure):
        sandbox_execution.submit_order(
            session_id, intent_id="service-fault-001", code="000001.SZ",
            side="buy", quantity=100,
        )
    assert sandbox_execution.get_session(session_id)["snapshot"]["cash"] == pytest.approx(10_000)


def test_fault_persistence_failure_rolls_back_and_api_maps_injected_failure(monkeypatch):
    session = sandbox_execution.create_session(initial_cash=10_000)
    session_id = session["id"]
    original_persist = sandbox_execution._persist

    def fail_persist(*args, **kwargs):
        raise sandbox_execution.SandboxPersistenceError("disk-failure")

    monkeypatch.setattr(sandbox_execution, "_persist", fail_persist)
    with pytest.raises(sandbox_execution.SandboxPersistenceError):
        sandbox_execution.set_fault(session_id, operation="submit", active=True)
    monkeypatch.setattr(sandbox_execution, "_persist", original_persist)
    assert sandbox_execution.get_session(session_id)["faults"] == {}

    def fail_submit(*args, **kwargs):
        raise SandboxInjectedFailure("secret-provider-detail-must-not-leak")

    monkeypatch.setattr(sandbox_api, "submit_order", fail_submit)
    with pytest.raises(HTTPException) as raised:
        sandbox_api.submit_sandbox_order(
            session_id,
            SandboxOrderRequest(intent_id="api-fault-001", code="000001.SZ", side="buy", quantity=100),
        )
    assert raised.value.status_code == 503
    assert raised.value.detail == "sandbox_fault_injected"


def test_fault_api_model_rejects_unknown_operation_and_accepts_toggle():
    with pytest.raises(Exception):
        SandboxFaultRequest.model_validate({"operation": "network", "active": True})
    request = SandboxFaultRequest.model_validate({"operation": "reconcile", "active": False})
    assert request.operation == "reconcile"
    assert request.active is False


@pytest.mark.parametrize(
    ("route_name", "args", "monkeypatch_name"),
    [
        ("advance_sandbox_orders", (), "advance"),
        ("get_sandbox_events", (), "list_events"),
    ],
)
def test_fault_api_maps_provider_failure_to_fixed_503(monkeypatch, route_name, args, monkeypatch_name):
    session = sandbox_execution.create_session(initial_cash=10_000)
    session_id = session["id"]

    def fail(*_args, **_kwargs):
        raise SandboxInjectedFailure("provider internals must not leak")

    monkeypatch.setattr(sandbox_api, monkeypatch_name, fail)
    route = getattr(sandbox_api, route_name)
    with pytest.raises(HTTPException) as raised:
        route(session_id, *args)
    assert raised.value.status_code == 503
    assert raised.value.detail == "sandbox_fault_injected"
