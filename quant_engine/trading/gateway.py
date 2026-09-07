"""Small, explicit broker boundary for paper-first execution.

The gateway intentionally has no network or broker SDK dependency.  A live
adapter must implement the same contract and be enabled by an explicit
configuration gate in a later phase.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Mapping, Optional


class OrderIntentStatus(str, Enum):
    PENDING = "pending"
    PARTIALLY_FILLED = "partially_filled"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FILLED = "filled"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class OrderIntent:
    """An immutable, idempotent request produced by the portfolio planner."""

    intent_id: str
    code: str
    side: str
    quantity: float
    limit_price: Optional[float] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        if not self.intent_id.strip():
            raise ValueError("intent_id is required")
        if self.side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")


@dataclass(frozen=True)
class ExecutionReport:
    intent_id: str
    status: OrderIntentStatus
    code: str
    side: str
    requested_quantity: float
    filled_quantity: float = 0.0
    fill_price: Optional[float] = None
    reason: Optional[str] = None
    received_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # Broker order identifiers and cursors are optional for PaperBroker but
    # mandatory for a concrete live adapter's durable reconciliation path.
    broker_order_id: Optional[str] = None
    event_cursor: Optional[str] = None


@dataclass(frozen=True)
class AccountSnapshot:
    cash: float
    market_value: float
    equity: float
    positions: Mapping[str, float]


class BrokerGateway(ABC):
    """Common interface for paper and live execution adapters."""

    @abstractmethod
    def submit(self, intent: OrderIntent) -> ExecutionReport:
        ...

    @abstractmethod
    def snapshot(self) -> AccountSnapshot:
        ...


@dataclass(frozen=True)
class BrokerCapabilities:
    """Declarative adapter capabilities exposed before any order is sent."""

    provider: str
    display_name: str
    supports_sandbox: bool = False
    supports_live: bool = False
    supports_cancel: bool = False
    supports_order_stream: bool = False


class LiveBrokerGateway(BrokerGateway):
    """Explicit extension point for a user-selected broker adapter.

    Phase 3A only defines this boundary.  A concrete adapter must provide its
    own credentials, connection health, order lifecycle and reconciliation;
    no fake implementation is registered by default.
    """

    @property
    @abstractmethod
    def capabilities(self) -> BrokerCapabilities:
        ...

    @abstractmethod
    def cancel(self, broker_order_id: str) -> ExecutionReport:
        ...

    @abstractmethod
    def get_order(self, broker_order_id: str) -> ExecutionReport:
        """Return the broker's current authoritative state for one order."""
        ...

    @abstractmethod
    def open_orders(self) -> tuple[ExecutionReport, ...]:
        """List non-terminal broker orders after a restart or reconnect."""
        ...

    @abstractmethod
    def reconcile(self, since: Optional[str] = None) -> tuple[ExecutionReport, ...]:
        """Replay execution events from a durable broker cursor."""
        ...


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: float = 100_000.0
    max_position_weight: float = 0.25
    max_daily_loss: float = 0.03


class RiskEngine:
    """Deterministic pre-trade checks suitable for beginner-facing defaults."""

    def __init__(self, limits: RiskLimits = RiskLimits()):
        if not 0 < limits.max_position_weight <= 1:
            raise ValueError("max_position_weight must be in (0, 1]")
        if limits.max_order_notional <= 0 or limits.max_daily_loss < 0:
            raise ValueError("risk limits must be non-negative and non-zero where required")
        self.limits = limits

    def check(
        self,
        intent: OrderIntent,
        *,
        reference_price: float,
        equity: float,
        current_position_value: float = 0.0,
        daily_return: float = 0.0,
    ) -> Optional[str]:
        if reference_price <= 0:
            return "invalid_reference_price"
        if equity <= 0:
            return "non_positive_equity"
        notional = intent.quantity * reference_price
        if notional > self.limits.max_order_notional + 1e-9:
            return "max_order_notional_exceeded"
        # A loss circuit breaker blocks new risk, while exits remain allowed so
        # a beginner can still reduce exposure after a drawdown.
        if intent.side == "buy" and daily_return <= -self.limits.max_daily_loss:
            return "daily_loss_limit_exceeded"
        if intent.side == "buy":
            resulting = current_position_value + notional
            if resulting / equity > self.limits.max_position_weight + 1e-9:
                return "max_position_weight_exceeded"
        return None


class PaperBrokerGateway(BrokerGateway):
    """A deliberately small paper gateway with idempotency and no side effects."""

    def __init__(self, initial_cash: float, risk: Optional[RiskEngine] = None):
        if initial_cash < 0:
            raise ValueError("initial_cash must be non-negative")
        self._cash = float(initial_cash)
        self._positions: dict[str, float] = {}
        self._prices: dict[str, float] = {}
        self._reports: dict[str, ExecutionReport] = {}
        self._risk = risk or RiskEngine()

    def set_price(self, code: str, price: float) -> None:
        if price <= 0:
            raise ValueError("price must be positive")
        self._prices[code] = float(price)

    def set_position(self, code: str, quantity: float, price: float) -> None:
        """Restore a persisted position before processing the next intent."""
        if quantity < 0 or price <= 0:
            raise ValueError("position quantity must be non-negative and price positive")
        self._positions[code] = float(quantity)
        self._prices[code] = float(price)

    def submit(self, intent: OrderIntent) -> ExecutionReport:
        if intent.intent_id in self._reports:
            return self._reports[intent.intent_id]
        price = intent.limit_price or self._prices.get(intent.code)
        if price is None:
            report = self._reject(intent, "price_unavailable")
        else:
            reason = self._risk.check(
                intent,
                reference_price=price,
                equity=self.snapshot().equity,
                current_position_value=self._positions.get(intent.code, 0) * price,
            )
            notional = intent.quantity * price
            if reason:
                report = self._reject(intent, reason)
            elif intent.side == "buy" and notional > self._cash + 1e-9:
                report = self._reject(intent, "insufficient_cash")
            elif intent.side == "sell" and intent.quantity > self._positions.get(intent.code, 0):
                report = self._reject(intent, "insufficient_position")
            else:
                if intent.side == "buy":
                    self._cash -= notional
                    self._positions[intent.code] = self._positions.get(intent.code, 0) + intent.quantity
                else:
                    self._cash += notional
                    self._positions[intent.code] = self._positions.get(intent.code, 0) - intent.quantity
                report = ExecutionReport(intent.intent_id, OrderIntentStatus.FILLED, intent.code, intent.side, intent.quantity, intent.quantity, price)
        self._reports[intent.intent_id] = report
        return report

    def _reject(self, intent: OrderIntent, reason: str) -> ExecutionReport:
        return ExecutionReport(intent.intent_id, OrderIntentStatus.REJECTED, intent.code, intent.side, intent.quantity, reason=reason)

    def snapshot(self) -> AccountSnapshot:
        market_value = sum(quantity * self._prices.get(code, 0.0) for code, quantity in self._positions.items())
        return AccountSnapshot(self._cash, market_value, self._cash + market_value, dict(self._positions))
