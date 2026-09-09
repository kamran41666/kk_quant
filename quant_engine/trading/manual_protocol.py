"""Immutable protocol objects for A-share manual daily execution.

The objects in this module are deliberately detached from SQLAlchemy, the
web layer, clocks, and broker adapters.  They are the shared vocabulary for
M1's label, rules, and plan calculations.  User-reported fills and ledgers
belong to later phases and are not represented as orders here.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence


MANUAL_EXECUTION_PROTOCOL_VERSION = "manual-daily-v1"
MANUAL_DAILY_LABEL_ID = "manual-daily-label-v1"
MANUAL_DAILY_POLICY_ID = "daily_two_sleeve_open_close_v1"


class DecisionAction(str, Enum):
    HOLD = "hold"
    REBALANCE = "rebalance"
    REDUCE = "reduce"
    FLAT = "flat"
    BLOCKED = "blocked"
    RECONCILE = "reconcile"


class DecisionStatus(str, Enum):
    DRAFT = "draft"
    READY = "ready"
    BLOCKED = "blocked"
    SUPERSEDED = "superseded"
    REVIEWED = "reviewed"


class CohortStatus(str, Enum):
    PLANNED = "planned"
    ENTERING = "entering"
    OPEN = "open"
    EXITING = "exiting"
    CLOSED = "closed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class PlanStatus(str, Enum):
    DRAFT = "draft"
    READY = "ready"
    VIEWED = "viewed"
    PARTIALLY_FILLED = "partially_filled"
    COMPLETED = "completed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    SUPERSEDED = "superseded"


class PlanItemStatus(str, Enum):
    PLANNED = "planned"
    SUBMITTED = "submitted"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    UNFILLED = "unfilled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    BLOCKED = "blocked"


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _decimal(value: Decimal | int | float | str, field: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def _plain(value: Any) -> Any:
    """Convert protocol values into a stable JSON-compatible structure."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        if value == 0:
            return "0"
        return format(value.normalize(), "f")
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value):
        return {item.name: _plain(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _plain(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, set):
        return sorted((_plain(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(_plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _normalize_weights(weights: Mapping[str, Decimal | int | float | str] | None) -> Mapping[str, Decimal]:
    normalized: dict[str, Decimal] = {}
    for raw_code, raw_weight in (weights or {}).items():
        code = str(raw_code).strip().upper()
        if not code:
            raise ValueError("target weight code is required")
        weight = _decimal(raw_weight, f"target_weights[{code}]")
        if weight < 0:
            raise ValueError("target weights cannot be negative")
        if weight:
            normalized[code] = weight
    if sum(normalized.values(), Decimal("0")) > Decimal("1") + Decimal("1e-12"):
        raise ValueError("target weights cannot exceed 1.0 gross exposure")
    return MappingProxyType(dict(sorted(normalized.items())))


@dataclass(frozen=True)
class ManualDailyPolicy:
    """The first immutable daily two-sleeve execution policy."""

    policy_id: str = MANUAL_DAILY_POLICY_ID
    label_id: str = MANUAL_DAILY_LABEL_ID
    market: str = "a-share"
    signal_phase: str = "close"
    entry_offset: int = 1
    entry_phase: str = "open"
    exit_offset: int = 2
    exit_phase: str = "close"
    cohort_count: int = 2
    cohort_gross_exposure: Decimal = Decimal("0.45")
    gross_exposure: Decimal = Decimal("0.90")
    entry_expiry: str = "same_session"
    exit_retry_policy: str = "next_available_close"
    buy_lot_size: int = 100
    allow_odd_lot_exit: bool = True
    auto_submit: bool = False

    def __post_init__(self) -> None:
        if self.policy_id != MANUAL_DAILY_POLICY_ID:
            raise ValueError("unsupported manual daily policy")
        if self.label_id != MANUAL_DAILY_LABEL_ID:
            raise ValueError("manual daily policy must use manual-daily-label-v1")
        if self.market != "a-share":
            raise ValueError("manual daily v1 only supports a-share")
        if (self.signal_phase, self.entry_phase, self.exit_phase) != ("close", "open", "close"):
            raise ValueError("manual daily v1 phases must be close/open/close")
        if self.entry_offset != 1 or self.exit_offset != 2:
            raise ValueError("manual daily v1 offsets must be 1 and 2")
        if self.cohort_count != 2 or self.buy_lot_size != 100:
            raise ValueError("manual daily v1 requires two cohorts and 100-share buy lots")
        cohort_exposure = _decimal(self.cohort_gross_exposure, "cohort_gross_exposure")
        gross_exposure = _decimal(self.gross_exposure, "gross_exposure")
        if cohort_exposure <= 0 or gross_exposure <= 0:
            raise ValueError("exposure must be positive")
        if cohort_exposure * self.cohort_count > gross_exposure + Decimal("1e-12"):
            raise ValueError("cohort exposure exceeds gross exposure")
        if gross_exposure > Decimal("1"):
            raise ValueError("gross exposure cannot exceed 1.0")
        if self.auto_submit:
            raise ValueError("manual daily v1 cannot submit automatically")
        object.__setattr__(self, "cohort_gross_exposure", cohort_exposure)
        object.__setattr__(self, "gross_exposure", gross_exposure)

    def as_dict(self) -> dict[str, Any]:
        return _plain(self)

    @property
    def policy_hash(self) -> str:
        return stable_hash(self.as_dict())


@dataclass(frozen=True)
class ManualPosition:
    code: str
    quantity: int
    available_quantity: Optional[int] = None
    market_value: Decimal = Decimal("0")
    cohort_id: Optional[str] = None
    planned_exit_date: Optional[date] = None

    def __post_init__(self) -> None:
        code = str(self.code).strip().upper()
        if not code:
            raise ValueError("position code is required")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity < 0:
            raise ValueError("position quantity must be a non-negative integer")
        available = self.quantity if self.available_quantity is None else self.available_quantity
        if isinstance(available, bool) or not isinstance(available, int) or available < 0 or available > self.quantity:
            raise ValueError("available_quantity must be between zero and quantity")
        value = _decimal(self.market_value, "market_value")
        if value < 0:
            raise ValueError("market_value must be non-negative")
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "available_quantity", available)
        object.__setattr__(self, "market_value", value)


@dataclass(frozen=True)
class AccountSnapshot:
    confirmed_cash: Decimal | int | float | str
    equity: Optional[Decimal | int | float | str] = None
    positions: Sequence[ManualPosition] = ()
    as_of: Optional[date | str] = None
    snapshot_id: Optional[str] = None

    def __post_init__(self) -> None:
        cash = _decimal(self.confirmed_cash, "confirmed_cash")
        if cash < 0:
            raise ValueError("confirmed_cash must be non-negative")
        raw_positions = self.positions
        if isinstance(raw_positions, Mapping):
            expanded = []
            for code, item in raw_positions.items():
                expanded.append(
                    {"code": code, **dict(item)}
                    if isinstance(item, Mapping)
                    else {"code": code, "quantity": item}
                )
            raw_positions = expanded
        values = tuple(item if isinstance(item, ManualPosition) else ManualPosition(**item) for item in raw_positions)
        values = tuple(sorted(values, key=lambda item: (item.code, item.cohort_id or "")))
        equity = _decimal(self.equity, "equity") if self.equity is not None else cash + sum((item.market_value for item in values), Decimal("0"))
        if equity < 0:
            raise ValueError("equity must be non-negative")
        object.__setattr__(self, "confirmed_cash", cash)
        object.__setattr__(self, "equity", equity)
        object.__setattr__(self, "positions", values)

    @property
    def cash(self) -> Decimal:
        return self.confirmed_cash

    @property
    def positions_by_code(self) -> dict[str, tuple[ManualPosition, ...]]:
        result: dict[str, list[ManualPosition]] = {}
        for position in self.positions:
            result.setdefault(position.code, []).append(position)
        return {code: tuple(values) for code, values in result.items()}

    @property
    def input_hash(self) -> str:
        return stable_hash(self)


@dataclass(frozen=True)
class QuoteSnapshot:
    code: str
    price: Decimal | int | float | str
    source: str = "manual_input"
    as_of: Optional[str] = None
    freshness: str = "fresh"
    suspended: bool = False
    limit_up: bool = False
    limit_down: bool = False
    is_trading_day: bool = True

    def __post_init__(self) -> None:
        code = str(self.code).strip().upper()
        price = _decimal(self.price, "price")
        if not code or price <= 0:
            raise ValueError("quote code and positive price are required")
        if not str(self.source).strip():
            raise ValueError("quote source is required")
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "source", str(self.source).strip())
        object.__setattr__(self, "freshness", str(self.freshness).strip().lower())


@dataclass(frozen=True)
class ManualCohort:
    id: str
    signal_date: date
    planned_entry_date: date
    planned_exit_date: date
    sleeve_index: int
    budget: Decimal | int | float | str
    status: CohortStatus | str = CohortStatus.PLANNED

    def __post_init__(self) -> None:
        if not str(self.id).strip():
            raise ValueError("cohort id is required")
        if self.planned_entry_date <= self.signal_date or self.planned_exit_date <= self.planned_entry_date:
            raise ValueError("cohort dates must be signal < entry < exit")
        if self.sleeve_index not in {0, 1}:
            raise ValueError("sleeve_index must be 0 or 1")
        budget = _decimal(self.budget, "budget")
        if budget <= 0:
            raise ValueError("cohort budget must be positive")
        status = _enum_value(self.status)
        if status not in {item.value for item in CohortStatus}:
            raise ValueError(f"invalid cohort status: {status}")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "budget", budget)


@dataclass(frozen=True)
class DailyDecision:
    signal_date: date
    action: DecisionAction | str
    target_weights: Mapping[str, Decimal | int | float | str] = ()
    risk_state: str = "normal"
    reason_codes: tuple[str, ...] = ()
    status: DecisionStatus | str = DecisionStatus.READY
    release_id: Optional[str] = None
    authorization_id: Optional[str] = None
    data_as_of: Optional[str] = None
    blocked_reason: Optional[str] = None
    revision: int = 0
    input_hash: str = ""
    decision_hash: str = ""
    id: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.signal_date, date):
            raise ValueError("signal_date must be a date")
        action = _enum_value(self.action)
        status = _enum_value(self.status)
        if action not in {item.value for item in DecisionAction}:
            raise ValueError(f"invalid decision action: {action}")
        if status not in {item.value for item in DecisionStatus}:
            raise ValueError(f"invalid decision status: {status}")
        if isinstance(self.revision, bool) or self.revision < 0:
            raise ValueError("revision must be non-negative")
        weights = _normalize_weights(self.target_weights if isinstance(self.target_weights, Mapping) else {})
        reason_codes = tuple(sorted({str(item).strip() for item in self.reason_codes if str(item).strip()}))
        blocked_reason = self.blocked_reason
        if action in {DecisionAction.BLOCKED.value, DecisionAction.RECONCILE.value} and not blocked_reason:
            blocked_reason = reason_codes[0] if reason_codes else f"decision_{action}"
        payload = {
            "signal_date": self.signal_date,
            "action": action,
            "target_weights": weights,
            "risk_state": self.risk_state,
            "reason_codes": reason_codes,
            "status": status,
            "release_id": self.release_id,
            "authorization_id": self.authorization_id,
            "data_as_of": self.data_as_of,
            "blocked_reason": blocked_reason,
            "revision": self.revision,
        }
        input_hash = self.input_hash or stable_hash(payload)
        decision_hash = self.decision_hash or stable_hash({**payload, "input_hash": input_hash})
        decision_id = self.id or f"decision-{decision_hash[:24]}"
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "target_weights", weights)
        object.__setattr__(self, "reason_codes", reason_codes)
        object.__setattr__(self, "blocked_reason", blocked_reason)
        object.__setattr__(self, "input_hash", input_hash)
        object.__setattr__(self, "decision_hash", decision_hash)
        object.__setattr__(self, "id", decision_id)

    def as_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class ExecutionItem:
    code: str
    side: str
    phase: str
    planned_quantity: int
    reference_price: Decimal | int | float | str
    price_source: str
    price_as_of: Optional[str]
    cohort_id: Optional[str] = None
    available_quantity: int = 0
    target_weight: Decimal | int | float | str = Decimal("0")
    cash_required: Decimal | int | float | str = Decimal("0")
    expected_proceeds: Decimal | int | float | str = Decimal("0")
    estimated_commission: Decimal | int | float | str = Decimal("0")
    estimated_tax: Decimal | int | float | str = Decimal("0")
    estimated_other_fee: Decimal | int | float | str = Decimal("0")
    order_sequence: int = 0
    reason_codes: tuple[str, ...] = ()
    status: PlanItemStatus | str = PlanItemStatus.PLANNED
    cash_dependency_type: str = "confirmed_cash"

    def __post_init__(self) -> None:
        code = str(self.code).strip().upper()
        side = str(self.side).strip().lower()
        phase = str(self.phase).strip().lower()
        status = _enum_value(self.status)
        if not code or side not in {"buy", "sell"} or phase not in {"buy", "sell"} or side != phase:
            raise ValueError("execution item code, side and phase are invalid")
        if isinstance(self.planned_quantity, bool) or not isinstance(self.planned_quantity, int) or self.planned_quantity <= 0:
            raise ValueError("planned_quantity must be a positive integer")
        if isinstance(self.available_quantity, bool) or not isinstance(self.available_quantity, int) or self.available_quantity < 0:
            raise ValueError("available_quantity must be a non-negative integer")
        if side == "sell" and self.planned_quantity > self.available_quantity:
            raise ValueError("sell quantity cannot exceed available quantity")
        if status not in {item.value for item in PlanItemStatus}:
            raise ValueError(f"invalid plan item status: {status}")
        price = _decimal(self.reference_price, "reference_price")
        if price <= 0:
            raise ValueError("reference_price must be positive")
        fields_to_decimal = (
            "target_weight", "cash_required", "expected_proceeds",
            "estimated_commission", "estimated_tax", "estimated_other_fee",
        )
        for name in fields_to_decimal:
            value = _decimal(getattr(self, name), name)
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
            object.__setattr__(self, name, value)
        reasons = tuple(sorted({str(item).strip() for item in self.reason_codes if str(item).strip()}))
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reference_price", price)
        object.__setattr__(self, "reason_codes", reasons)

    @property
    def expected_notional(self) -> Decimal:
        return self.reference_price * self.planned_quantity

    @property
    def estimated_fee(self) -> Decimal:
        return self.estimated_commission + self.estimated_tax + self.estimated_other_fee


@dataclass(frozen=True)
class ExecutionPlan:
    decision_id: str
    execution_date: date
    execution_session: str
    plan_type: str
    items: Sequence[ExecutionItem] = ()
    authorization_id: Optional[str] = None
    account_snapshot_id: Optional[str] = None
    version: int = 1
    status: PlanStatus | str = PlanStatus.DRAFT
    cash_before: Decimal | int | float | str = Decimal("0")
    expected_cash_after: Decimal | int | float | str = Decimal("0")
    expected_fees: Decimal | int | float | str = Decimal("0")
    input_hash: str = ""
    quote_snapshot_hash: str = ""
    trading_rule_id: Optional[str] = None
    trading_rule_version: Optional[str] = None
    blocked_reason: Optional[str] = None
    supersedes_plan_id: Optional[str] = None
    id: Optional[str] = None
    plan_hash: str = ""

    def __post_init__(self) -> None:
        session = str(self.execution_session).strip().lower()
        plan_type = str(self.plan_type).strip().lower()
        status = _enum_value(self.status)
        if not isinstance(self.execution_date, date):
            raise ValueError("execution_date must be a date")
        if session not in {"open", "close"}:
            raise ValueError("execution_session must be open or close")
        if plan_type not in {"entry", "exit", "rebalance", "risk", "mixed"}:
            raise ValueError("invalid plan_type")
        if isinstance(self.version, bool) or self.version < 1:
            raise ValueError("version must be positive")
        if status not in {item.value for item in PlanStatus}:
            raise ValueError(f"invalid plan status: {status}")
        items = tuple(self.items)
        if any(not isinstance(item, ExecutionItem) for item in items):
            raise TypeError("items must contain ExecutionItem values")
        items = tuple(sorted(items, key=lambda item: (item.order_sequence, item.side != "sell", item.code, item.cohort_id or "")))
        cash_before = _decimal(self.cash_before, "cash_before")
        expected_cash_after = _decimal(self.expected_cash_after, "expected_cash_after")
        expected_fees = _decimal(self.expected_fees, "expected_fees")
        if cash_before < 0 or expected_cash_after < 0 or expected_fees < 0:
            raise ValueError("cash and fees must be non-negative")
        payload = {
            "decision_id": self.decision_id,
            "execution_date": self.execution_date,
            "execution_session": session,
            "plan_type": plan_type,
            "items": items,
            "authorization_id": self.authorization_id,
            "account_snapshot_id": self.account_snapshot_id,
            "version": self.version,
            "status": status,
            "cash_before": cash_before,
            "expected_cash_after": expected_cash_after,
            "expected_fees": expected_fees,
            "input_hash": self.input_hash,
            "quote_snapshot_hash": self.quote_snapshot_hash,
            "trading_rule_id": self.trading_rule_id,
            "trading_rule_version": self.trading_rule_version,
            "blocked_reason": self.blocked_reason,
            "supersedes_plan_id": self.supersedes_plan_id,
        }
        plan_hash = self.plan_hash or stable_hash(payload)
        plan_key = {
            "decision_id": self.decision_id,
            "execution_date": self.execution_date,
            "execution_session": session,
            "plan_type": plan_type,
            "version": self.version,
        }
        plan_id = self.id or f"plan-{stable_hash(plan_key)[:24]}"
        object.__setattr__(self, "execution_session", session)
        object.__setattr__(self, "plan_type", plan_type)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "items", items)
        object.__setattr__(self, "cash_before", cash_before)
        object.__setattr__(self, "expected_cash_after", expected_cash_after)
        object.__setattr__(self, "expected_fees", expected_fees)
        object.__setattr__(self, "id", plan_id)
        object.__setattr__(self, "plan_hash", plan_hash)

    @property
    def buy_items(self) -> tuple[ExecutionItem, ...]:
        return tuple(item for item in self.items if item.side == "buy")

    @property
    def sell_items(self) -> tuple[ExecutionItem, ...]:
        return tuple(item for item in self.items if item.side == "sell")

    def as_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class ManualAuthorizationLimits:
    """Pure planning subset of an eventual user-approved authorization."""

    capital_limit: Decimal | int | float | str
    max_order_notional: Optional[Decimal | int | float | str] = None
    max_gross_exposure: Optional[Decimal | int | float | str] = None
    max_single_weight: Optional[Decimal | int | float | str] = None
    max_daily_items: Optional[int] = None

    def __post_init__(self) -> None:
        capital = _decimal(self.capital_limit, "capital_limit")
        if capital <= 0:
            raise ValueError("capital_limit must be positive")
        object.__setattr__(self, "capital_limit", capital)
        for name in ("max_order_notional", "max_gross_exposure", "max_single_weight"):
            raw = getattr(self, name)
            if raw is None:
                continue
            value = _decimal(raw, name)
            if value <= 0:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        if self.max_daily_items is not None and (isinstance(self.max_daily_items, bool) or self.max_daily_items < 1):
            raise ValueError("max_daily_items must be positive")


def resolve_action(*actions: DecisionAction | str) -> str:
    """Resolve competing actions using the fixed safety priority."""
    priority = {
        DecisionAction.HOLD.value: 0,
        DecisionAction.REBALANCE.value: 1,
        DecisionAction.REDUCE.value: 2,
        DecisionAction.FLAT.value: 3,
        DecisionAction.BLOCKED.value: 4,
        DecisionAction.RECONCILE.value: 5,
    }
    values = [_enum_value(item) for item in actions if item is not None]
    if not values:
        return DecisionAction.HOLD.value
    invalid = [item for item in values if item not in priority]
    if invalid:
        raise ValueError(f"invalid decision action: {invalid[0]}")
    return max(values, key=lambda item: priority[item])


def make_decision(
    signal_date: date,
    target_weights: Mapping[str, Decimal | int | float | str] | None = None,
    *,
    action: DecisionAction | str = DecisionAction.HOLD,
    risk_state: str = "normal",
    reason_codes: Sequence[str] = (),
    release_id: Optional[str] = None,
    authorization_id: Optional[str] = None,
    data_as_of: Optional[str] = None,
    blocked_reason: Optional[str] = None,
    revision: int = 0,
) -> DailyDecision:
    return DailyDecision(
        signal_date=signal_date,
        action=action,
        target_weights=target_weights or {},
        risk_state=risk_state,
        reason_codes=tuple(reason_codes),
        release_id=release_id,
        authorization_id=authorization_id,
        data_as_of=data_as_of,
        blocked_reason=blocked_reason,
        revision=revision,
    )


# Friendly aliases used by phase documents and callers.
ManualExecutionPolicy = ManualDailyPolicy
ManualPolicy = ManualDailyPolicy
PositionSnapshot = ManualPosition
Decision = DailyDecision
Cohort = ManualCohort
Plan = ExecutionPlan
PlanItem = ExecutionItem


__all__ = [
    "MANUAL_EXECUTION_PROTOCOL_VERSION", "MANUAL_DAILY_LABEL_ID", "MANUAL_DAILY_POLICY_ID",
    "DecisionAction", "DecisionStatus", "CohortStatus", "PlanStatus", "PlanItemStatus",
    "ManualDailyPolicy", "ManualExecutionPolicy", "ManualPosition", "PositionSnapshot",
    "ManualPolicy",
    "AccountSnapshot", "QuoteSnapshot", "ManualCohort", "Cohort", "DailyDecision", "Decision",
    "ExecutionItem", "PlanItem", "ExecutionPlan", "Plan", "ManualAuthorizationLimits",
    "canonical_json", "stable_hash", "resolve_action", "make_decision",
]
