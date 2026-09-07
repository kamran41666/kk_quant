import pytest

from quant_engine.trading.gateway import OrderIntent, OrderIntentStatus
from quant_engine.trading.sandbox import SandboxBrokerGateway, SandboxInjectedFailure


def test_sandbox_partial_fill_advance_and_idempotent_replay():
    gateway = SandboxBrokerGateway(10_000, initial_fill_ratio=0.5)
    gateway.set_price("000001.SZ", 10)
    intent = OrderIntent("sandbox-order-001", "000001.SZ", "buy", 200)

    first = gateway.submit(intent)
    assert first.status is OrderIntentStatus.PARTIALLY_FILLED
    assert first.filled_quantity == 100
    assert first.fill_price == 10
    assert first.broker_order_id == "sandbox-sandbox-order-001"
    assert first.event_cursor == "1"
    assert gateway.submit(intent) == first
    assert gateway.snapshot().cash == pytest.approx(9_000)
    assert gateway.snapshot().positions == {"000001.SZ": 100}

    gateway.set_price("000001.SZ", 12)
    advanced = gateway.advance()
    assert len(advanced) == 1
    assert advanced[0].status is OrderIntentStatus.FILLED
    assert advanced[0].filled_quantity == 200
    assert advanced[0].fill_price == pytest.approx(11)
    assert gateway.open_orders() == ()
    assert [item.event_cursor for item in gateway.reconcile()] == ["1", "2"]
    assert [item.event_cursor for item in gateway.reconcile("1")] == ["2"]


def test_sandbox_cancel_and_reconcile_cursor_validation():
    gateway = SandboxBrokerGateway(10_000, initial_fill_ratio=0)
    gateway.set_price("000001.SZ", 10)
    pending = gateway.submit(OrderIntent("sandbox-order-002", "000001.SZ", "buy", 100))
    assert pending.status is OrderIntentStatus.ACCEPTED
    cancelled = gateway.cancel(pending.broker_order_id)
    assert cancelled.status is OrderIntentStatus.CANCELLED
    assert gateway.cancel(pending.broker_order_id) == cancelled
    assert gateway.open_orders() == ()
    with pytest.raises(ValueError, match="numeric event cursor"):
        gateway.reconcile("bad")
    with pytest.raises(KeyError, match="sandbox order"):
        gateway.get_order("missing")


def test_sandbox_rejects_without_mutating_account_and_restores_checkpoint():
    gateway = SandboxBrokerGateway(1_000)
    rejected = gateway.submit(OrderIntent("sandbox-order-003", "000001.SZ", "buy", 100, 20))
    assert rejected.status is OrderIntentStatus.REJECTED
    assert rejected.reason == "insufficient_cash"
    assert gateway.snapshot().cash == pytest.approx(1_000)
    gateway.set_price("000001.SZ", 5)
    filled = gateway.submit(OrderIntent("sandbox-order-004", "000001.SZ", "buy", 100))
    assert filled.status is OrderIntentStatus.FILLED

    restored = SandboxBrokerGateway.from_checkpoint(gateway.checkpoint())
    assert restored.snapshot() == gateway.snapshot()
    assert restored.submit(OrderIntent("sandbox-order-004", "000001.SZ", "buy", 100)) == filled
    assert restored.reconcile("0") == gateway.reconcile("0")


def test_sandbox_input_contract_is_explicit():
    with pytest.raises(ValueError, match="initial_cash"):
        SandboxBrokerGateway(-1)
    with pytest.raises(ValueError, match="between 0 and 1"):
        SandboxBrokerGateway(1_000, initial_fill_ratio=1.1)
    gateway = SandboxBrokerGateway(1_000)
    with pytest.raises(ValueError, match="canonical A-share"):
        gateway.set_price("AAPL", 10)
    with pytest.raises(ValueError, match="finite"):
        gateway.set_price("000001.SZ", float("nan"))


def test_sandbox_idempotency_conflict_is_explicit():
    gateway = SandboxBrokerGateway(10_000)
    gateway.set_price("000001.SZ", 10)
    gateway.submit(OrderIntent("sandbox-order-005", "000001.SZ", "buy", 100))
    with pytest.raises(ValueError, match="intent_id_conflict"):
        gateway.submit(OrderIntent("sandbox-order-005", "000001.SZ", "buy", 200))


def test_sandbox_fault_injection_is_deterministic_and_checkpointed():
    gateway = SandboxBrokerGateway(10_000, initial_fill_ratio=0)
    gateway.set_price("000001.SZ", 10)
    intent = OrderIntent("sandbox-order-006", "000001.SZ", "buy", 100)
    gateway.set_fault("submit", True)
    with pytest.raises(SandboxInjectedFailure, match="sandbox_submit_injected_failure"):
        gateway.submit(intent)

    gateway.set_fault("submit", False)
    accepted = gateway.submit(intent)
    gateway.set_fault("advance", True)
    with pytest.raises(SandboxInjectedFailure, match="sandbox_advance_injected_failure"):
        gateway.advance()
    gateway.set_fault("advance", False)
    gateway.set_fault("reconcile", True)
    with pytest.raises(SandboxInjectedFailure, match="sandbox_reconcile_injected_failure"):
        gateway.reconcile()

    restored = SandboxBrokerGateway.from_checkpoint(gateway.checkpoint())
    assert restored.checkpoint()["faults"] == {"reconcile": True}
    with pytest.raises(SandboxInjectedFailure):
        restored.reconcile()
    # An idempotent replay remains safe even when submit is currently faulted.
    restored.set_fault("submit", True)
    assert restored.submit(intent).status is OrderIntentStatus.ACCEPTED


def test_sandbox_rejects_unknown_fault_operation_or_checkpoint():
    gateway = SandboxBrokerGateway(10_000)
    with pytest.raises(ValueError, match="unsupported sandbox fault"):
        gateway.set_fault("network", True)
    checkpoint = gateway.checkpoint()
    checkpoint["faults"] = {"network": True}
    with pytest.raises(ValueError, match="invalid sandbox faults"):
        SandboxBrokerGateway.from_checkpoint(checkpoint)
    checkpoint["faults"] = {"submit": "false"}
    with pytest.raises(ValueError, match="invalid sandbox faults"):
        SandboxBrokerGateway.from_checkpoint(checkpoint)
