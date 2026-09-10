"""Pure planning functions for the M1 manual execution protocol.

The planner consumes immutable snapshots and quotes and returns immutable
protocol objects.  It never imports SQLAlchemy, reads the clock, calls a
provider, or submits a paper/live order.  Later phases may persist its output
and accept user-reported events, but those are separate responsibilities.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from quant_engine.trading.effective_rules import EffectiveDatedTradingRuleRegistry, FeeEstimate
from quant_engine.trading.manual_protocol import (
    AccountSnapshot,
    CohortStatus,
    DailyDecision,
    DecisionAction,
    ExecutionItem,
    ExecutionPlan,
    ManualAuthorizationLimits,
    ManualCohort,
    ManualDailyPolicy,
    ManualPosition,
    PlanItemStatus,
    PlanStatus,
    QuoteSnapshot,
    stable_hash,
)


_BLOCKING_ACTIONS = {DecisionAction.BLOCKED.value, DecisionAction.RECONCILE.value}
_NON_BUY_ACTIONS = {
    DecisionAction.REDUCE.value,
    DecisionAction.FLAT.value,
    DecisionAction.BLOCKED.value,
    DecisionAction.RECONCILE.value,
}
_TERMINAL_COHORTS = {CohortStatus.CLOSED.value, CohortStatus.CANCELLED.value}


def _as_decision(value: DailyDecision | Mapping[str, Any]) -> DailyDecision:
    return value if isinstance(value, DailyDecision) else DailyDecision(**dict(value))


def _as_position(value: ManualPosition | Mapping[str, Any]) -> ManualPosition:
    return value if isinstance(value, ManualPosition) else ManualPosition(**dict(value))


def _as_account(value: AccountSnapshot | Mapping[str, Any]) -> AccountSnapshot:
    if isinstance(value, AccountSnapshot):
        return value
    raw = dict(value)
    if "confirmed_cash" not in raw and "cash" in raw:
        raw["confirmed_cash"] = raw.pop("cash")
    positions = raw.get("positions", ())
    if isinstance(positions, Mapping):
        rows = []
        for code, item in positions.items():
            if isinstance(item, Mapping):
                rows.append({"code": code, **dict(item)})
            else:
                rows.append({"code": code, "quantity": item})
        raw["positions"] = rows
    raw.setdefault("positions", ())
    return AccountSnapshot(**raw)


def _as_cohorts(values: Iterable[ManualCohort | Mapping[str, Any]]) -> tuple[ManualCohort, ...]:
    return tuple(item if isinstance(item, ManualCohort) else ManualCohort(**dict(item)) for item in values)


def _as_quote(value: QuoteSnapshot | Mapping[str, Any] | Decimal | int | float | str, code: str) -> QuoteSnapshot:
    if isinstance(value, QuoteSnapshot):
        return value
    if isinstance(value, Mapping):
        return QuoteSnapshot(code=code, **dict(value))
    return QuoteSnapshot(code=code, price=value)


def _as_quotes(values: Mapping[str, QuoteSnapshot | Mapping[str, Any] | Decimal | int | float | str] | Sequence[QuoteSnapshot]) -> dict[str, QuoteSnapshot]:
    if isinstance(values, Mapping):
        return {str(code).strip().upper(): _as_quote(value, str(code)) for code, value in values.items()}
    result: dict[str, QuoteSnapshot] = {}
    for item in values:
        quote = item if isinstance(item, QuoteSnapshot) else QuoteSnapshot(**dict(item))
        result[quote.code] = quote
    return result


def _quote_hash(quotes: Mapping[str, QuoteSnapshot]) -> str:
    return stable_hash({code: quote for code, quote in sorted(quotes.items())})


def _fee(registry: EffectiveDatedTradingRuleRegistry, quote: QuoteSnapshot, quantity: int, side: str, execution_date: date) -> FeeEstimate:
    return registry.estimate_fee("a-share", quote.price * quantity, side, execution_date)


def _due_position(position: ManualPosition, cohorts: Mapping[str, ManualCohort], execution_date: date) -> bool:
    if position.planned_exit_date is not None and position.planned_exit_date <= execution_date:
        return True
    if position.cohort_id is None or position.cohort_id not in cohorts:
        return False
    cohort = cohorts[position.cohort_id]
    return cohort.status not in _TERMINAL_COHORTS and cohort.planned_exit_date <= execution_date


def _entry_or_exit_session(
    decision: DailyDecision,
    cohorts: Sequence[ManualCohort],
    execution_date: date,
    has_sells: bool,
    has_buys: bool,
) -> str:
    if has_buys and not has_sells:
        return "open"
    if has_sells and not has_buys:
        return "close"
    if has_buys and has_sells:
        # A mixed plan is still assigned to one concrete session.  Explicit
        # callers can choose the session; the default follows the entry leg
        # when the same execution date is a cohort entry date.
        if any(item.planned_entry_date == execution_date for item in cohorts):
            return "open"
        return "close"
    return "open"


def _plan_type(decision: DailyDecision, has_sells: bool, has_buys: bool) -> str:
    if has_sells and has_buys:
        return "mixed"
    if has_buys:
        return "entry"
    if has_sells:
        return "risk" if decision.action in {DecisionAction.REDUCE.value, DecisionAction.BLOCKED.value, DecisionAction.RECONCILE.value} else "exit"
    if decision.action == DecisionAction.FLAT.value:
        return "exit"
    return "risk" if decision.action in _BLOCKING_ACTIONS else "rebalance"


def _resolve_execution_date(decision: DailyDecision, cohorts: Sequence[ManualCohort], execution_date: date | None) -> date:
    if execution_date is not None:
        if not isinstance(execution_date, date):
            raise ValueError("execution_date must be a date")
        return execution_date
    candidates = sorted({
        item.planned_entry_date for item in cohorts if item.planned_entry_date > decision.signal_date
    } | {
        item.planned_exit_date for item in cohorts if item.planned_exit_date > decision.signal_date
    })
    return candidates[0] if candidates else decision.signal_date


def draft(
    decision: DailyDecision | Mapping[str, Any],
    cohorts: Iterable[ManualCohort | Mapping[str, Any]] = (),
    account_snapshot: AccountSnapshot | Mapping[str, Any] | None = None,
    quote_snapshot: Mapping[str, QuoteSnapshot | Mapping[str, Any] | Decimal | int | float | str] | Sequence[QuoteSnapshot] = (),
    *,
    execution_date: date | None = None,
    execution_session: str | None = None,
    policy: ManualDailyPolicy | None = None,
    registry: EffectiveDatedTradingRuleRegistry | None = None,
    authorization: ManualAuthorizationLimits | Mapping[str, Any] | None = None,
    version: int = 1,
    supersedes_plan_id: str | None = None,
) -> ExecutionPlan:
    """Create a deterministic, non-submitting execution plan.

    Buy quantities and buy fees use only ``confirmed_cash``.  Expected sell
    proceeds are reported for transparency but are never used to make a buy
    affordable; the next plan must use a later confirmed snapshot.
    """
    decision = _as_decision(decision)
    policy = policy or ManualDailyPolicy()
    registry = registry or EffectiveDatedTradingRuleRegistry.default()
    account = _as_account(account_snapshot or AccountSnapshot(confirmed_cash=0))
    cohort_values = _as_cohorts(cohorts)
    cohort_map = {item.id: item for item in cohort_values}
    quotes = _as_quotes(quote_snapshot)
    trade_date = _resolve_execution_date(decision, cohort_values, execution_date)
    rule = registry.resolve(policy.market, trade_date)
    if authorization is not None and not isinstance(authorization, ManualAuthorizationLimits):
        authorization = ManualAuthorizationLimits(**dict(authorization))

    reasons: set[str] = set()
    if authorization is not None and account.confirmed_cash > authorization.capital_limit:
        reasons.add("confirmed_cash_exceeds_capital_limit")

    positions = tuple(sorted(account.positions, key=lambda item: (item.code, item.cohort_id or "")))
    by_code: dict[str, list[ManualPosition]] = {}
    for position in positions:
        by_code.setdefault(position.code, []).append(position)

    sell_specs: list[dict[str, Any]] = []
    target_weights = dict(decision.target_weights)
    target_codes = set(target_weights)
    for code, code_positions in by_code.items():
        for position in code_positions:
            due = _due_position(position, cohort_map, trade_date)
            # Scheduled lifecycle exits belong to the close session.  Risk or
            # flat exits may be explicitly requested for another session.
            lifecycle_exit = due and execution_session != "open"
            target_exit = (
                decision.action == DecisionAction.FLAT.value
                or (decision.action in {DecisionAction.REBALANCE.value, DecisionAction.REDUCE.value} and code not in target_codes)
            )
            if not lifecycle_exit and not target_exit:
                continue
            quantity = min(position.quantity, position.available_quantity)
            if quantity > 0:
                sell_specs.append({
                    "code": code,
                    "quantity": quantity,
                    "available_quantity": position.available_quantity,
                    "cohort_id": position.cohort_id,
                    "target_weight": target_weights.get(code, Decimal("0")),
                })
            if quantity < position.quantity:
                reasons.add(f"sell_quantity_unavailable:{code}:{position.cohort_id or '-'}")

    buy_specs: list[dict[str, Any]] = []
    entry_cohorts = tuple(item for item in cohort_values if item.planned_entry_date == trade_date)
    if decision.action not in _NON_BUY_ACTIONS and execution_session != "close":
        if target_codes and not entry_cohorts:
            reasons.add("entry_cohort_missing")
        for code in sorted(target_codes):
            quote = quotes.get(code)
            if quote is None:
                reasons.add(f"quote_missing:{code}")
                continue
            current = sum(
                item.quantity for item in by_code.get(code, ())
                if item.cohort_id in {cohort.id for cohort in entry_cohorts}
            )
            desired = registry.normalize_buy_quantity(
                policy.market, account.equity * target_weights[code], quote.price, trade_date,
            )
            quantity = max(0, desired - current)
            quantity = quantity // rule.lot_size * rule.lot_size
            if quantity <= 0:
                continue
            buy_specs.append({
                "code": code,
                "quantity": quantity,
                "available_quantity": 0,
                "cohort_id": entry_cohorts[0].id if entry_cohorts else None,
                "target_weight": target_weights[code],
            })

    def priced_item(spec: Mapping[str, Any], side: str, sequence: int) -> ExecutionItem | None:
        code = str(spec["code"]).upper()
        quote = quotes.get(code)
        if quote is None:
            reasons.add(f"quote_missing:{code}")
            return None
        quantity = int(spec["quantity"])
        estimate = _fee(registry, quote, quantity, side, trade_date)
        notional = quote.price * quantity
        return ExecutionItem(
            code=code,
            side=side,
            phase=side,
            planned_quantity=quantity,
            reference_price=quote.price,
            price_source=quote.source,
            price_as_of=quote.as_of,
            cohort_id=spec.get("cohort_id"),
            available_quantity=int(spec.get("available_quantity", 0)),
            target_weight=spec.get("target_weight", Decimal("0")),
            cash_required=notional + estimate.total if side == "buy" else Decimal("0"),
            expected_proceeds=max(Decimal("0"), notional - estimate.total) if side == "sell" else Decimal("0"),
            estimated_commission=estimate.commission,
            estimated_tax=estimate.stamp_duty,
            estimated_other_fee=estimate.other_fee,
            order_sequence=sequence,
            reason_codes=tuple(sorted(
                (["quote_suspended"] if quote.suspended else [])
                + (["quote_limit_up"] if quote.limit_up and side == "buy" else [])
                + (["quote_limit_down"] if quote.limit_down and side == "sell" else [])
            )),
        )

    items: list[ExecutionItem] = []
    # Sells are always ordered before buys, while code order keeps the result
    # stable across processes and dictionary insertion order.
    for sequence, spec in enumerate(sorted(sell_specs, key=lambda item: (item["code"], item.get("cohort_id") or "")), start=1):
        item = priced_item(spec, "sell", sequence)
        if item is not None:
            items.append(item)
    provisional_buys: list[ExecutionItem] = []
    for sequence, spec in enumerate(sorted(buy_specs, key=lambda item: item["code"]), start=len(items) + 1):
        item = priced_item(spec, "buy", sequence)
        if item is not None:
            provisional_buys.append(item)

    buy_total = sum((item.cash_required for item in provisional_buys), Decimal("0"))
    if buy_total > account.confirmed_cash + Decimal("1e-12"):
        reasons.add("confirmed_cash_insufficient")
    else:
        items.extend(provisional_buys)

    expected_sell_proceeds = sum((item.expected_proceeds for item in items if item.side == "sell"), Decimal("0"))
    expected_fees = sum((item.estimated_fee for item in items), Decimal("0"))
    expected_cash_after = account.confirmed_cash - sum((item.cash_required for item in items if item.side == "buy"), Decimal("0")) + expected_sell_proceeds
    if expected_cash_after < 0:
        expected_cash_after = Decimal("0")

    has_sells = any(item.side == "sell" for item in items)
    has_buys = any(item.side == "buy" for item in items)
    session = execution_session or _entry_or_exit_session(decision, cohort_values, trade_date, has_sells, has_buys)
    if session not in {"open", "close"}:
        raise ValueError("execution_session must be open or close")
    plan_type = _plan_type(decision, has_sells, has_buys)
    if not items and decision.action in _BLOCKING_ACTIONS:
        reasons.add(decision.blocked_reason or f"decision_{decision.action}")
    if has_sells and has_buys and execution_session is None:
        raise ValueError("mixed_open_close_requires_explicit_session")
    status = PlanStatus.BLOCKED.value if reasons else (PlanStatus.COMPLETED.value if not items else PlanStatus.DRAFT.value)
    blocked_reason = ";".join(sorted(reasons)) if reasons else None
    input_hash = stable_hash({
        "decision": decision,
        "cohorts": cohort_values,
        "account": account,
        "quotes": quotes,
        "policy": policy,
        "authorization": authorization,
        "execution_date": trade_date,
        "execution_session": session,
        "version": version,
        "supersedes_plan_id": supersedes_plan_id,
    })
    return ExecutionPlan(
        decision_id=decision.id or decision.decision_hash,
        execution_date=trade_date,
        execution_session=session,
        plan_type=plan_type,
        items=items,
        authorization_id=decision.authorization_id,
        account_snapshot_id=account.snapshot_id or account.input_hash,
        version=version,
        status=status,
        cash_before=account.confirmed_cash,
        expected_cash_after=expected_cash_after,
        expected_fees=expected_fees,
        input_hash=input_hash,
        quote_snapshot_hash=_quote_hash(quotes),
        trading_rule_id=rule.rule_id,
        trading_rule_version=rule.version,
        blocked_reason=blocked_reason,
        supersedes_plan_id=supersedes_plan_id,
    )


def _reprice_items(
    plan: ExecutionPlan,
    account: AccountSnapshot,
    quotes: Mapping[str, QuoteSnapshot],
    registry: EffectiveDatedTradingRuleRegistry,
) -> tuple[tuple[ExecutionItem, ...], set[str], Decimal, Decimal, Decimal]:
    reasons: set[str] = set()
    items: list[ExecutionItem] = []
    for item in plan.items:
        quote = quotes.get(item.code)
        if quote is None:
            reasons.add(f"quote_missing:{item.code}")
            items.append(replace(item, status=PlanItemStatus.BLOCKED.value, reason_codes=tuple(sorted(set(item.reason_codes) | {"quote_missing"}))))
            continue
        estimate = _fee(registry, quote, item.planned_quantity, item.side, plan.execution_date)
        notional = quote.price * item.planned_quantity
        items.append(replace(
            item,
            reference_price=quote.price,
            price_source=quote.source,
            price_as_of=quote.as_of,
            cash_required=notional + estimate.total if item.side == "buy" else Decimal("0"),
            expected_proceeds=max(Decimal("0"), notional - estimate.total) if item.side == "sell" else Decimal("0"),
            estimated_commission=estimate.commission,
            estimated_tax=estimate.stamp_duty,
            estimated_other_fee=estimate.other_fee,
        ))
    buy_total = sum((item.cash_required for item in items if item.side == "buy" and item.status != PlanItemStatus.BLOCKED.value), Decimal("0"))
    if buy_total > account.confirmed_cash + Decimal("1e-12"):
        reasons.add("confirmed_cash_insufficient")
    sell_proceeds = sum((item.expected_proceeds for item in items if item.side == "sell" and item.status != PlanItemStatus.BLOCKED.value), Decimal("0"))
    fees = sum((item.estimated_fee for item in items), Decimal("0"))
    return tuple(items), reasons, buy_total, sell_proceeds, fees


def refresh(
    draft_plan: ExecutionPlan,
    quote_snapshot: Mapping[str, QuoteSnapshot | Mapping[str, Any] | Decimal | int | float | str] | Sequence[QuoteSnapshot],
    account_snapshot: AccountSnapshot | Mapping[str, Any],
    *,
    registry: EffectiveDatedTradingRuleRegistry | None = None,
) -> ExecutionPlan:
    """Refresh quote/account evidence in place conceptually; draft only."""
    if draft_plan.status != PlanStatus.DRAFT.value:
        raise ValueError("only draft plans can be refreshed")
    registry = registry or EffectiveDatedTradingRuleRegistry.default()
    account = _as_account(account_snapshot)
    quotes = _as_quotes(quote_snapshot)
    items, reasons, buy_total, sell_proceeds, fees = _reprice_items(draft_plan, account, quotes, registry)
    if not items and not reasons:
        reasons.add("plan_has_no_items")
    return replace(
        draft_plan,
        items=items,
        cash_before=account.confirmed_cash,
        expected_cash_after=max(Decimal("0"), account.confirmed_cash - buy_total + sell_proceeds),
        expected_fees=fees,
        input_hash=stable_hash({"prior_plan_hash": draft_plan.plan_hash, "account": account, "quotes": quotes}),
        quote_snapshot_hash=_quote_hash(quotes),
        account_snapshot_id=account.snapshot_id or account.input_hash,
        status=PlanStatus.BLOCKED.value if reasons else PlanStatus.DRAFT.value,
        blocked_reason=";".join(sorted(reasons)) if reasons else None,
        plan_hash="",
    )


def revise(
    frozen_plan: ExecutionPlan,
    quote_snapshot: Mapping[str, QuoteSnapshot | Mapping[str, Any] | Decimal | int | float | str] | Sequence[QuoteSnapshot],
    account_snapshot: AccountSnapshot | Mapping[str, Any],
    reason: str,
    *,
    registry: EffectiveDatedTradingRuleRegistry | None = None,
) -> ExecutionPlan:
    """Create a new draft version; the supplied ready/viewed plan is immutable."""
    if frozen_plan.status not in {PlanStatus.READY.value, PlanStatus.VIEWED.value}:
        raise ValueError("only ready or viewed plans can be revised")
    if not str(reason).strip():
        raise ValueError("revision reason is required")
    draft_plan = replace(
        frozen_plan,
        version=frozen_plan.version + 1,
        status=PlanStatus.DRAFT.value,
        supersedes_plan_id=frozen_plan.id,
        id=None,
        plan_hash="",
        blocked_reason=str(reason).strip(),
    )
    return refresh(draft_plan, quote_snapshot, account_snapshot, registry=registry)


@dataclass(frozen=True)
class PreflightResult:
    """Immutable result returned by the plan-only risk preflight."""

    allowed: bool
    reason_codes: Iterable[str] = ()
    item_results: Mapping[str, Sequence[str]] | None = None
    status: str = ""
    input_hash: str = ""

    def __post_init__(self) -> None:
        allowed = bool(self.allowed)
        status = "ready" if allowed else "blocked"
        reasons = tuple(sorted(set(str(item) for item in self.reason_codes if str(item))))
        item_results = {
            str(code): tuple(sorted(set(str(item) for item in values)))
            for code, values in sorted((self.item_results or {}).items())
        }
        input_hash = self.input_hash or stable_hash({
            "allowed": allowed,
            "status": status,
            "reason_codes": reasons,
            "item_results": item_results,
        })
        object.__setattr__(self, "allowed", allowed)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "item_results", item_results)
        object.__setattr__(self, "input_hash", input_hash)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "item_results": {key: list(values) for key, values in self.item_results.items()},
            "input_hash": self.input_hash,
        }


def preflight(
    plan: ExecutionPlan,
    authorization: ManualAuthorizationLimits | Mapping[str, Any] | None,
    account_snapshot: AccountSnapshot | Mapping[str, Any],
    quote_snapshot: Mapping[str, QuoteSnapshot | Mapping[str, Any] | Decimal | int | float | str] | Sequence[QuoteSnapshot],
    *,
    policy: ManualDailyPolicy | None = None,
) -> PreflightResult:
    """Check a plan against current facts without changing any state."""
    account = _as_account(account_snapshot)
    quotes = _as_quotes(quote_snapshot)
    policy = policy or ManualDailyPolicy()
    if authorization is not None and not isinstance(authorization, ManualAuthorizationLimits):
        authorization = ManualAuthorizationLimits(**dict(authorization))
    reasons: set[str] = set()
    item_reasons: dict[str, list[str]] = {}
    if plan.status in {PlanStatus.SUPERSEDED.value, PlanStatus.CANCELLED.value, PlanStatus.EXPIRED.value, PlanStatus.BLOCKED.value}:
        reasons.add(f"plan_{plan.status}")
    if plan.cash_before != account.confirmed_cash:
        reasons.add("account_snapshot_changed")
    if plan.quote_snapshot_hash != _quote_hash(quotes):
        reasons.add("quote_snapshot_changed_requires_revision")
    if plan.execution_session not in {"open", "close"}:
        reasons.add("invalid_execution_session")
    active_items = [item for item in plan.items if item.status not in {PlanItemStatus.BLOCKED.value, PlanItemStatus.SKIPPED.value}]
    if authorization is not None:
        if authorization.max_daily_items is not None and len(active_items) > authorization.max_daily_items:
            reasons.add("max_daily_items_exceeded")
        if authorization.max_order_notional is not None:
            for item in active_items:
                if item.expected_notional > authorization.max_order_notional + Decimal("1e-12"):
                    item_reasons.setdefault(item.code, []).append("max_order_notional_exceeded")
        if account.equity > authorization.capital_limit + Decimal("1e-12"):
            reasons.add("account_equity_exceeds_capital_limit")
    buy_total = Decimal("0")
    projected_by_code: dict[str, Decimal] = {}
    for position in account.positions:
        projected_by_code[position.code] = projected_by_code.get(position.code, Decimal("0")) + position.market_value
    for item in active_items:
        quote = quotes.get(item.code)
        if quote is None:
            item_reasons.setdefault(item.code, []).append("quote_missing")
            continue
        if not quote.as_of:
            item_reasons.setdefault(item.code, []).append("quote_timestamp_missing")
        if quote.freshness != "fresh":
            item_reasons.setdefault(item.code, []).append("quote_not_fresh")
        if quote.suspended:
            item_reasons.setdefault(item.code, []).append("suspended")
        if not quote.is_trading_day:
            item_reasons.setdefault(item.code, []).append("not_trading_day")
        if item.side == "buy":
            if quote.limit_up:
                item_reasons.setdefault(item.code, []).append("limit_up_buy")
            if item.planned_quantity % policy.buy_lot_size:
                item_reasons.setdefault(item.code, []).append("buy_lot_invalid")
            buy_total += item.cash_required
            projected_by_code[item.code] = projected_by_code.get(item.code, Decimal("0")) + quote.price * item.planned_quantity
        else:
            if quote.limit_down:
                item_reasons.setdefault(item.code, []).append("limit_down_sell")
            available = sum(position.available_quantity for position in account.positions if position.code == item.code)
            if item.planned_quantity > available:
                item_reasons.setdefault(item.code, []).append("sell_quantity_unavailable")
            projected_by_code[item.code] = max(
                Decimal("0"), projected_by_code.get(item.code, Decimal("0")) - quote.price * item.planned_quantity,
            )
    if buy_total > account.confirmed_cash + Decimal("1e-12"):
        reasons.add("confirmed_cash_insufficient")
    if account.equity <= 0 and active_items:
        reasons.add("non_positive_equity")
    if authorization is not None and authorization.max_single_weight is not None and account.equity > 0:
        for code, value in projected_by_code.items():
            if value / account.equity > authorization.max_single_weight + Decimal("1e-12"):
                item_reasons.setdefault(code, []).append("max_single_weight_exceeded")
                reasons.add(f"{code}:max_single_weight_exceeded")
    projected_gross = sum(projected_by_code.values(), Decimal("0"))
    if authorization is not None and authorization.max_gross_exposure is not None and account.equity > 0:
        if projected_gross / account.equity > authorization.max_gross_exposure + Decimal("1e-12"):
            reasons.add("max_gross_exposure_exceeded")
    for code, values in item_reasons.items():
        reasons.update(f"{code}:{value}" for value in values)
    return PreflightResult(not reasons, reasons, item_reasons)


# Names matching the phase plan's prose interface.
build_plan = draft
refresh_plan = refresh
revise_plan = revise
preflight_plan = preflight
ManualPlanningService = type(
    "ManualPlanningService",
    (),
    {
        "draft": staticmethod(draft),
        "refresh": staticmethod(refresh),
        "revise": staticmethod(revise),
        "preflight": staticmethod(preflight),
    },
)


__all__ = [
    "draft", "build_plan", "refresh", "refresh_plan", "revise", "revise_plan",
    "preflight", "preflight_plan", "PreflightResult", "ManualPlanningService",
]
