"""Cohort-aware atomic research execution for manual-daily evidence v1.

This module is pure and deterministic.  It records every intent and attempt,
uses only the previous completed session's volume for capacity, and never
submits a paper or broker order.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
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
    },
    "stress": {
        "commission_rate": Decimal("0.0005"), "minimum_commission": Decimal("5"),
        "transfer_rate": Decimal("0.00002"), "slippage_rate": Decimal("0.003"),
        "participation_rate": Decimal("0.005"),
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
        self._intent_ids: set[str] = set()

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
        sellable = lot.quantity if lot and lot.buy_date < intent.trade_date else 0
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
                basis = existing.avg_cost * existing.quantity + gross + total_fee
                existing.quantity += quantity
                existing.avg_cost = _money(basis / existing.quantity)
            else:
                self.lots[key] = ResearchCohortLot(
                    intent.cohort_id, intent.code, quantity, intent.trade_date,
                    _money((gross + total_fee) / quantity),
                )
        else:
            if lot is None or quantity > lot.quantity:
                raise AssertionError("cohort lot changed during deterministic execution")
            self.cash += gross - total_fee
            lot.quantity -= quantity
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
                "quantity": lot.quantity, "sellable_quantity": lot.quantity if lot.buy_date < day else 0,
                "close": str(_money(bar["close"])), "market_value": str(value),
            }
            self.positions.append(row)
            day_positions.append(row)
        return {
            "date": day.isoformat(), "cash": str(_money(self.cash)),
            "receivable_cash": "0.00", "market_value": str(_money(market_value)),
            "equity": str(_money(self.cash + market_value)),
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
    "RESEARCH_EXECUTION_COSTS", "ResearchExecutionIntent", "ResearchCohortLot",
    "ManualResearchLedger",
]
