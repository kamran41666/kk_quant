"""Cohort-aware atomic research execution for manual-daily evidence v1.

This module is pure and deterministic.  It records every intent and attempt,
uses only the previous completed session's volume for capacity, and never
submits a paper or broker order.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import hashlib
import json
from typing import Any, Mapping

from quant_engine.trading.effective_rules import EffectiveDatedTradingRuleRegistry


ZERO = Decimal("0")
CENT = Decimal("0.01")
LOT = 100
RESEARCH_EXECUTION_COSTS = {
    "baseline": {
        "commission_rate": Decimal("0.00025"), "minimum_commission": Decimal("5"),
        "transfer_rate": Decimal("0.00002"), "slippage_rate": Decimal("0.001"),
        "participation_rate": Decimal("0.01"),
        "dividend_tax_rate": Decimal("0"),
    },
    "stress": {
        "commission_rate": Decimal("0.0005"), "minimum_commission": Decimal("5"),
        "transfer_rate": Decimal("0.00002"), "slippage_rate": Decimal("0.003"),
        "participation_rate": Decimal("0.005"),
        "dividend_tax_rate": Decimal("0.20"),
    },
}


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError(f"{field}_must_be_numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{field}_must_be_finite")
    return result


def _money(value: Any) -> Decimal:
    return _decimal(value, "money").quantize(CENT, rounding=ROUND_HALF_UP)


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ResearchExecutionIntent:
    intent_id: str
    trade_date: date
    phase: str
    cohort_id: str
    code: str
    side: str
    requested_quantity: int
    reference_price: Decimal | int | float | str
    reason: str

    def __post_init__(self) -> None:
        if not self.intent_id or not self.cohort_id or not self.code:
            raise ValueError("intent_identity_required")
        if self.phase not in {"open", "close"} or self.side not in {"buy", "sell"}:
            raise ValueError("intent_phase_or_side_invalid")
        if isinstance(self.requested_quantity, bool) or self.requested_quantity <= 0:
            raise ValueError("intent_quantity_must_be_positive_integer")
        if self.side == "buy" and self.requested_quantity % LOT:
            raise ValueError("buy_intent_must_be_round_lot")
        price = _decimal(self.reference_price, "reference_price")
        if price <= 0:
            raise ValueError("reference_price_must_be_positive")
        object.__setattr__(self, "code", self.code.strip().upper())
        object.__setattr__(self, "reference_price", price)


@dataclass
class ResearchCohortLot:
    cohort_id: str
    code: str
    quantity: int
    buy_date: date
    avg_cost: Decimal
    cost_basis: Decimal = ZERO
    locked_quantities: list[tuple[date, int]] = field(default_factory=list)

    def sellable(self, day: date) -> int:
        if self.buy_date >= day:
            return 0
        locked = sum(quantity for unlock_date, quantity in self.locked_quantities if unlock_date > day)
        return max(0, self.quantity - locked)


@dataclass(frozen=True)
class ResearchCorporateAction:
    action_id: str
    code: str
    record_date: date | None
    ex_date: date | None
    pay_date: date | None
    stock_listing_date: date | None
    source_hash: str
    cash_per_share: Decimal | int | float | str = ZERO
    bonus_ratio: Decimal | int | float | str = ZERO
    allocation_verified: bool = True

    def __post_init__(self) -> None:
        if not self.action_id or not self.code:
            raise ValueError("corporate_action_identity_required")
        for value in (self.record_date, self.ex_date, self.pay_date, self.stock_listing_date):
            if value is not None and not isinstance(value, date):
                raise ValueError("corporate_action_dates_must_be_dates")
        if len(self.source_hash) != 64 or any(char not in "0123456789abcdef" for char in self.source_hash):
            raise ValueError("corporate_action_source_hash_invalid")
        cash = _decimal(self.cash_per_share, "cash_per_share")
        bonus = _decimal(self.bonus_ratio, "bonus_ratio")
        if cash < 0 or bonus < 0 or (cash == 0 and bonus == 0):
            raise ValueError("corporate_action_cash_or_bonus_invalid")
        object.__setattr__(self, "code", self.code.strip().upper())
        object.__setattr__(self, "cash_per_share", cash)
        object.__setattr__(self, "bonus_ratio", bonus)

    @property
    def economic_key(self) -> str:
        return _hash({
            "code": self.code, "record_date": self.record_date, "ex_date": self.ex_date,
            "pay_date": self.pay_date, "stock_listing_date": self.stock_listing_date,
            "cash_per_share": self.cash_per_share, "bonus_ratio": self.bonus_ratio,
        })

    @property
    def action_hash(self) -> str:
        return _hash({
            "economic_key": self.economic_key, "allocation_verified": self.allocation_verified,
            "source_hash": self.source_hash,
        })


class ManualResearchLedger:
    """Account ledger with cohort-scoped lots and shared daily capacity."""

    def __init__(
        self,
        initial_capital: Decimal | int | str,
        *,
        cost_scenario: str,
        registry: EffectiveDatedTradingRuleRegistry | None = None,
    ) -> None:
        capital = _money(initial_capital)
        if capital <= 0:
            raise ValueError("initial_capital_must_be_positive")
        if cost_scenario not in RESEARCH_EXECUTION_COSTS:
            raise ValueError("unsupported_research_cost_scenario")
        self.initial_capital = capital
        self.cash = capital
        self.cost_scenario = cost_scenario
        self.costs = RESEARCH_EXECUTION_COSTS[cost_scenario]
        self.registry = registry or EffectiveDatedTradingRuleRegistry.default()
        self.lots: dict[tuple[str, str], ResearchCohortLot] = {}
        self.capacity_used: dict[tuple[date, str], int] = {}
        self.intents: list[dict[str, Any]] = []
        self.attempts: list[dict[str, Any]] = []
        self.trades: list[dict[str, Any]] = []
        self.positions: list[dict[str, Any]] = []
        self.corporate_actions: list[dict[str, Any]] = []
        self.corporate_action_audit: list[dict[str, Any]] = []
        self.receivables: list[dict[str, Any]] = []
        self._actions: dict[str, ResearchCorporateAction] = {}
        self._economic_action_hashes: dict[str, str] = {}
        self._entitlements: dict[str, dict[str, int]] = {}
        self._recorded_actions: set[str] = set()
        self._applied_actions: set[str] = set()
        self._listed_actions: set[str] = set()
        self._intent_ids: set[str] = set()

    @property
    def receivable_cash(self) -> Decimal:
        return sum((_decimal(item["amount"], "receivable_amount") for item in self.receivables if item["status"] == "pending"), ZERO)

    @property
    def company_action_evidence_valid(self) -> bool:
        return not any(item["severity"] == "error" for item in self.corporate_action_audit)

    def _action_issue(self, action: ResearchCorporateAction, reason: str, **details: Any) -> None:
        key = (action.action_id, reason)
        if any((item["action_id"], item["reason"]) == key for item in self.corporate_action_audit):
            return
        self.corporate_action_audit.append({
            "action_id": action.action_id, "code": action.code,
            "reason": reason, "severity": "error", **details,
        })

    def register_corporate_actions(self, actions: list[ResearchCorporateAction]) -> None:
        for action in actions:
            if action.action_id in self._actions:
                if self._actions[action.action_id] != action:
                    raise ValueError("corporate_action_id_conflict")
                continue
            prior_hash = self._economic_action_hashes.get(action.economic_key)
            if prior_hash is not None:
                if prior_hash != action.action_hash:
                    raise ValueError("duplicate_economic_action_conflict")
                continue
            self._actions[action.action_id] = action
            self._economic_action_hashes[action.economic_key] = action.action_hash
            if action.record_date is None:
                self._action_issue(action, "record_date_unresolved")
            if action.ex_date is None:
                self._action_issue(action, "ex_date_unresolved")
            if action.record_date and action.ex_date and action.record_date >= action.ex_date:
                self._action_issue(action, "record_date_must_precede_ex_date")
            if action.cash_per_share > 0 and action.pay_date is None:
                self._action_issue(action, "pay_date_unresolved")
            if action.ex_date and action.pay_date and action.pay_date < action.ex_date:
                self._action_issue(action, "pay_date_precedes_ex_date")
            if action.ex_date and action.pay_date and action.pay_date < action.ex_date:
                self._action_issue(action, "pay_date_precedes_ex_date")
            if action.bonus_ratio > 0 and action.stock_listing_date is None:
                self._action_issue(action, "stock_listing_date_unresolved")
            if action.ex_date and action.stock_listing_date and action.stock_listing_date < action.ex_date:
                self._action_issue(action, "stock_listing_date_precedes_ex_date")
            if action.bonus_ratio > 0 and not action.allocation_verified:
                self._action_issue(action, "bonus_allocation_unverified")

    def record_action_entitlements_at_close(self, day: date) -> None:
        for action in sorted(self._actions.values(), key=lambda item: item.action_id):
            if action.record_date != day or action.action_id in self._recorded_actions:
                continue
            entitlements = {
                cohort_id: lot.quantity
                for (cohort_id, code), lot in sorted(self.lots.items())
                if code == action.code and lot.quantity > 0
            }
            self._entitlements[action.action_id] = entitlements
            self._recorded_actions.add(action.action_id)
            for cohort_id, quantity in entitlements.items():
                self.corporate_actions.append({
                    "action_id": action.action_id, "date": day.isoformat(),
                    "stage": "record", "code": action.code, "cohort_id": cohort_id,
                    "eligible_quantity": quantity, "source_hash": action.source_hash,
                    "action_hash": action.action_hash, "economic_key": action.economic_key,
                })

    @staticmethod
    def _allocate_bonus(entitlements: Mapping[str, int], ratio: Decimal) -> dict[str, int]:
        exact = {cohort_id: Decimal(quantity) * ratio for cohort_id, quantity in entitlements.items()}
        total = int(sum(exact.values(), ZERO).to_integral_value(rounding=ROUND_DOWN))
        allocated = {
            cohort_id: int(value.to_integral_value(rounding=ROUND_DOWN))
            for cohort_id, value in exact.items()
        }
        remaining = total - sum(allocated.values())
        order = sorted(exact, key=lambda cohort_id: (-(exact[cohort_id] - allocated[cohort_id]), cohort_id))
        for cohort_id in order[:remaining]:
            allocated[cohort_id] += 1
        return allocated

    @staticmethod
    def _allocate_cash(
        entitlements: Mapping[str, int],
        cash_per_share: Decimal,
        tax_rate: Decimal,
    ) -> dict[str, Decimal]:
        exact = {
            cohort_id: Decimal(quantity) * cash_per_share * (Decimal("1") - tax_rate)
            for cohort_id, quantity in entitlements.items()
        }
        account_total = sum(exact.values(), ZERO).quantize(CENT, rounding=ROUND_HALF_UP)
        allocated = {
            cohort_id: value.quantize(CENT, rounding=ROUND_DOWN)
            for cohort_id, value in exact.items()
        }
        remaining_cents = int(((account_total - sum(allocated.values(), ZERO)) / CENT).to_integral_value())
        order = sorted(exact, key=lambda cohort_id: (-(exact[cohort_id] - allocated[cohort_id]), cohort_id))
        for cohort_id in order[:remaining_cents]:
            allocated[cohort_id] += CENT
        return allocated

    def _apply_action(self, action: ResearchCorporateAction, day: date) -> None:
        if action.action_id in self._applied_actions:
            return
        entitlements = self._entitlements.get(action.action_id)
        if entitlements is None:
            self._action_issue(action, "record_entitlements_missing")
            entitlements = {}
        if action.cash_per_share > 0:
            cash_allocation = self._allocate_cash(
                entitlements, action.cash_per_share, self.costs["dividend_tax_rate"],
            )
            for cohort_id, quantity in sorted(entitlements.items()):
                gross = _money(Decimal(quantity) * action.cash_per_share)
                net = cash_allocation[cohort_id]
                effective_pay_date = (
                    action.pay_date
                    if action.pay_date is not None and (action.ex_date is None or action.pay_date >= action.ex_date)
                    else None
                )
                self.receivables.append({
                    "action_id": action.action_id, "code": action.code,
                    "cohort_id": cohort_id, "amount": str(net),
                    "pay_date": effective_pay_date.isoformat() if effective_pay_date else None,
                    "source_pay_date": action.pay_date.isoformat() if action.pay_date else None,
                    "status": "pending", "source_hash": action.source_hash,
                    "action_hash": action.action_hash, "economic_key": action.economic_key,
                })
                self.corporate_actions.append({
                    "action_id": action.action_id, "date": day.isoformat(),
                    "stage": "ex_cash", "code": action.code, "cohort_id": cohort_id,
                    "eligible_quantity": quantity, "gross_cash": str(gross),
                    "receivable_cash": str(net), "source_hash": action.source_hash,
                    "action_hash": action.action_hash, "economic_key": action.economic_key,
                })
        if action.bonus_ratio > 0 and action.allocation_verified:
            allocation = self._allocate_bonus(entitlements, action.bonus_ratio)
            unlock = (
                action.stock_listing_date
                if action.stock_listing_date is not None
                and (action.ex_date is None or action.stock_listing_date >= action.ex_date)
                else date.max
            )
            for cohort_id, quantity in sorted(allocation.items()):
                if quantity <= 0:
                    continue
                key = (cohort_id, action.code)
                lot = self.lots.get(key)
                if lot:
                    basis = lot.cost_basis
                    lot.quantity += quantity
                    lot.cost_basis = basis
                    lot.avg_cost = _money(basis / lot.quantity)
                else:
                    lot = ResearchCohortLot(cohort_id, action.code, quantity, day, ZERO, ZERO)
                    self.lots[key] = lot
                lot.locked_quantities.append((unlock, quantity))
                self.corporate_actions.append({
                    "action_id": action.action_id, "date": day.isoformat(),
                    "stage": "ex_bonus", "code": action.code, "cohort_id": cohort_id,
                    "eligible_quantity": entitlements[cohort_id], "bonus_quantity": quantity,
                    "stock_listing_date": unlock.isoformat(), "source_hash": action.source_hash,
                    "action_hash": action.action_hash, "economic_key": action.economic_key,
                })
        self._applied_actions.add(action.action_id)

    def process_corporate_actions_at_open(self, day: date) -> None:
        for action in sorted(self._actions.values(), key=lambda item: item.action_id):
            if action.ex_date == day:
                self._apply_action(action, day)
        for receivable in self.receivables:
            if receivable["status"] != "pending" or receivable["pay_date"] is None:
                continue
            if date.fromisoformat(receivable["pay_date"]) <= day:
                amount = _decimal(receivable["amount"], "receivable_amount")
                self.cash += amount
                receivable["status"] = "paid"
                self.corporate_actions.append({
                    "action_id": receivable["action_id"], "date": day.isoformat(),
                    "stage": "pay", "code": receivable["code"],
                    "cohort_id": receivable["cohort_id"], "cash": str(amount),
                    "source_hash": receivable["source_hash"],
                    "action_hash": receivable["action_hash"], "economic_key": receivable["economic_key"],
                })
        for action in sorted(self._actions.values(), key=lambda item: item.action_id):
            if (
                action.stock_listing_date is not None
                and action.stock_listing_date <= day
                and action.action_id in self._applied_actions
                and action.action_id not in self._listed_actions
            ):
                self._listed_actions.add(action.action_id)
                self.corporate_actions.append({
                    "action_id": action.action_id, "date": day.isoformat(),
                    "stage": "stock_listing", "code": action.code,
                    "source_hash": action.source_hash,
                    "action_hash": action.action_hash, "economic_key": action.economic_key,
                })

    @staticmethod
    def _finite_positive(value: Any) -> bool:
        try:
            result = _decimal(value, "market_value")
        except ValueError:
            return False
        return result > 0

    def _limit_prices(self, bar: Mapping[str, Any], day: date) -> tuple[Decimal, Decimal] | None:
        preclose = bar.get("preclose")
        if not self._finite_positive(preclose):
            return None
        rate = Decimal("0.05") if bool(bar.get("is_st")) and day < date(2026, 7, 6) else Decimal("0.10")
        previous = _decimal(preclose, "preclose")
        return tuple(
            (previous * multiplier).quantize(CENT, rounding=ROUND_HALF_UP)
            for multiplier in (Decimal("1") - rate, Decimal("1") + rate)
        )

    def _capacity(self, intent: ResearchExecutionIntent, previous_bar: Mapping[str, Any]) -> tuple[int, int]:
        previous_volume = previous_bar.get("volume")
        if not self._finite_positive(previous_volume):
            return 0, 0
        total = int((_decimal(previous_volume, "previous_volume") * self.costs["participation_rate"]).to_integral_value(rounding=ROUND_DOWN))
        used = self.capacity_used.get((intent.trade_date, intent.code), 0)
        return max(0, total - used), total

    def _fees(self, gross: Decimal, side: str, day: date) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        commission = max(_money(gross * self.costs["commission_rate"]), self.costs["minimum_commission"])
        transfer = _money(gross * self.costs["transfer_rate"])
        stamp_rate = self.registry.resolve("a-share", day).stamp_duty_rate if side == "sell" else ZERO
        stamp = _money(gross * stamp_rate)
        return commission, transfer, stamp, commission + transfer + stamp

    def execute_intent(
        self,
        intent: ResearchExecutionIntent,
        *,
        bar: Mapping[str, Any],
        previous_bar: Mapping[str, Any],
        historically_eligible: bool,
    ) -> dict[str, Any]:
        if intent.intent_id in self._intent_ids:
            raise ValueError("duplicate_research_intent_id")
        self._intent_ids.add(intent.intent_id)
        requested_notional = _money(intent.reference_price * intent.requested_quantity)
        intent_row = {
            "intent_id": intent.intent_id, "date": intent.trade_date.isoformat(),
            "phase": intent.phase, "cohort_id": intent.cohort_id, "code": intent.code,
            "side": intent.side, "requested_quantity": intent.requested_quantity,
            "reference_price": str(intent.reference_price),
            "requested_notional": str(requested_notional), "reason": intent.reason,
            "status": "pending", "filled_quantity": 0,
        }
        self.intents.append(intent_row)
        reason: str | None = None
        if intent.side == "buy" and not historically_eligible:
            reason = "historically_ineligible"
        elif bool(bar.get("is_suspended", True)):
            reason = "suspended"
        elif intent.side == "buy" and bool(bar.get("is_st", True)):
            reason = "st_entry_blocked"
        field = "open" if intent.phase == "open" else "close"
        raw_price = bar.get(field)
        if reason is None and not self._finite_positive(raw_price):
            reason = f"{field}_price_unavailable"
        limits = self._limit_prices(bar, intent.trade_date)
        if reason is None and limits is None:
            reason = "price_limit_reference_missing"
        down, up = limits or (ZERO, ZERO)
        raw = _decimal(raw_price, field) if self._finite_positive(raw_price) else ZERO
        if reason is None and (
            (intent.side == "buy" and raw >= up)
            or (intent.side == "sell" and raw <= down)
        ):
            reason = "upper_limit" if intent.side == "buy" else "lower_limit"
        capacity_remaining, capacity_total = self._capacity(intent, previous_bar)
        if reason is None and capacity_total <= 0:
            reason = "previous_session_volume_unavailable"
        lot = self.lots.get((intent.cohort_id, intent.code))
        sellable = lot.sellable(intent.trade_date) if lot else 0
        if reason is None and intent.side == "sell" and sellable <= 0:
            reason = "cohort_t_plus_one_or_position_unavailable"
        direction = Decimal("1") if intent.side == "buy" else Decimal("-1")
        price = (raw * (Decimal("1") + direction * self.costs["slippage_rate"])).quantize(CENT, rounding=ROUND_HALF_UP)
        if reason is None and (price < down or price > up):
            reason = "slippage_exceeds_price_limit"
        quantity = min(intent.requested_quantity, capacity_remaining)
        if intent.side == "buy":
            quantity = quantity // LOT * LOT
        elif lot and quantity != lot.quantity:
            quantity = quantity // LOT * LOT
        if reason is None and quantity <= 0:
            reason = "shared_capacity_or_lot_constraint"
        commission = transfer = stamp = total_fee = ZERO
        if reason is None:
            gross = _money(price * quantity)
            commission, transfer, stamp, total_fee = self._fees(gross, intent.side, intent.trade_date)
            if intent.side == "buy":
                while quantity >= LOT and self.cash < gross + total_fee:
                    quantity -= LOT
                    gross = _money(price * quantity)
                    commission, transfer, stamp, total_fee = self._fees(gross, intent.side, intent.trade_date) if quantity else (ZERO, ZERO, ZERO, ZERO)
                if quantity <= 0:
                    reason = "cash_or_lot_constraint"
            else:
                quantity = min(quantity, sellable)
                if quantity <= 0:
                    reason = "cohort_position_unavailable"
        attempt = {
            "attempt_id": f"{intent.intent_id}:attempt:1", "intent_id": intent.intent_id,
            "date": intent.trade_date.isoformat(), "phase": intent.phase,
            "cohort_id": intent.cohort_id, "code": intent.code, "side": intent.side,
            "requested_quantity": intent.requested_quantity,
            "capacity_total": capacity_total, "capacity_used_before": capacity_total - capacity_remaining,
            "capacity_available": capacity_remaining, "filled_quantity": 0,
            "status": "rejected" if reason else "filled", "reason": reason,
        }
        self.attempts.append(attempt)
        if reason:
            intent_row["status"] = "rejected"
            return attempt
        gross = _money(price * quantity)
        if intent.side == "buy":
            self.cash -= gross + total_fee
            key = (intent.cohort_id, intent.code)
            existing = self.lots.get(key)
            if existing:
                basis = existing.cost_basis + gross + total_fee
                existing.quantity += quantity
                existing.cost_basis = basis
                existing.avg_cost = _money(basis / existing.quantity)
            else:
                self.lots[key] = ResearchCohortLot(
                    intent.cohort_id, intent.code, quantity, intent.trade_date,
                    _money((gross + total_fee) / quantity), gross + total_fee,
                )
        else:
            if lot is None or quantity > lot.quantity:
                raise AssertionError("cohort lot changed during deterministic execution")
            self.cash += gross - total_fee
            original_quantity = lot.quantity
            lot.quantity -= quantity
            lot.cost_basis = _money(lot.cost_basis * Decimal(lot.quantity) / Decimal(original_quantity)) if lot.quantity else ZERO
            if lot.quantity:
                lot.avg_cost = _money(lot.cost_basis / lot.quantity)
            if lot.quantity == 0:
                del self.lots[(intent.cohort_id, intent.code)]
        self.capacity_used[(intent.trade_date, intent.code)] = (
            self.capacity_used.get((intent.trade_date, intent.code), 0) + quantity
        )
        attempt["filled_quantity"] = quantity
        attempt["capacity_used_after"] = self.capacity_used[(intent.trade_date, intent.code)]
        intent_row["status"] = "filled" if quantity == intent.requested_quantity else "partially_filled"
        intent_row["filled_quantity"] = quantity
        self.trades.append({
            "trade_id": f"{intent.intent_id}:fill:1", "intent_id": intent.intent_id,
            "attempt_id": attempt["attempt_id"], "date": intent.trade_date.isoformat(),
            "phase": intent.phase, "cohort_id": intent.cohort_id, "code": intent.code,
            "side": intent.side, "quantity": quantity, "price": str(price),
            "gross": str(gross), "commission": str(commission),
            "transfer_fee": str(transfer), "stamp_duty": str(stamp),
            "total_fee": str(total_fee),
        })
        return attempt

    def mark_close(self, day: date, bars: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        market_value = ZERO
        day_positions = []
        for (cohort_id, code), lot in sorted(self.lots.items()):
            bar = bars.get(code, {})
            if not self._finite_positive(bar.get("close")):
                raise ValueError(f"held_close_missing:{day.isoformat()}:{code}")
            value = _money(_decimal(bar["close"], "close") * lot.quantity)
            market_value += value
            row = {
                "date": day.isoformat(), "cohort_id": cohort_id, "code": code,
                "quantity": lot.quantity, "sellable_quantity": lot.sellable(day),
                "close": str(_money(bar["close"])), "market_value": str(value),
            }
            self.positions.append(row)
            day_positions.append(row)
        return {
            "date": day.isoformat(), "cash": str(_money(self.cash)),
            "receivable_cash": str(_money(self.receivable_cash)), "market_value": str(_money(market_value)),
            "equity": str(_money(self.cash + self.receivable_cash + market_value)),
            "position_count": len(day_positions),
        }

    @property
    def capacity_fill_rate(self) -> Decimal:
        denominator = sum((_decimal(row["requested_notional"], "requested_notional") for row in self.intents), ZERO)
        numerator = sum((
            _decimal(row["reference_price"], "reference_price") * int(row["filled_quantity"])
            for row in self.intents
        ), ZERO)
        return numerator / denominator if denominator else Decimal("1")


__all__ = [
    "RESEARCH_EXECUTION_COSTS", "ResearchExecutionIntent", "ResearchCohortLot", "ResearchCorporateAction",
    "ManualResearchLedger",
]
