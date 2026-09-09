"""Effective-dated trading rules used by deterministic planning code.

This module deliberately contains no clock, database, network, or broker
dependency.  A rule is selected only from the market and the supplied
effective date.  The legacy paper ledger has a separate fee adapter below so
introducing this registry does not rewrite historical paper results.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable, Mapping, Optional


A_SHARE = "a-share"
CN_FUND = "cn-fund"
US_EQUITY = "us-equity"


def _decimal(value: Decimal | int | float | str, field: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


@dataclass(frozen=True)
class TradingRule:
    """One immutable market rule valid for a half-open date interval."""

    market: str
    rule_id: str
    version: str
    effective_from: date
    segment: str = "main-board"
    effective_until: Optional[date] = None
    lot_size: int = 100
    min_buy_quantity: int = 100
    allow_odd_lot_sell: bool = True
    t_plus_one: bool = True
    commission_rate: Decimal = Decimal("0.00025")
    minimum_commission: Decimal = Decimal("5")
    stamp_duty_rate: Decimal = Decimal("0.0005")
    other_fee_rate: Decimal = Decimal("0")
    price_tick: Decimal = Decimal("0.01")
    price_limit_up_pct: Optional[Decimal] = Decimal("0.10")
    price_limit_down_pct: Optional[Decimal] = Decimal("0.10")

    def __post_init__(self) -> None:
        if not self.market or not self.rule_id or not self.version:
            raise ValueError("market, rule_id and version are required")
        segment = str(self.segment).strip().lower()
        if not segment:
            raise ValueError("segment is required")
        if not isinstance(self.effective_from, date):
            raise ValueError("effective_from must be a date")
        if self.effective_until is not None and self.effective_until <= self.effective_from:
            raise ValueError("effective_until must be after effective_from")
        if self.lot_size < 1 or self.min_buy_quantity < 1:
            raise ValueError("lot_size and min_buy_quantity must be positive")
        if self.min_buy_quantity % self.lot_size:
            raise ValueError("min_buy_quantity must be a multiple of lot_size")
        for name in (
            "commission_rate", "minimum_commission", "stamp_duty_rate",
            "other_fee_rate", "price_tick", "price_limit_up_pct", "price_limit_down_pct",
        ):
            raw = getattr(self, name)
            if raw is None and name.startswith("price_limit_"):
                continue
            value = _decimal(raw, name)
            if value < 0 or (name == "price_tick" and value == 0):
                raise ValueError(f"{name} must be non-negative" if name != "price_tick" else "price_tick must be positive")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "segment", segment)

    def applies_to(self, as_of: date) -> bool:
        if not isinstance(as_of, date):
            raise ValueError("as_of must be a date")
        return self.effective_from <= as_of and (
            self.effective_until is None or as_of < self.effective_until
        )


@dataclass(frozen=True)
class FeeEstimate:
    """A plan-time fee estimate; it is never a user-reported execution fact."""

    commission: Decimal
    stamp_duty: Decimal
    other_fee: Decimal

    def __post_init__(self) -> None:
        for name in ("commission", "stamp_duty", "other_fee"):
            value = _decimal(getattr(self, name), name)
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
            object.__setattr__(self, name, value)

    @property
    def total(self) -> Decimal:
        return self.commission + self.stamp_duty + self.other_fee

    def as_dict(self) -> dict[str, str]:
        return {
            "commission": str(self.commission),
            "stamp_duty": str(self.stamp_duty),
            "other_fee": str(self.other_fee),
            "total": str(self.total),
        }


def _default_rules() -> tuple[TradingRule, ...]:
    # The 2023-08-28 A-share stamp-duty change is represented explicitly.  A
    # manual plan therefore resolves the rate from the trade date instead of
    # silently applying today's rate to historical research.
    return (
        TradingRule(
            market=A_SHARE,
            rule_id="a-share-main-board",
            version="v1-pre-2023-08-28",
            effective_from=date(1900, 1, 1),
            effective_until=date(2023, 8, 28),
            lot_size=100,
            min_buy_quantity=100,
            t_plus_one=True,
            stamp_duty_rate=Decimal("0.001"),
        ),
        TradingRule(
            market=A_SHARE,
            rule_id="a-share-main-board",
            version="v1-2023-08-28",
            effective_from=date(2023, 8, 28),
            lot_size=100,
            min_buy_quantity=100,
            t_plus_one=True,
            stamp_duty_rate=Decimal("0.0005"),
        ),
        TradingRule(
            market=CN_FUND,
            rule_id="cn-fund-nav",
            version="v1",
            effective_from=date(1900, 1, 1),
            lot_size=1,
            min_buy_quantity=1,
            allow_odd_lot_sell=True,
            t_plus_one=False,
            commission_rate=Decimal("0"),
            minimum_commission=Decimal("0"),
            stamp_duty_rate=Decimal("0"),
        ),
        TradingRule(
            market=US_EQUITY,
            rule_id="us-equity",
            version="v1",
            effective_from=date(1900, 1, 1),
            lot_size=1,
            min_buy_quantity=1,
            allow_odd_lot_sell=True,
            t_plus_one=False,
            commission_rate=Decimal("0"),
            minimum_commission=Decimal("0"),
            stamp_duty_rate=Decimal("0"),
        ),
    )


class EffectiveDatedTradingRuleRegistry:
    """Resolve and estimate rules without consulting mutable application state."""

    def __init__(self, rules: Iterable[TradingRule] | None = None):
        supplied = tuple(rules) if rules is not None else _default_rules()
        if not supplied:
            raise ValueError("at least one trading rule is required")
        for rule in supplied:
            if not isinstance(rule, TradingRule):
                raise TypeError("rules must contain TradingRule values")
        grouped: dict[str, list[TradingRule]] = {}
        for rule in supplied:
            grouped.setdefault(rule.market, []).append(rule)
        for market, values in grouped.items():
            by_segment: dict[str, list[TradingRule]] = {}
            for value in values:
                by_segment.setdefault(value.segment, []).append(value)
            for segment, segment_values in by_segment.items():
                ordered = sorted(segment_values, key=lambda item: item.effective_from)
                for previous, current in zip(ordered, ordered[1:]):
                    if previous.effective_until is None or previous.effective_until > current.effective_from:
                        raise ValueError(f"overlapping trading rules for {market}:{segment}")
        self._rules: Mapping[str, tuple[TradingRule, ...]] = {
            market: tuple(sorted(values, key=lambda item: item.effective_from))
            for market, values in grouped.items()
        }

    @classmethod
    def default(cls) -> "EffectiveDatedTradingRuleRegistry":
        return cls()

    def resolve(self, market: str, as_of: date, segment: str = "main-board") -> TradingRule:
        if not isinstance(as_of, date):
            raise ValueError("as_of must be a date")
        normalized_segment = str(segment).strip().lower()
        values = self._rules.get(str(market).strip().lower())
        if not values:
            raise ValueError(f"trading_rule_not_found:{market}")
        for rule in reversed(values):
            if rule.segment == normalized_segment and rule.applies_to(as_of):
                return rule
        raise ValueError(f"trading_rule_not_found:{market}:{as_of.isoformat()}")

    def rule_for(self, market: str, as_of: date, segment: str = "main-board") -> TradingRule:
        return self.resolve(market, as_of, segment)

    def estimate_fee(
        self,
        market: str,
        notional: Decimal | int | float | str,
        side: str,
        as_of: date,
        segment: str = "main-board",
    ) -> FeeEstimate:
        rule = self.resolve(market, as_of, segment)
        value = _decimal(notional, "notional")
        if value < 0:
            raise ValueError("notional must be non-negative")
        normalized_side = str(side).strip().lower()
        if normalized_side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        commission = max(value * rule.commission_rate, rule.minimum_commission) if value else Decimal("0")
        stamp = value * rule.stamp_duty_rate if normalized_side == "sell" else Decimal("0")
        other = value * rule.other_fee_rate
        return FeeEstimate(commission, stamp, other)

    def normalize_buy_quantity(
        self,
        market: str,
        target_notional: Decimal | int | float | str,
        price: Decimal | int | float | str,
        as_of: date,
        segment: str = "main-board",
    ) -> int:
        rule = self.resolve(market, as_of, segment)
        notional = _decimal(target_notional, "target_notional")
        unit_price = _decimal(price, "price")
        if notional < 0 or unit_price <= 0:
            raise ValueError("target_notional must be non-negative and price must be positive")
        quantity = int((notional / unit_price) // rule.lot_size) * rule.lot_size
        return quantity if quantity >= rule.min_buy_quantity else 0

    def normalize_sell_quantity(
        self,
        market: str,
        requested_quantity: int,
        available_quantity: int,
        as_of: date,
        segment: str = "main-board",
    ) -> int:
        rule = self.resolve(market, as_of, segment)
        if isinstance(requested_quantity, bool) or isinstance(available_quantity, bool):
            raise ValueError("quantities must be integers")
        if requested_quantity < 0 or available_quantity < 0:
            raise ValueError("quantities must be non-negative")
        quantity = min(requested_quantity, available_quantity)
        if quantity and not rule.allow_odd_lot_sell:
            quantity = quantity // rule.lot_size * rule.lot_size
        return quantity

    def describe(self, market: str, as_of: date) -> dict[str, object]:
        rule = self.resolve(market, as_of)
        return {
            "market": rule.market,
            "segment": rule.segment,
            "rule_id": rule.rule_id,
            "version": rule.version,
            "effective_from": rule.effective_from.isoformat(),
            "effective_until": rule.effective_until.isoformat() if rule.effective_until else None,
            "lot_size": rule.lot_size,
            "min_buy_quantity": rule.min_buy_quantity,
            "allow_odd_lot_sell": rule.allow_odd_lot_sell,
            "t_plus_one": rule.t_plus_one,
            "commission_rate": str(rule.commission_rate),
            "minimum_commission": str(rule.minimum_commission),
            "stamp_duty_rate": str(rule.stamp_duty_rate),
            "other_fee_rate": str(rule.other_fee_rate),
            "price_tick": str(rule.price_tick),
            "price_limit_up_pct": str(rule.price_limit_up_pct) if rule.price_limit_up_pct is not None else None,
            "price_limit_down_pct": str(rule.price_limit_down_pct) if rule.price_limit_down_pct is not None else None,
        }


def legacy_paper_fee_for(
    market: str,
    notional: Decimal | int | float | str,
    side: str,
) -> tuple[float, float]:
    """Keep the persistent paper ledger's historical 0.001 sell-tax result.

    This adapter is intentionally separate from the effective-dated manual
    rules.  Existing paper rows and tests must not change when M1 is added.
    """
    normalized_market = str(market).strip().lower()
    value = _decimal(notional, "notional")
    if value < 0:
        raise ValueError("notional must be non-negative")
    normalized_side = str(side).strip().lower()
    if normalized_side not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")
    if normalized_market == A_SHARE:
        commission = max(value * Decimal("0.00025"), Decimal("5")) if value else Decimal("0")
        stamp = value * Decimal("0.001") if normalized_side == "sell" else Decimal("0")
        return float(commission), float(stamp)
    if normalized_market in {CN_FUND, US_EQUITY}:
        return 0.0, 0.0
    raise ValueError(f"unsupported_paper_market:{normalized_market}")


TradingRuleRegistry = EffectiveDatedTradingRuleRegistry


__all__ = [
    "A_SHARE", "CN_FUND", "US_EQUITY", "TradingRule", "FeeEstimate",
    "EffectiveDatedTradingRuleRegistry", "legacy_paper_fee_for",
    "TradingRuleRegistry",
]
