from quant_engine.trading.gateway import (
    OrderIntent,
    OrderIntentStatus,
    PaperBrokerGateway,
    RiskEngine,
    RiskLimits,
)


def test_paper_gateway_is_idempotent_and_updates_snapshot():
    gateway = PaperBrokerGateway(
        100_000,
        risk=RiskEngine(RiskLimits(max_order_notional=20_000, max_position_weight=1.0)),
    )
    gateway.set_price("000001.SZ", 10)
    intent = OrderIntent("rebalance-1", "000001.SZ", "buy", 1_000)

    first = gateway.submit(intent)
    second = gateway.submit(intent)

    assert first.status is OrderIntentStatus.FILLED
    assert second == first
    assert gateway.snapshot().positions["000001.SZ"] == 1_000
    assert gateway.snapshot().cash == 90_000


def test_risk_and_account_guards_reject_without_mutating_state():
    gateway = PaperBrokerGateway(10_000, risk=RiskEngine(RiskLimits(max_order_notional=10_000, max_position_weight=1.0)))
    gateway.set_price("000001.SZ", 10)
    too_large = OrderIntent("too-large", "000001.SZ", "buy", 2_000)
    report = gateway.submit(too_large)
    assert report.status is OrderIntentStatus.REJECTED
    assert report.reason == "max_order_notional_exceeded"
    assert gateway.snapshot().cash == 10_000

    gateway = PaperBrokerGateway(10_000, risk=RiskEngine(RiskLimits(max_order_notional=20_000, max_position_weight=1.0)))
    gateway.set_price("000001.SZ", 10)
    report = gateway.submit(OrderIntent("sell-empty", "000001.SZ", "sell", 100))
    assert report.status is OrderIntentStatus.REJECTED
    assert report.reason == "insufficient_position"
