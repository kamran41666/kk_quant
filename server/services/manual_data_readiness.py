"""Fail-closed readiness checks for the manual daily decision boundary."""
from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Sequence
import hashlib
import json


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def assess_manual_data_readiness(
    *,
    signal_date: date,
    data_as_of: str,
    calendar: Any,
    required_codes: Sequence[str],
    quotes: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate the exact inputs used by a decision; no closure is inferred."""
    reasons: list[str] = []
    coverage = calendar.ensure_coverage(signal_date, signal_date)
    if not coverage.get("complete"):
        reasons.append("TRADING_CALENDAR_UNAVAILABLE")
    trading_days = set(calendar.get_trading_days(signal_date, signal_date))
    if signal_date not in trading_days:
        reasons.append("signal_date_not_verified_trading_day")
    for code in sorted(set(required_codes)):
        quote = quotes.get(code)
        if quote is None:
            reasons.append(f"quote_missing:{code}")
            continue
        if quote.get("as_of") is None or quote.get("source") is None:
            reasons.append(f"quote_provenance_missing:{code}")
        if quote.get("close") is None or quote.get("open") is None:
            reasons.append(f"quote_ohlc_missing:{code}")
        if quote.get("is_suspended"):
            reasons.append(f"quote_suspended:{code}")
    payload = {
        "signal_date": signal_date.isoformat(), "data_as_of": data_as_of,
        "calendar": coverage, "required_codes": sorted(set(required_codes)),
        "quotes": quotes, "reasons": sorted(reasons),
    }
    return {"ready": not reasons, "status": "ready" if not reasons else "blocked", "reasons": sorted(reasons), "input_hash": _hash(payload)}


__all__ = ["assess_manual_data_readiness"]
