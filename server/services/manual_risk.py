"""Pure risk preflight for a manual plan; it never mutates orders or cash."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Sequence


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def manual_plan_risk_preflight(
    *,
    account_status: str,
    authorization_status: str,
    confirmed_cash: Any,
    account_equity: Any,
    items: Sequence[Mapping[str, Any]],
    limits: Mapping[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    if account_status in {"reconcile", "suspended", "closed"}:
        reasons.append(f"account_{account_status}")
    if authorization_status not in {"approved", "active"}:
        reasons.append(f"authorization_{authorization_status}")
    if len(items) > int(limits.get("max_daily_items", 0)):
        reasons.append("max_daily_items_exceeded")
    cash = _d(confirmed_cash)
    equity = _d(account_equity)
    buy_cash = sum((_d(item.get("cash_required", 0)) for item in items if item.get("side") == "buy"), Decimal("0"))
    if buy_cash > cash:
        reasons.append("confirmed_cash_insufficient")
    max_order = _d(limits.get("max_order_notional", 0))
    max_weight = _d(limits.get("max_single_weight", 0))
    gross_limit = _d(limits.get("max_gross_exposure", 0))
    gross = Decimal("0")
    for item in items:
        notional = _d(item.get("expected_notional", 0))
        if notional > max_order:
            reasons.append(f"max_order_notional_exceeded:{item.get('code', '')}")
        if item.get("side") == "buy" and equity > 0 and notional / equity > max_weight:
            reasons.append(f"max_single_weight_exceeded:{item.get('code', '')}")
        gross += notional if item.get("side") == "buy" else -notional
    if equity > 0 and gross / equity > gross_limit:
        reasons.append("max_gross_exposure_exceeded")
    return {"allowed": not reasons, "status": "ready" if not reasons else "blocked", "reason_codes": sorted(set(reasons))}


__all__ = ["manual_plan_risk_preflight"]
