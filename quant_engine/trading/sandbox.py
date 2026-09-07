"""Deterministic, offline sandbox adapter for the Phase 3B contract.

The adapter is deliberately broker-neutral.  It implements the same
``LiveBrokerGateway`` lifecycle as a future official sandbox adapter, but it
never opens a socket, reads credentials, or submits an order outside the
process.  It is therefore suitable for UI demonstrations and contract tests,
not for price discovery or broker certification.
"""
from __future__ import annotations

import copy
import math
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .gateway import (
    AccountSnapshot,
    BrokerCapabilities,
    ExecutionReport,
    LiveBrokerGateway,
    OrderIntent,
    OrderIntentStatus,
)


class SandboxInjectedFailure(RuntimeError):
    """Deterministic provider outage used only by the local sandbox."""


_FAULT_OPERATIONS = frozenset({"submit", "advance", "reconcile"})


@dataclass
class _SandboxOrder:
    intent: OrderIntent
    broker_order_id: str
    status: OrderIntentStatus
    filled_quantity: int = 0
    fill_price: Optional[float] = None
    reason: Optional[str] = None
    event_cursor: Optional[str] = None
    received_at: Optional[str] = None


class SandboxBrokerGateway(LiveBrokerGateway):
    """An in-process gateway with deterministic fills and replay cursors.

    ``initial_fill_ratio`` controls the first match.  ``0`` leaves an order
    accepted until :meth:`advance`; a value between 0 and 1 creates a partial
    fill, and ``1`` fills the whole order immediately.  The default is fully
    filled so a beginner can exercise the happy path without a matching
    engine.  Account state is intentionally process-local; callers that need
    restart tests can use :meth:`checkpoint` and :meth:`from_checkpoint`.
    """

    def __init__(self, initial_cash: float, *, initial_fill_ratio: float = 1.0):
        if not math.isfinite(initial_cash) or initial_cash < 0:
            raise ValueError("initial_cash must be non-negative")
        if not math.isfinite(initial_fill_ratio) or not 0 <= initial_fill_ratio <= 1:
            raise ValueError("initial_fill_ratio must be between 0 and 1")
        self._cash = float(initial_cash)
        self._initial_fill_ratio = float(initial_fill_ratio)
        self._positions: dict[str, int] = {}
        self._prices: dict[str, float] = {}
        self._orders: dict[str, _SandboxOrder] = {}
        self._events: list[ExecutionReport] = []
        self._cursor = 0
        self._faults: dict[str, bool] = {}
        self._lock = threading.RLock()

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            provider="local-sandbox",
            display_name="本地确定性沙盒",
            supports_sandbox=True,
            supports_live=False,
            supports_cancel=True,
            supports_order_stream=True,
        )

    def set_price(self, code: str, price: float) -> None:
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code) or not math.isfinite(price) or price <= 0:
            raise ValueError("code must be a canonical A-share code and price must be finite and positive")
        with self._lock:
            self._prices[code] = float(price)

    def set_fault(self, operation: str, active: bool) -> None:
        if not isinstance(operation, str) or operation not in _FAULT_OPERATIONS:
            raise ValueError("unsupported sandbox fault operation")
        with self._lock:
            if active:
                self._faults[operation] = True
            else:
                self._faults.pop(operation, None)

    def submit(self, intent: OrderIntent) -> ExecutionReport:
        with self._lock:
            existing = self._orders.get(intent.intent_id)
            if existing is not None:
                if not self._same_intent(existing.intent, intent):
                    raise ValueError("intent_id_conflict")
                return self._report(existing)
            if self._faults.get("submit"):
                raise SandboxInjectedFailure("sandbox_submit_injected_failure")
            if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", intent.code):
                raise ValueError("code must be a canonical A-share code")
            if intent.quantity % 100:
                raise ValueError("sandbox A-share orders must use 100-share lots")
            if intent.limit_price is not None and (not math.isfinite(intent.limit_price) or intent.limit_price <= 0):
                raise ValueError("limit_price must be finite and positive")
            broker_order_id = f"sandbox-{intent.intent_id}"
            order = _SandboxOrder(intent=intent, broker_order_id=broker_order_id,
                                  status=OrderIntentStatus.ACCEPTED)
            self._orders[intent.intent_id] = order
            price = intent.limit_price or self._prices.get(intent.code)
            if price is None or not math.isfinite(price) or price <= 0:
                order.status = OrderIntentStatus.REJECTED
                order.reason = "price_unavailable"
                return self._record(order)
            if intent.side == "buy" and intent.quantity * price > self._cash + 1e-9:
                order.status = OrderIntentStatus.REJECTED
                order.reason = "insufficient_cash"
                return self._record(order)
            if intent.side == "sell" and intent.quantity > self._positions.get(intent.code, 0):
                order.status = OrderIntentStatus.REJECTED
                order.reason = "insufficient_position"
                return self._record(order)
            first_quantity = int(intent.quantity * self._initial_fill_ratio)
            if self._initial_fill_ratio > 0:
                first_quantity = max(1, first_quantity)
            if first_quantity:
                self._fill(order, min(first_quantity, intent.quantity), price)
            if order.filled_quantity == intent.quantity:
                order.status = OrderIntentStatus.FILLED
            elif order.filled_quantity > 0:
                order.status = OrderIntentStatus.PARTIALLY_FILLED
            else:
                order.status = OrderIntentStatus.ACCEPTED
            return self._record(order)

    def advance(self) -> tuple[ExecutionReport, ...]:
        """Match all currently open orders at their latest known price."""
        with self._lock:
            if self._faults.get("advance"):
                raise SandboxInjectedFailure("sandbox_advance_injected_failure")
            reports: list[ExecutionReport] = []
            for order in tuple(self._orders.values()):
                if order.status not in {OrderIntentStatus.ACCEPTED, OrderIntentStatus.PARTIALLY_FILLED}:
                    continue
                price = self._prices.get(order.intent.code) or order.fill_price
                if price is None:
                    continue
                remaining = order.intent.quantity - order.filled_quantity
                if remaining <= 0:
                    continue
                affordable = remaining
                if order.intent.side == "buy":
                    affordable = min(affordable, int((self._cash + 1e-9) / price))
                else:
                    affordable = min(affordable, self._positions.get(order.intent.code, 0))
                if affordable <= 0:
                    continue
                self._fill(order, affordable, price)
                order.status = (OrderIntentStatus.FILLED
                                if order.filled_quantity == order.intent.quantity
                                else OrderIntentStatus.PARTIALLY_FILLED)
                reports.append(self._record(order))
            return tuple(reports)

    def cancel(self, broker_order_id: str) -> ExecutionReport:
        with self._lock:
            order = self._find_by_broker_id(broker_order_id)
            if order.status in {OrderIntentStatus.FILLED, OrderIntentStatus.REJECTED,
                                 OrderIntentStatus.CANCELLED}:
                return self._report(order)
            order.status = OrderIntentStatus.CANCELLED
            order.reason = "cancelled_by_user"
            return self._record(order)

    def get_order(self, broker_order_id: str) -> ExecutionReport:
        with self._lock:
            return self._report(self._find_by_broker_id(broker_order_id))

    def open_orders(self) -> tuple[ExecutionReport, ...]:
        with self._lock:
            return tuple(self._report(order) for order in self._orders.values()
                         if order.status in {OrderIntentStatus.ACCEPTED,
                                             OrderIntentStatus.PARTIALLY_FILLED})

    def reconcile(self, since: Optional[str] = None) -> tuple[ExecutionReport, ...]:
        with self._lock:
            if self._faults.get("reconcile"):
                raise SandboxInjectedFailure("sandbox_reconcile_injected_failure")
            if since in (None, ""):
                cursor = 0
            else:
                try:
                    cursor = int(since)
                except (TypeError, ValueError) as exc:
                    raise ValueError("since must be a numeric event cursor") from exc
                if cursor < 0:
                    raise ValueError("since must be non-negative")
            return tuple(copy.deepcopy(event) for event in self._events
                         if int(event.event_cursor or 0) > cursor)

    def snapshot(self) -> AccountSnapshot:
        with self._lock:
            market_value = sum(quantity * self._prices.get(code, 0.0)
                               for code, quantity in self._positions.items())
            return AccountSnapshot(self._cash, market_value,
                                   self._cash + market_value,
                                   dict(self._positions))

    def checkpoint(self) -> dict[str, Any]:
        """Return a JSON-safe state image for restart/recovery tests."""
        with self._lock:
            return {
                "cash": self._cash,
                "initial_fill_ratio": self._initial_fill_ratio,
                "positions": dict(self._positions),
                "prices": dict(self._prices),
                "orders": [
                    {
                        "intent": {
                            "intent_id": order.intent.intent_id,
                            "code": order.intent.code,
                            "side": order.intent.side,
                            "quantity": order.intent.quantity,
                            "limit_price": order.intent.limit_price,
                            "created_at": order.intent.created_at,
                        },
                        "broker_order_id": order.broker_order_id,
                        "status": order.status.value,
                        "filled_quantity": order.filled_quantity,
                        "fill_price": order.fill_price,
                        "reason": order.reason,
                        "event_cursor": order.event_cursor,
                        "received_at": order.received_at,
                    }
                    for order in self._orders.values()
                ],
                "events": [self._report_to_dict(event) for event in self._events],
                "cursor": self._cursor,
                "faults": dict(self._faults),
            }

    @classmethod
    def from_checkpoint(cls, state: dict[str, Any]) -> "SandboxBrokerGateway":
        """Restore a checkpoint without contacting a broker."""
        gateway = cls(float(state["cash"]),
                      initial_fill_ratio=float(state.get("initial_fill_ratio", 1.0)))
        gateway._positions = {str(k): int(v) for k, v in state.get("positions", {}).items()}
        gateway._prices = {str(k): float(v) for k, v in state.get("prices", {}).items()}
        for raw in state.get("orders", []):
            raw_intent = raw["intent"]
            intent = OrderIntent(raw_intent["intent_id"], raw_intent["code"],
                                 raw_intent["side"], int(raw_intent["quantity"]),
                                 raw_intent.get("limit_price"), raw_intent.get("created_at", ""))
            order = _SandboxOrder(intent=intent,
                                  broker_order_id=raw["broker_order_id"],
                                  status=OrderIntentStatus(raw["status"]),
                                  filled_quantity=int(raw.get("filled_quantity", 0)),
                                  fill_price=raw.get("fill_price"),
                                  reason=raw.get("reason"),
                                  event_cursor=raw.get("event_cursor"),
                                  received_at=raw.get("received_at"))
            gateway._orders[intent.intent_id] = order
        gateway._events = [gateway._report_from_dict(raw) for raw in state.get("events", [])]
        gateway._cursor = int(state.get("cursor", len(gateway._events)))
        faults = state.get("faults", {})
        if (not isinstance(faults, dict)
                or any(key not in _FAULT_OPERATIONS or not isinstance(value, bool)
                       for key, value in faults.items())):
            raise ValueError("invalid sandbox faults checkpoint")
        gateway._faults = {str(key): value for key, value in faults.items() if value}
        return gateway

    def _fill(self, order: _SandboxOrder, quantity: int, price: float) -> None:
        amount = quantity * price
        previous_quantity = order.filled_quantity
        if order.intent.side == "buy":
            self._cash -= amount
            self._positions[order.intent.code] = self._positions.get(order.intent.code, 0) + quantity
        else:
            self._cash += amount
            self._positions[order.intent.code] = self._positions.get(order.intent.code, 0) - quantity
        order.filled_quantity += quantity
        order.fill_price = ((order.fill_price * previous_quantity + price * quantity)
                            / order.filled_quantity if previous_quantity and order.fill_price is not None
                            else float(price))

    def _record(self, order: _SandboxOrder) -> ExecutionReport:
        self._cursor += 1
        report = self._report(order, event_cursor=str(self._cursor),
                              received_at=datetime.now(timezone.utc).isoformat())
        order.event_cursor = report.event_cursor
        order.received_at = report.received_at
        self._events.append(report)
        return report

    def _report(self, order: _SandboxOrder, *, event_cursor: Optional[str] = None,
                received_at: Optional[str] = None) -> ExecutionReport:
        return ExecutionReport(
            intent_id=order.intent.intent_id,
            status=order.status,
            code=order.intent.code,
            side=order.intent.side,
            requested_quantity=order.intent.quantity,
            filled_quantity=order.filled_quantity,
            fill_price=order.fill_price,
            reason=order.reason,
            received_at=received_at or order.received_at or datetime.now(timezone.utc).isoformat(),
            broker_order_id=order.broker_order_id,
            event_cursor=event_cursor or order.event_cursor,
        )

    def _find_by_broker_id(self, broker_order_id: str) -> _SandboxOrder:
        for order in self._orders.values():
            if order.broker_order_id == broker_order_id:
                return order
        raise KeyError("sandbox order not found")

    @staticmethod
    def _same_intent(left: OrderIntent, right: OrderIntent) -> bool:
        return (left.intent_id == right.intent_id and left.code == right.code
                and left.side == right.side and left.quantity == right.quantity
                and left.limit_price == right.limit_price)

    @staticmethod
    def _report_to_dict(report: ExecutionReport) -> dict[str, Any]:
        return {
            "intent_id": report.intent_id,
            "status": report.status.value,
            "code": report.code,
            "side": report.side,
            "requested_quantity": report.requested_quantity,
            "filled_quantity": report.filled_quantity,
            "fill_price": report.fill_price,
            "reason": report.reason,
            "received_at": report.received_at,
            "broker_order_id": report.broker_order_id,
            "event_cursor": report.event_cursor,
        }

    @staticmethod
    def _report_from_dict(raw: dict[str, Any]) -> ExecutionReport:
        return ExecutionReport(
            intent_id=raw["intent_id"], status=OrderIntentStatus(raw["status"]),
            code=raw["code"], side=raw["side"],
            requested_quantity=int(raw["requested_quantity"]),
            filled_quantity=int(raw.get("filled_quantity", 0)),
            fill_price=raw.get("fill_price"), reason=raw.get("reason"),
            received_at=raw.get("received_at") or datetime.now(timezone.utc).isoformat(),
            broker_order_id=raw.get("broker_order_id"), event_cursor=raw.get("event_cursor"),
        )
