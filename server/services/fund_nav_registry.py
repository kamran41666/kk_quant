"""Evidence registry for official fund NAV observations.

The market endpoint is the trusted ingestion boundary for public Eastmoney
data.  A paper order may only reuse an observation that was returned by that
boundary, with the exact code, value, source, freshness and timestamp.  This
prevents a caller from labelling an arbitrary hand-entered number as
``eastmoney:fund_nav``; SQLite-backed evidence remains available after a
process restart.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import threading
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from server.models.schema import FundNavEvidence

_lock = threading.RLock()
_quotes: dict[str, dict[str, str | float | None]] = {}
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _timestamp(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed.astimezone(timezone.utc).isoformat()


def register_fund_nav(*, code: str, price: float, source: str,
                      as_of: Optional[str], freshness: str,
                      received_at: Optional[str] = None,
                      db: Optional[Session] = None) -> None:
    """Record one provider-returned NAV observation for later paper use.

    The in-memory copy keeps the hot path cheap and supports isolated unit
    tests.  When a database session is supplied (the HTTP ingestion path),
    the same exact observation is upserted durably so a process restart or a
    second worker does not erase valid evidence.
    """
    normalized = str(code).strip().upper()
    normalized_as_of = _timestamp(as_of)
    try:
        normalized_price = float(price)
    except (TypeError, ValueError):
        return
    if (not normalized or not math.isfinite(normalized_price) or normalized_price <= 0
            or not str(source).strip() or not normalized_as_of):
        return
    with _lock:
        _quotes[normalized] = {
            "price": normalized_price,
            "source": str(source),
            "as_of": normalized_as_of,
            "freshness": str(freshness).lower(),
            "received_at": _timestamp(received_at),
        }
        if db is not None:
            try:
                row = (db.query(FundNavEvidence)
                       .filter(FundNavEvidence.code == normalized,
                               FundNavEvidence.source == str(source),
                               FundNavEvidence.as_of == normalized_as_of,
                               FundNavEvidence.freshness == str(freshness).lower())
                       .first())
                if row is None:
                    row = FundNavEvidence(code=normalized, price=normalized_price, source=str(source),
                                          as_of=normalized_as_of, freshness=str(freshness).lower(),
                                          received_at=_timestamp(received_at))
                    db.add(row)
                else:
                    row.price = normalized_price
                    row.received_at = _timestamp(received_at)
                db.commit()
            except Exception:
                # A provider response must not be turned into an apparently
                # valid order when the durable evidence write failed.  Keep
                # the process copy for diagnostics but make callers fail
                # closed by removing this observation from the hot registry.
                db.rollback()
                _quotes.pop(normalized, None)
                raise


def verify_fund_nav(*, code: str, price: float, source: str,
                    as_of: Optional[str], freshness: str,
                    db: Optional[Session] = None) -> bool:
    """Return true only when the order exactly matches registered evidence."""
    normalized = str(code).strip().upper()
    requested_as_of = _timestamp(as_of)
    with _lock:
        quote = _quotes.get(normalized)
        matches_memory = False
        if quote is not None and requested_as_of is not None:
            matches_memory = (
                abs(float(quote["price"]) - float(price)) <= 1e-9
                and str(quote["source"]) == str(source)
                and str(quote["as_of"]) == requested_as_of
                and str(quote["freshness"]) == str(freshness).lower()
            )
        if db is None:
            return matches_memory
    if db is None or requested_as_of is None:
        return False
    row = (db.query(FundNavEvidence)
           .filter(FundNavEvidence.code == normalized,
                   FundNavEvidence.source == str(source),
                   FundNavEvidence.as_of == requested_as_of,
                   FundNavEvidence.freshness == str(freshness).lower())
           .order_by(FundNavEvidence.id.desc())
           .first())
    # With a session available, durable evidence is authoritative.  A caller
    # cannot rely on a stale in-memory observation after its database row has
    # been removed or changed.
    return row is not None and abs(float(row.price) - float(price)) <= 1e-9


def clear_fund_nav_registry() -> None:
    """Test/support hook; production code never clears evidence implicitly."""
    with _lock:
        _quotes.clear()
