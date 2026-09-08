"""Raw-share research accounting, deliberately separate from paper authorization.

Cash entitlements belong to the account on ex-date but fund orders only on
pay-date. Bonus shares belong to the account on ex-date and stay locked until
their supplied listing date. Every unresolved assumption is recorded.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import math
from typing import Any

from quant_engine.backtest.types import Order, OrderSide, Position, Trade


RESEARCH_COSTS = {
    "baseline": dict(commission_rate=.00025, min_commission=5., slippage_rate=.001,
                     participation_rate=.01, transfer_rate=.00002, dividend_tax_rate=0.),
    "stress": dict(commission_rate=.0005, min_commission=5., slippage_rate=.003,
                   participation_rate=.005, transfer_rate=.00002, dividend_tax_rate=.2),
}


def finite_positive(value: Any) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def canonical_code(value: Any) -> str:
    value = str(value).strip().upper()
    if value.startswith(("SH.", "SZ.")):
        value = value[3:] + "." + value[:2]
    if len(value) == 6 and value.isdigit():
        value += ".SH" if value.startswith("6") else ".SZ"
    return value


def main_board(code: str) -> bool:
    return (code.endswith(".SH") and code[:3] in {"600", "601", "603"}) or (
        code.endswith(".SZ") and code[:3] in {"000", "001", "002"}
    )


@dataclass
class ResearchAction:
    action_id: str
    code: str
    record_date: date | None
    ex_date: date | None
    pay_date: date | None
    stock_date: date | None
    cash_ps: float
    bonus_ratio: float
    entitlement: int | None = None
    processed: bool = False
    bonus_allocation_verified: bool = True


class ResearchLedger:
    def __init__(self, initial_capital: float, cost_scenario: str = "baseline"):
        if not math.isfinite(initial_capital) or initial_capital <= 0:
            raise ValueError("initial_capital must be positive and finite")
        if cost_scenario not in RESEARCH_COSTS:
            raise ValueError(f"unknown research cost scenario: {cost_scenario}")
        self.cost_scenario = cost_scenario
        self.costs = dict(RESEARCH_COSTS[cost_scenario])
        self.cash = float(initial_capital)
        self._positions: dict[str, Position] = {}
        self._locks: dict[str, list[tuple[date, int]]] = {}
        self._prices: dict[str, float] = {}
        self._receivables: list[dict] = []
        self._deliveries: list[dict] = []
        self.audit: list[dict] = []
        self.corporate_actions: list[dict] = []
        self._audit_keys: set[tuple] = set()
        self._previous_close = float(initial_capital)
        self.daily_return = 0.
        self._order_sequence = 0

    @property
    def positions(self):
        return dict(self._positions)

    @property
    def market_value(self):
        return sum(pos.market_value for pos in self._positions.values())

    @property
    def receivable_cash(self):
        return sum(item["amount"] for item in self._receivables)

    @property
    def total_value(self):
        return self.cash + self.receivable_cash + self.market_value

    @property
    def validated(self):
        return not any(item["severity"] == "error" for item in self.audit)

    def issue(self, day, code, reason, *, severity="error", **details):
        key = (str(day), code, reason, str(sorted(details.items())))
        if key not in self._audit_keys:
            self._audit_keys.add(key)
            self.audit.append(dict(date=str(day) if day else None, code=code,
                                   reason=reason, severity=severity, **details))

    def sellable(self, code: str, day: date) -> int:
        pos = self._positions.get(code)
        locked = sum(shares for unlock, shares in self._locks.get(code, []) if unlock > day)
        return max(0, pos.shares - locked) if pos else 0

    def record_entitlement(self, action: ResearchAction, day: date):
        action.entitlement = self._positions[action.code].shares if action.code in self._positions else 0
        self.corporate_actions.append(dict(action_id=action.action_id, date=str(day),
                                           code=action.code, stage="record", shares=action.entitlement))

    def apply_action(self, action: ResearchAction, day: date):
        if action.processed:
            return
        action.processed = True
        owned = self._positions[action.code].shares if action.code in self._positions else 0
        if action.entitlement is None:
            if owned:
                self.issue(day, action.code, "corporate_action_record_date_unresolved", action_id=action.action_id)
            return
        shares = action.entitlement
        if shares <= 0:
            return
        gross = shares * action.cash_ps
        net = gross * (1. - self.costs["dividend_tax_rate"])
        if net:
            if action.pay_date is None or action.pay_date < day:
                self.issue(day, action.code, "dividend_pay_date_unresolved", action_id=action.action_id)
                payable = None
            else:
                payable = action.pay_date
            self._receivables.append(dict(action_id=action.action_id, code=action.code,
                                          amount=net, pay_date=payable))
        exact_bonus = shares * action.bonus_ratio
        unconfirmed_bonus = exact_bonus if not action.bonus_allocation_verified else 0.
        bonus = math.floor(exact_bonus + 1e-9) if action.bonus_allocation_verified else 0
        if unconfirmed_bonus:
            self.issue(day, action.code, "bonus_holder_allocation_unverified",
                       action_id=action.action_id, entitled_shares=shares,
                       unconfirmed_bonus_shares=unconfirmed_bonus,
                       treatment="unconfirmed_shares_not_included_in_holdings_or_equity")
        if action.bonus_allocation_verified and exact_bonus - bonus > 1e-8:
            self.issue(day, action.code, "bonus_fraction_floor_assumption", severity="warning",
                       action_id=action.action_id, discarded_shares=exact_bonus - bonus)
        if bonus:
            stock_date = action.stock_date
            if stock_date is None or stock_date < day:
                self.issue(day, action.code, "bonus_listing_date_unresolved", action_id=action.action_id)
                stock_date = date.max
            pos = self._positions.get(action.code)
            if pos:
                cost_basis = pos.avg_cost * pos.shares
                pos.shares += bonus
                pos.avg_cost = cost_basis / pos.shares
            else:
                self._positions[action.code] = Position(action.code, bonus, 0., 0., stock_date)
            self._locks.setdefault(action.code, []).append((stock_date, bonus))
            self._deliveries.append(dict(action_id=action.action_id, code=action.code,
                                         shares=bonus, stock_date=stock_date))
        self.corporate_actions.append(dict(action_id=action.action_id, date=str(day), code=action.code,
                                           stage="ex", entitled_shares=shares, gross_cash=gross,
                                           cash_receivable=net, bonus_shares=bonus,
                                           bonus_shares_booked=bonus,
                                           bonus_allocation_verified=action.bonus_allocation_verified,
                                           unconfirmed_bonus_shares=unconfirmed_bonus))

    def settle_due(self, day: date):
        for code in list(self._locks):
            self._locks[code] = [(unlock, shares) for unlock, shares in self._locks[code] if unlock > day]
        unpaid = []
        for item in self._receivables:
            if item["pay_date"] is not None and item["pay_date"] <= day:
                self.cash += item["amount"]
                self.corporate_actions.append(dict(action_id=item["action_id"], code=item["code"],
                                                   date=str(day), stage="pay", cash=item["amount"]))
            else:
                unpaid.append(item)
        self._receivables = unpaid
        pending = []
        for item in self._deliveries:
            if item["stock_date"] <= day:
                self.corporate_actions.append(dict(action_id=item["action_id"], code=item["code"],
                                                   date=str(day), stage="stock_listing", shares=item["shares"]))
            else:
                pending.append(item)
        self._deliveries = pending

    def delist(self, code: str, day: date):
        pos = self._positions.pop(code, None)
        if pos:
            self.issue(day, code, "delisting_zero_recovery_stress", shares=pos.shares,
                       last_marked_value=pos.market_value)
            self._locks.pop(code, None)
            self._deliveries = [item for item in self._deliveries if item["code"] != code]

    def marked_equity(self, bars: dict[str, dict], field: str, day: date) -> float:
        value = self.cash + self.receivable_cash
        for code, pos in self._positions.items():
            price = bars.get(code, {}).get(field)
            if not finite_positive(price):
                price = self._prices.get(code, 0.)
                self.issue(day, code, "held_price_missing", field=field)
            value += pos.shares * float(price)
        return value

    def mark_close(self, bars: dict[str, dict], day: date):
        for code, pos in self._positions.items():
            price = bars.get(code, {}).get("close")
            if finite_positive(price):
                self._prices[code] = float(price)
            else:
                self.issue(day, code, "held_price_missing", field="close")
            pos.market_value = pos.shares * self._prices.get(code, 0.)
        total = self.total_value
        self.daily_return = total / self._previous_close - 1. if self._previous_close else 0.
        self._previous_close = total

    @staticmethod
    def _limits(bar: dict, day: date):
        preclose = bar.get("preclose")
        if not finite_positive(preclose):
            return None
        rate = .05 if bool(bar.get("is_st")) and day < date(2026, 7, 6) else .1
        previous = Decimal(str(preclose))
        return tuple(float((previous * Decimal(str(multiplier))).quantize(Decimal(".01"), rounding=ROUND_HALF_UP))
                     for multiplier in (1. - rate, 1. + rate))

    def _fees(self, gross: float, side: OrderSide, day: date):
        commission = max(gross * self.costs["commission_rate"], self.costs["min_commission"])
        transfer = gross * self.costs["transfer_rate"]
        stamp = gross * (.001 if day < date(2023, 8, 28) else .0005) if side == OrderSide.SELL else 0.
        return commission, transfer, stamp

    def rebalance(self, target: dict[str, float], bars: dict[str, dict], previous_bars: dict[str, dict],
                  eligible: set[str], day: date) -> list[tuple[Order, Trade | None]]:
        equity = self.marked_equity(bars, "open", day)
        proposals = []
        for code in sorted(set(target) | set(self._positions)):
            pos = self._positions.get(code)
            owned = pos.shares if pos else 0
            opening = bars.get(code, {}).get("open")
            weight = target.get(code, 0.)
            if not finite_positive(opening):
                if owned or weight:
                    self.issue(day, code, "execution_bar_missing")
                continue
            desired = math.floor(max(0., equity * weight / float(opening)) + 1e-8)
            delta = desired - owned
            if delta:
                proposals.append((code, OrderSide.BUY if delta > 0 else OrderSide.SELL, abs(delta)))
        results = []
        for code, side, requested in sorted(proposals, key=lambda item: (item[1] == OrderSide.BUY, item[0])):
            self._order_sequence += 1
            order = Order(order_id=f"research-{day}-{self._order_sequence:08d}", code=code, date=day,
                          side=side, shares=requested)
            results.append((order, None))
            bar = bars[code]
            if side == OrderSide.BUY and code not in eligible:
                order.reject("ineligible_universe")
                continue
            if bool(bar.get("is_suspended", True)):
                order.reject("suspended")
                continue
            limits = self._limits(bar, day)
            if limits is None:
                self.issue(day, code, "price_limit_reference_missing")
                order.reject("price_limit_reference_missing")
                continue
            down, up = limits
            opening = float(bar["open"])
            if opening < down - 1e-8 or opening > up + 1e-8:
                self.issue(day, code, "unconfirmed_price_limit_regime", open=opening, down=down, up=up)
                order.reject("unconfirmed_price_limit_regime")
                continue
            if (side == OrderSide.BUY and opening >= up - 1e-8) or (side == OrderSide.SELL and opening <= down + 1e-8):
                order.reject("upper_limit" if side == OrderSide.BUY else "lower_limit")
                continue
            modeled_price = Decimal(str(opening)) * (
                Decimal("1") + Decimal(str(self.costs["slippage_rate"])) * (1 if side == OrderSide.BUY else -1)
            )
            # Check the modeled impact before rounding as well: rounding a
            # beyond-limit price back inside must not manufacture liquidity.
            price = float(modeled_price.quantize(Decimal(".01"), rounding=ROUND_HALF_UP))
            if float(modeled_price) < down - 1e-8 or float(modeled_price) > up + 1e-8 or price < down - 1e-8 or price > up + 1e-8:
                order.reject("slippage_exceeds_price_limit")
                continue
            previous_volume = previous_bars.get(code, {}).get("volume")
            if not finite_positive(previous_volume):
                order.reject("previous_session_volume_unavailable")
                continue
            capacity = math.floor(float(previous_volume) * self.costs["participation_rate"])
            quantity = min(requested, capacity)
            if side == OrderSide.SELL:
                owned = self._positions[code].shares
                quantity = min(quantity, self.sellable(code, day))
                if quantity != owned:
                    quantity = quantity // 100 * 100
            else:
                affordable = min(self.cash / (price * (1. + self.costs["commission_rate"] + self.costs["transfer_rate"])),
                                 max(0., self.cash - self.costs["min_commission"]) / (price * (1. + self.costs["transfer_rate"])))
                quantity = min(quantity, math.floor(affordable + 1e-8)) // 100 * 100
            if quantity <= 0:
                order.reject("cash_liquidity_lot_or_t1_constraint")
                continue
            gross = quantity * price
            commission, transfer, stamp = self._fees(gross, side, day)
            cost = commission + transfer + stamp
            if side == OrderSide.BUY:
                self.cash -= gross + cost
                pos = self._positions.get(code)
                if pos:
                    basis = pos.shares * pos.avg_cost + gross + cost
                    pos.shares += quantity
                    pos.avg_cost = basis / pos.shares
                    pos.market_value += gross
                else:
                    self._positions[code] = Position(code, quantity, (gross + cost) / quantity,
                                                     gross, day + timedelta(days=1))
                self._locks.setdefault(code, []).append((day + timedelta(days=1), quantity))
                self._prices[code] = price
            else:
                self.cash += gross - cost
                pos = self._positions[code]
                pos.shares -= quantity
                pos.market_value = pos.shares * self._prices.get(code, price)
                if not pos.shares:
                    self._positions.pop(code)
                    self._locks.pop(code, None)
            order.fill(quantity)
            trade = Trade(trade_id=order.order_id + "-fill", order_id=order.order_id, code=code,
                          date=day, side=side, shares=quantity, price=price, amount=gross,
                          commission=commission + transfer, stamp_duty=stamp)
            results[-1] = (order, trade)
        return results
