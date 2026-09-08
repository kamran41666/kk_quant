"""Persistent, paper-only execution service for Phase 2.

The service deliberately keeps the broker boundary local. A paper order is
accepted with explicit quote provenance, written atomically to the ledger, and
can be replayed safely with the same idempotency key.
"""
from __future__ import annotations

import json
import math
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from quant_engine.data.live import SHANGHAI_TZ
from quant_engine.trading.gateway import OrderIntent, PaperBrokerGateway, RiskEngine, RiskLimits
from server.models.schema import (
    PaperAccount, PaperAccountPosition, PaperFill, PaperLedgerEvent,
    PaperDailyReport, PaperDeviation, PaperLot, PaperOrder, PaperValuation,
)
from server.services.paper_market_rules import (
    A_SHARE,
    accepts_quote_freshness,
    fee_for,
    is_index_symbol,
    is_market_trading_day,
    market_metadata,
    normalize_market,
    normalize_symbol,
    rule_for,
    unlock_date,
    validate_quantity,
)
from server.services.fund_nav_registry import verify_fund_nav

COMMISSION_RATE = 0.00025
STAMP_DUTY_RATE = 0.001
MIN_COMMISSION = 5.0
MAX_QUOTE_AGE_SECONDS = 15 * 60
_account_lock = threading.RLock()
US_EASTERN_TZ = ZoneInfo("America/New_York")


def _begin_account_transaction(db: Session) -> None:
    """Take a database-level write lock for file-backed SQLite.

    The process-local lock protects threads in one worker; ``BEGIN IMMEDIATE``
    also serializes independent worker processes. In-memory test databases do
    not support this cross-connection guarantee and use the process lock only.
    """
    bind = db.get_bind()
    if bind.dialect.name != "sqlite" or bind.url.database in (None, "", ":memory:"):
        return
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    except OperationalError as exc:
        db.rollback()
        raise ValueError("account_busy_retry") from exc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trade_date() -> date:
    return datetime.now(SHANGHAI_TZ).date()


def _normalize_trade_date(value: Optional[date]) -> tuple[date, bool]:
    """Return the effective trade date and whether it was explicitly supplied.

    ``submit_order`` historically used the host's current date. Keeping that
    default preserves the interactive paper-trading API, while an explicit
    date gives backtests/replays one authoritative date for lots and audit
    timestamps. Datetimes are rejected instead of silently dropping their
    time component.
    """
    if value is None:
        return _trade_date(), False
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError("trade_date must be a date")
    today = _trade_date()
    if value > today:
        raise ValueError("trade_date_in_future")
    return value, True


def _trade_timestamp(trade_date: date, *, explicit: bool) -> str:
    """Create a stable audit timestamp for historical trades."""
    if not explicit:
        return _now()
    return f"{trade_date.isoformat()}T00:00:00+00:00"


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("price_as_of must be an ISO-8601 timestamp") from exc
    # Naive timestamps in legacy rows were emitted by the Shanghai-local
    # application.  Interpret them consistently before comparing quote dates.
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=SHANGHAI_TZ)).astimezone(timezone.utc)


def _validate_quote(*, price: float, price_source: str, price_as_of: Optional[str],
                    price_freshness: str, market: str = A_SHARE,
                    code: Optional[str] = None, trade_date: Optional[date] = None,
                    db: Optional[Session] = None) -> None:
    """Validate a quote for the account's market.

    Fund NAV is an end-of-day observation and may legitimately be labelled
    stale.  Equity markets retain the stricter fifteen-minute freshness gate.
    The market argument is deliberately server-derived for paper orders; it
    cannot be used by a caller to turn an equity quote into a fund quote.
    """
    market = normalize_market(market)
    if not isinstance(price, (int, float)) or not math.isfinite(float(price)) or price <= 0:
        raise ValueError("price must be positive")
    source = (price_source or "").strip()
    freshness = (price_freshness or "").strip().lower()
    if not source:
        raise ValueError("price_source is required")
    if freshness in {"unknown", "expired"} or not accepts_quote_freshness(market, freshness):
        raise ValueError("quote_stale_or_unknown")
    parsed = _parse_iso(price_as_of)
    if parsed is not None and parsed > datetime.now(timezone.utc) + timedelta(seconds=5):
        raise ValueError("quote_timestamp_in_future")
    if market == "cn-fund":
        # A fund order is not an intraday trade. It can only be filled from a
        # dated official NAV observation; a hand-entered price would create a
        # false execution path, especially on weekends or when the feed is
        # unavailable.
        if freshness == "manual" or source != "eastmoney:fund_nav":
            raise ValueError("fund_order_requires_official_nav")
        if parsed is None:
            raise ValueError("fund_nav_timestamp_required")
        effective_day = trade_date or _trade_date()
        if parsed.astimezone(SHANGHAI_TZ).date() > effective_day:
            raise ValueError("fund_nav_date_in_future")
        if not code or not verify_fund_nav(code=code, price=float(price), source=source,
                                           as_of=price_as_of, freshness=freshness, db=db):
            raise ValueError("fund_nav_not_verified")
        return
    if freshness == "manual":
        if source != "manual_input":
            raise ValueError("manual quote must use price_source=manual_input")
        return
    if parsed is None:
        raise ValueError("quote_timestamp_required")
    # Official fund NAVs are not intraday quotes.  Their source timestamp is
    # still retained for audit, while age is intentionally not treated as an
    # execution-time freshness failure.
    if market == "cn-fund" and freshness == "stale":
        return
    # Historical paper replays pass an explicit accounting date.  In that
    # mode freshness is judged against the replay day rather than the wall
    # clock, while a future observation is still rejected.
    # No market may use a quote that is newer than the accounting date.  This
    # matters for explicit historical A-share replays as well as US/fund runs.
    quote_zone = US_EASTERN_TZ if market == "us-equity" else SHANGHAI_TZ
    quote_day = parsed.astimezone(quote_zone).date()
    if trade_date is not None and quote_day > trade_date:
        raise ValueError("quote_date_in_future")
    if trade_date is not None and quote_day <= trade_date < _trade_date():
        return
    age = (datetime.now(timezone.utc) - parsed).total_seconds()
    if age < -5 or age > MAX_QUOTE_AGE_SECONDS:
        raise ValueError("quote_stale_or_unknown")


def _account_dict(account: PaperAccount) -> dict[str, Any]:
    try:
        metadata = market_metadata(account.market)
    except ValueError as exc:
        # A manually corrupted database must fail closed rather than silently
        # treating a USD account as CNY/A-share.
        raise ValueError("invalid_paper_account_market") from exc
    today = _trade_date()
    return {
        "id": account.id, "name": account.name,
        "initial_capital": account.initial_capital, "cash": account.cash,
        **metadata,
        "trading": {
            "today": today.isoformat(),
            "is_trading_day": is_market_trading_day(account.market, today),
        },
        "status": account.status,
        "validation_only": bool(getattr(account, "validation_only", False)),
        "execution_mode": "paper_only",
        "paper_only": True,
        "live_execution": False,
        "risk": {"max_order_notional": account.max_order_notional,
                 "max_position_weight": account.max_position_weight,
                 "max_daily_loss": account.max_daily_loss},
        "created_at": account.created_at, "updated_at": account.updated_at,
    }


def create_account(db: Session, *, name: str, initial_capital: float,
                   max_order_notional: float = 100_000.0,
                   max_position_weight: float = 0.25,
                   max_daily_loss: float = 0.03,
                   market: str = A_SHARE,
                   validation_only: bool = False) -> dict[str, Any]:
    market = normalize_market(market)
    if not isinstance(initial_capital, (int, float)) or not math.isfinite(float(initial_capital)) or initial_capital <= 0:
        raise ValueError("initial_capital must be positive")
    if (not math.isfinite(float(max_order_notional)) or max_order_notional <= 0
            or not 0 < max_position_weight <= 1
            or not math.isfinite(float(max_daily_loss)) or max_daily_loss < 0):
        raise ValueError("invalid risk limits")
    account = PaperAccount(name=name, initial_capital=initial_capital, cash=initial_capital,
                           max_order_notional=max_order_notional,
                           max_position_weight=max_position_weight, max_daily_loss=max_daily_loss,
                           market=market, validation_only=bool(validation_only))
    db.add(account)
    db.commit()
    db.refresh(account)
    return _account_dict(account)


def list_accounts(db: Session) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for account in db.query(PaperAccount).order_by(PaperAccount.created_at.desc()).all():
        positions = (
            db.query(PaperAccountPosition)
            .filter(PaperAccountPosition.account_id == account.id, PaperAccountPosition.shares > 0)
            .all()
        )
        market_value = sum(float(item.market_value) for item in positions)
        valuation = _latest_valuation(db, account.id)
        result.append({
            **_account_dict(account),
            "market_value": market_value,
            "equity": float(account.cash) + market_value,
            "daily_return": valuation.daily_return if valuation else 0.0,
            "valuation_date": valuation.valuation_date if valuation else None,
        })
    return result


def _latest_valuation(db: Session, account_id: str) -> Optional[PaperValuation]:
    return (db.query(PaperValuation).filter(PaperValuation.account_id == account_id)
            .order_by(PaperValuation.valuation_date.desc(), PaperValuation.created_at.desc()).first())


def account_snapshot(db: Session, account_id: str) -> dict[str, Any]:
    account = db.query(PaperAccount).filter(PaperAccount.id == account_id).first()
    if not account:
        raise KeyError("paper account not found")
    positions = db.query(PaperAccountPosition).filter(PaperAccountPosition.account_id == account_id).all()
    account_market = normalize_market(account.market)
    if any(normalize_market(item.market) != account_market for item in positions):
        raise ValueError("account_position_market_mismatch")
    market_value = sum(item.market_value for item in positions)
    valuation = _latest_valuation(db, account_id)
    return {**_account_dict(account), "market_value": market_value,
            "equity": account.cash + market_value,
            "daily_return": valuation.daily_return if valuation else 0.0,
            "valuation_date": valuation.valuation_date if valuation else None,
            "positions": [{"market": account_market, "currency": market_metadata(account_market)["currency"],
                           "code": item.code, "shares": item.shares,
                           "avg_cost": item.avg_cost, "market_value": item.market_value,
                           "last_price": item.last_price,
                           "price_source": item.last_price_source,
                           "price_as_of": item.last_price_as_of,
                           "price_freshness": item.last_price_freshness}
                          for item in positions if item.shares > 0]}


def list_orders(db: Session, account_id: str, limit: int = 50) -> list[dict[str, Any]]:
    if not db.query(PaperAccount.id).filter(PaperAccount.id == account_id).first():
        raise KeyError("paper account not found")
    rows = (db.query(PaperOrder).filter(PaperOrder.account_id == account_id)
            .order_by(PaperOrder.created_at.desc()).limit(limit).all())
    return [_order_dict(item) for item in rows]


def list_ledger(db: Session, account_id: str, limit: int = 100) -> list[dict[str, Any]]:
    if not db.query(PaperAccount.id).filter(PaperAccount.id == account_id).first():
        raise KeyError("paper account not found")
    rows = (db.query(PaperLedgerEvent).filter(PaperLedgerEvent.account_id == account_id)
            .order_by(PaperLedgerEvent.created_at.desc()).limit(limit).all())
    account = db.query(PaperAccount).filter(PaperAccount.id == account_id).first()
    account_market = normalize_market(account.market) if account else A_SHARE
    return [{"id": item.id, "event_type": item.event_type, "reference_id": item.reference_id,
             "market": account_market, "currency": rule_for(account_market).currency,
             "amount": item.amount, "payload": json.loads(item.payload or "{}"),
             "created_at": item.created_at} for item in rows]


def list_valuations(db: Session, account_id: str, limit: int = 60) -> list[dict[str, Any]]:
    if not db.query(PaperAccount.id).filter(PaperAccount.id == account_id).first():
        raise KeyError("paper account not found")
    rows = (db.query(PaperValuation).filter(PaperValuation.account_id == account_id)
            .order_by(PaperValuation.valuation_date.desc(), PaperValuation.created_at.desc())
            .limit(limit).all())
    account = db.query(PaperAccount).filter(PaperAccount.id == account_id).first()
    account_market = normalize_market(account.market) if account else A_SHARE
    return [{"date": row.valuation_date, "market": account_market,
             "currency": rule_for(account_market).currency,
             "cash": row.cash, "market_value": row.market_value,
             "total_value": row.equity, "daily_return": row.daily_return,
             "price_source": row.price_source, "price_as_of": row.price_as_of,
             "price_freshness": row.price_freshness,
             "price_metadata": json.loads(row.price_metadata or "{}"),
             "execution_mode": "paper_only", "paper_only": True, "live_execution": False}
            for row in reversed(rows)]


def account_report(db: Session, account_id: str) -> dict[str, Any]:
    """Build a beginner-readable account report from durable checkpoints."""
    account = db.query(PaperAccount).filter(PaperAccount.id == account_id).first()
    if not account:
        raise KeyError("paper account not found")
    rows = (db.query(PaperValuation).filter(PaperValuation.account_id == account_id)
            .order_by(PaperValuation.valuation_date.asc()).all())
    positions = db.query(PaperAccountPosition).filter(PaperAccountPosition.account_id == account_id).all()
    account_market = normalize_market(account.market)
    if any(normalize_market(item.market) != account_market for item in positions):
        raise ValueError("account_position_market_mismatch")
    current_equity = account.cash + sum(item.market_value for item in positions)
    equities = [row.equity for row in rows if row.equity > 0]
    peak = account.initial_capital
    max_drawdown = 0.0
    for equity in equities:
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1.0)
    last = rows[-1] if rows else None
    order_count = db.query(PaperOrder).filter(PaperOrder.account_id == account_id).count()
    fill_count = db.query(PaperFill).filter(PaperFill.account_id == account_id).count()
    return {
        "account_id": account_id,
        "market": account_market,
        "currency": rule_for(account_market).currency,
        "initial_capital": account.initial_capital,
        "equity": last.equity if last else current_equity,
        "total_return": ((last.equity if last else current_equity) / account.initial_capital - 1.0) if account.initial_capital > 0 else 0.0,
        "max_drawdown": max_drawdown,
        "valuation_count": len(rows),
        "order_count": order_count,
        "fill_count": fill_count,
        "last_valuation_date": last.valuation_date if last else None,
        "execution_mode": "paper_only",
        "paper_only": True,
        "live_execution": False,
    }


def reconcile_account(db: Session, account_id: str) -> dict[str, Any]:
    """Rebuild cash and share totals from immutable fills and compare state."""
    account = db.query(PaperAccount).filter(PaperAccount.id == account_id).first()
    if not account:
        raise KeyError("paper account not found")
    account_market = normalize_market(account.market)
    fills = db.query(PaperFill).filter(PaperFill.account_id == account_id).all()
    if any(normalize_market(item.market) != account_market for item in fills):
        raise ValueError("paper_fill_market_mismatch")
    positions = db.query(PaperAccountPosition).filter(PaperAccountPosition.account_id == account_id).all()
    if any(normalize_market(item.market) != account_market for item in positions):
        raise ValueError("account_position_market_mismatch")
    events = db.query(PaperLedgerEvent).filter(PaperLedgerEvent.account_id == account_id, PaperLedgerEvent.event_type == "fill").all()
    expected_cash = account.initial_capital + sum(float(event.amount) for event in events)
    actual_cash = account.cash
    expected_positions: dict[str, float] = {}
    for fill in fills:
        expected_positions[fill.code] = expected_positions.get(fill.code, 0) + (fill.quantity if fill.side == "buy" else -fill.quantity)
    actual_positions = {row.code: row.shares for row in positions if row.shares}
    position_codes = sorted(set(expected_positions) | set(actual_positions))
    position_diffs = {code: {"expected": expected_positions.get(code, 0), "actual": actual_positions.get(code, 0)} for code in position_codes if expected_positions.get(code, 0) != actual_positions.get(code, 0)}
    cash_diff = actual_cash - expected_cash
    ok = abs(cash_diff) <= 1e-6 and not position_diffs
    return {"account_id": account_id, "market": account_market,
            "currency": rule_for(account_market).currency,
            "status": "ok" if ok else "mismatch",
            "cash": {"expected": expected_cash, "actual": actual_cash, "difference": cash_diff},
            "position_diffs": position_diffs,
            "execution_mode": "paper_only", "paper_only": True, "live_execution": False}


def record_deviation(db: Session, *, account_id: str, valuation_date: date,
                     expected_return: float, source: str = "manual_plan") -> dict[str, Any]:
    if not -1 < expected_return < 10:
        raise ValueError("expected_return is outside supported range")
    valuation = (db.query(PaperValuation).filter(PaperValuation.account_id == account_id, PaperValuation.valuation_date == valuation_date.isoformat()).first())
    if not valuation:
        raise ValueError("valuation_required_before_deviation")
    row = (db.query(PaperDeviation).filter(PaperDeviation.account_id == account_id, PaperDeviation.valuation_date == valuation_date.isoformat()).first())
    tracking_error = valuation.daily_return - expected_return
    if row:
        row.expected_return, row.actual_return, row.tracking_error, row.source = expected_return, valuation.daily_return, tracking_error, source
    else:
        row = PaperDeviation(account_id=account_id, valuation_date=valuation_date.isoformat(), expected_return=expected_return, actual_return=valuation.daily_return, tracking_error=tracking_error, source=source)
        db.add(row)
    db.commit()
    db.refresh(row)
    return {"date": row.valuation_date, "expected_return": row.expected_return, "actual_return": row.actual_return, "tracking_error": row.tracking_error, "source": row.source,
            "execution_mode": "paper_only", "paper_only": True, "live_execution": False}


def list_deviations(db: Session, account_id: str, limit: int = 60) -> list[dict[str, Any]]:
    if not db.query(PaperAccount.id).filter(PaperAccount.id == account_id).first():
        raise KeyError("paper account not found")
    rows = (db.query(PaperDeviation).filter(PaperDeviation.account_id == account_id).order_by(PaperDeviation.valuation_date.desc()).limit(limit).all())
    return [{"date": row.valuation_date, "expected_return": row.expected_return, "actual_return": row.actual_return, "tracking_error": row.tracking_error, "source": row.source,
             "execution_mode": "paper_only", "paper_only": True, "live_execution": False} for row in reversed(rows)]


def build_daily_report(db: Session, *, account_id: str, report_date: date) -> dict[str, Any]:
    account = db.query(PaperAccount).filter(PaperAccount.id == account_id).first()
    if not account:
        raise KeyError("paper account not found")
    valuation = (db.query(PaperValuation).filter(PaperValuation.account_id == account_id, PaperValuation.valuation_date == report_date.isoformat()).first())
    if not valuation:
        raise ValueError("valuation_required_before_report")

    # A daily report is an as-of checkpoint.  Never derive its metrics from
    # the current account report: later valuations and orders would otherwise
    # leak into a historical day (for example, a 2024 report displaying a
    # 2026 return).  Valuations are unique per account/day, so the ordered
    # rows below form the complete observable history through ``report_date``.
    historical_valuations = (db.query(PaperValuation)
                             .filter(PaperValuation.account_id == account_id,
                                     PaperValuation.valuation_date <= report_date.isoformat())
                             .order_by(PaperValuation.valuation_date.asc()).all())
    last_historical = historical_valuations[-1]
    peak = account.initial_capital
    max_drawdown = 0.0
    for item in historical_valuations:
        peak = max(peak, item.equity)
        max_drawdown = min(max_drawdown, item.equity / peak - 1.0)
    # Use the next midnight as an exclusive bound so records with fractional
    # seconds during the final second of the report date are included.
    cutoff = f"{report_date + timedelta(days=1)}T00:00:00"
    order_count = (db.query(PaperOrder)
                   .filter(PaperOrder.account_id == account_id,
                           PaperOrder.created_at < cutoff).count())
    fill_count = (db.query(PaperFill)
                  .filter(PaperFill.account_id == account_id,
                          PaperFill.created_at < cutoff).count())
    reconciliation = reconcile_account(db, account_id)
    row = (db.query(PaperDailyReport).filter(PaperDailyReport.account_id == account_id, PaperDailyReport.report_date == report_date.isoformat()).first())
    values = {"equity": valuation.equity, "daily_return": valuation.daily_return,
              "total_return": last_historical.equity / account.initial_capital - 1.0,
              "max_drawdown": max_drawdown, "order_count": order_count,
              "fill_count": fill_count,
              "reconciliation_status": reconciliation["status"]}
    if row:
        for key, value in values.items():
            setattr(row, key, value)
    else:
        row = PaperDailyReport(account_id=account_id, report_date=report_date.isoformat(), **values)
        db.add(row)
    db.commit()
    db.refresh(row)
    return {"date": row.report_date, **values}


def list_daily_reports(db: Session, account_id: str, limit: int = 60) -> list[dict[str, Any]]:
    if not db.query(PaperAccount.id).filter(PaperAccount.id == account_id).first():
        raise KeyError("paper account not found")
    rows = (db.query(PaperDailyReport).filter(PaperDailyReport.account_id == account_id).order_by(PaperDailyReport.report_date.desc()).limit(limit).all())
    return [{
        "date": row.report_date, "equity": row.equity,
        "daily_return": row.daily_return, "total_return": row.total_return,
        "max_drawdown": row.max_drawdown, "order_count": row.order_count,
        "fill_count": row.fill_count,
        "reconciliation_status": row.reconciliation_status,
        "execution_mode": "paper_only", "paper_only": True, "live_execution": False,
    } for row in reversed(rows)]


def _ensure_legacy_lot(db: Session, account_id: str, market: str, code: str,
                       position: PaperAccountPosition, trade_date: date) -> list[PaperLot]:
    lots = (db.query(PaperLot).filter(PaperLot.account_id == account_id,
                                     PaperLot.market == market,
                                     PaperLot.code == code,
                                     PaperLot.remaining_quantity > 0)
            .order_by(PaperLot.buy_at.asc()).all())
    if lots or position.shares <= 0:
        return lots
    # Existing Phase-1 positions have no lot history. They are already settled
    # because the legacy engine only exposed end-of-day holdings.
    legacy = PaperLot(account_id=account_id, market=market, code=code, quantity=position.shares,
                      remaining_quantity=position.shares, buy_price=position.avg_cost,
                      buy_at=f"{trade_date.isoformat()}T00:00:00+00:00",
                      unlock_date=trade_date.isoformat(), owner="manual")
    db.add(legacy)
    db.flush()
    return [legacy]


def _available_lots(db: Session, account_id: str, market: str, code: str, trade_date: date,
                    position: Optional[PaperAccountPosition],
                    lot_owner: Optional[str] = None,
                    lot_owner_id: Optional[str] = None) -> tuple[float, list[PaperLot]]:
    lots = _ensure_legacy_lot(db, account_id, market, code, position, trade_date) if position else []
    available = sum(item.remaining_quantity for item in lots
                    if item.unlock_date <= trade_date.isoformat()
                    and (lot_owner is None or getattr(item, "owner", "manual") == lot_owner)
                    and (lot_owner_id is None or getattr(item, "owner_id", None) == lot_owner_id))
    return available, lots


def _consume_lots(lots: list[PaperLot], quantity: float, trade_date: date,
                  lot_owner: Optional[str] = None,
                  lot_owner_id: Optional[str] = None) -> None:
    remaining = quantity
    for lot in sorted(lots, key=lambda item: item.buy_at):
        if lot.unlock_date > trade_date.isoformat() or lot.remaining_quantity <= 0:
            continue
        if lot_owner is not None and getattr(lot, "owner", "manual") != lot_owner:
            continue
        if lot_owner_id is not None and getattr(lot, "owner_id", None) != lot_owner_id:
            continue
        consumed = min(remaining, lot.remaining_quantity)
        lot.remaining_quantity -= consumed
        remaining -= consumed
        if remaining <= 1e-9:
            return
    if remaining:
        raise ValueError("insufficient_settled_position")


def submit_order(db: Session, *, account_id: str, idempotency_key: str, code: str,
                 side: str, quantity: float, price: float,
                 price_source: str = "manual_input", price_as_of: Optional[str] = None,
                 price_freshness: str = "manual",
                 trade_date: Optional[date] = None,
                 lot_owner: Optional[str] = None,
                 lot_owner_id: Optional[str] = None,
                 market: Optional[str] = None) -> dict[str, Any]:
    """Submit one paper order atomically from the user's perspective."""
    effective_trade_date, explicit_trade_date = _normalize_trade_date(trade_date)
    audit_timestamp = _trade_timestamp(effective_trade_date, explicit=explicit_trade_date)
    if lot_owner is not None and lot_owner not in {"manual", "strategy"}:
        raise ValueError("lot_owner must be manual or strategy")
    if lot_owner == "manual" and lot_owner_id is not None:
        raise ValueError("manual_lot_owner_id_not_allowed")
    if lot_owner == "strategy" and (not lot_owner_id or len(str(lot_owner_id)) > 36):
        raise ValueError("strategy_lot_owner_id_required")
    if lot_owner is None and lot_owner_id is not None:
        raise ValueError("lot_owner_required_for_owner_id")
    with _account_lock:
        _begin_account_transaction(db)
        account = db.query(PaperAccount).filter(PaperAccount.id == account_id).first()
        if not account:
            raise KeyError("paper account not found")
        account_market = normalize_market(account.market)
        if getattr(account, "validation_only", False) and lot_owner != "strategy":
            raise ValueError("validation_only_account_rejects_manual_orders")
        requested_market = normalize_market(market) if market is not None else account_market
        if requested_market != account_market:
            raise ValueError("order_market_must_match_account")
        canonical_code = normalize_symbol(account_market, code)
        if is_index_symbol(account_market, canonical_code):
            raise ValueError("index_not_tradable")
        quantity = validate_quantity(account_market, quantity)
        side = str(side or "").strip().lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        _validate_quote(price=price, price_source=price_source, price_as_of=price_as_of,
                        price_freshness=price_freshness, market=account_market,
                        code=canonical_code, trade_date=effective_trade_date, db=db)
        if explicit_trade_date and not is_market_trading_day(account_market, effective_trade_date):
            raise ValueError("market_not_trading_day")
        existing = db.query(PaperOrder).filter(PaperOrder.idempotency_key == idempotency_key).first()
        if existing:
            same = (existing.account_id == account_id and existing.market == account_market
                    and existing.code == canonical_code and existing.side == side
                    and abs(existing.quantity - quantity) <= 1e-9
                    and existing.limit_price is not None
                    and abs(existing.limit_price - price) <= 1e-9)
            if not same:
                raise ValueError("idempotency_key already belongs to a different order")
            return _order_dict(existing)

        if account.status != "active":
            raise ValueError("paper account is not active")
        rows = db.query(PaperAccountPosition).filter(PaperAccountPosition.account_id == account_id).all()
        if any(normalize_market(item.market) != account_market for item in rows):
            raise ValueError("account_position_market_mismatch")
        position = next((item for item in rows if item.market == account_market and item.code == canonical_code), None)
        sell_owner = lot_owner if side == "sell" else None
        sell_owner_id = lot_owner_id if side == "sell" else None
        available, lots = _available_lots(db, account_id, account_market, canonical_code,
                                          effective_trade_date, position, sell_owner, sell_owner_id)
        if side == "sell" and quantity > available:
            total = position.shares if position else 0
            reason = ("t_plus_one_lock" if account_market == A_SHARE and total >= quantity and available < quantity
                      else "insufficient_settled_position")
            order = PaperOrder(account_id=account_id, idempotency_key=idempotency_key,
                               market=account_market, code=canonical_code,
                               side=side, quantity=quantity, status="rejected", limit_price=price,
                               filled_quantity=0, reject_reason=reason, price_source=price_source,
                               price_as_of=price_as_of, price_freshness=price_freshness,
                               created_at=audit_timestamp)
            db.add(order)
            db.commit()
            db.refresh(order)
            return _order_dict(order)

        latest = _latest_valuation(db, account_id)
        daily_return = latest.daily_return if latest else 0.0
        equity = account.cash + sum(item.market_value for item in rows)
        risk = RiskEngine(RiskLimits(account.max_order_notional, account.max_position_weight, account.max_daily_loss))
        gateway = PaperBrokerGateway(account.cash, risk)
        for row in rows:
            gateway.set_position(row.code, row.shares, row.last_price if row.last_price > 0 else row.avg_cost)
        gateway.set_price(canonical_code, price)
        notional = quantity * price
        commission, stamp = fee_for(account_market, notional, side)
        intent = OrderIntent(idempotency_key, canonical_code, side, quantity, price)
        reason = risk.check(intent, reference_price=price, equity=equity,
                            current_position_value=(position.shares * price if position else 0.0),
                            daily_return=daily_return)
        if side == "buy" and notional + commission > account.cash + 1e-9:
            reason = reason or "insufficient_cash_including_fees"
        report = None if reason else gateway.submit(intent)
        status, filled = (("rejected", 0) if reason else (report.status.value, report.filled_quantity))
        reject_reason = reason or (report.reason if report else None)
        order = PaperOrder(account_id=account_id, idempotency_key=idempotency_key,
                           market=account_market, code=canonical_code,
                           side=side, quantity=quantity, status=status, limit_price=price,
                           filled_quantity=filled, fill_price=report.fill_price if report else None,
                           reject_reason=reject_reason, price_source=price_source,
                           price_as_of=price_as_of, price_freshness=price_freshness,
                           created_at=audit_timestamp)
        db.add(order)
        db.flush()
        if filled:
            fill_price = report.fill_price
            gross, fees = filled * fill_price, commission + stamp
            if side == "buy":
                account.cash -= gross + fees
                if position:
                    position.avg_cost = (position.shares * position.avg_cost + gross + fees) / (position.shares + filled)
                    position.shares += filled
                else:
                    position = PaperAccountPosition(account_id=account_id, market=account_market,
                                                    code=canonical_code, shares=filled,
                                                    avg_cost=(gross + fees) / filled, last_price=fill_price,
                                                    market_value=gross,
                                                    last_price_source=price_source,
                                                    last_price_as_of=price_as_of,
                                                    last_price_freshness=price_freshness)
                    db.add(position)
                unlock = unlock_date(account_market, effective_trade_date).isoformat()
                db.add(PaperLot(account_id=account_id, market=account_market, code=canonical_code, quantity=filled,
                                remaining_quantity=filled, buy_price=fill_price, buy_at=audit_timestamp,
                                unlock_date=unlock, owner=lot_owner or "manual", owner_id=lot_owner_id))
            else:
                _consume_lots(lots, filled, effective_trade_date, sell_owner, sell_owner_id)
                account.cash += gross - fees
                if position:
                    position.shares -= filled
                    position.last_price = fill_price
                    if position.shares <= 0:
                        db.delete(position)
                    else:
                        position.market_value = position.shares * fill_price
            if position and position.shares > 0:
                position.last_price, position.market_value = fill_price, position.shares * fill_price
                position.last_price_source = price_source
                position.last_price_as_of = price_as_of
                position.last_price_freshness = price_freshness
            db.add(PaperFill(order_id=order.id, account_id=account_id, market=account_market,
                             code=canonical_code, side=side,
                             quantity=filled, price=fill_price, amount=gross,
                             created_at=audit_timestamp))
            unlock = unlock_date(account_market, effective_trade_date).isoformat() if side == "buy" else None
            db.add(PaperLedgerEvent(account_id=account_id, event_type="fill", reference_id=order.id,
                                    amount=(-gross - fees if side == "buy" else gross - fees),
                                    payload=json.dumps({"market": account_market,
                                                        "currency": rule_for(account_market).currency,
                                                        "code": canonical_code, "side": side,
                                                        "quantity": filled, "price": fill_price,
                                                        "fees": fees, "price_source": price_source,
                                                        "price_as_of": price_as_of,
                                                        "price_freshness": price_freshness,
                                                        "unlock_date": unlock}),
                                    created_at=audit_timestamp))
        account.updated_at = _now()
        db.commit()
        db.refresh(order)
        return _order_dict(order)


def mark_to_market(db: Session, *, account_id: str, prices: Mapping[str, float],
                   valuation_date: Optional[date] = None,
                   price_source: str = "manual_input", price_as_of: Optional[str] = None,
                   price_freshness: str = "manual",
                   price_metadata: Optional[Mapping[str, Mapping[str, Optional[str]]]] = None) -> dict[str, Any]:
    with _account_lock:
        return _mark_to_market_unlocked(db, account_id=account_id, prices=prices,
                                        valuation_date=valuation_date,
                                        price_source=price_source, price_as_of=price_as_of,
                                        price_freshness=price_freshness,
                                        price_metadata=price_metadata)


def _mark_to_market_unlocked(db: Session, *, account_id: str, prices: Mapping[str, float],
                             valuation_date: Optional[date] = None,
                             price_source: str = "manual_input", price_as_of: Optional[str] = None,
                             price_freshness: str = "manual",
                             price_metadata: Optional[Mapping[str, Mapping[str, Optional[str]]]] = None) -> dict[str, Any]:
    """Record a valuation and the daily return used by the loss breaker."""
    _begin_account_transaction(db)
    valuation_day = valuation_date or _trade_date()
    if valuation_day > _trade_date():
        raise ValueError("valuation_date_in_future")
    account = db.query(PaperAccount).filter(PaperAccount.id == account_id).first()
    if not account:
        raise KeyError("paper account not found")
    account_market = normalize_market(account.market)
    # Normalize incoming symbols once so callers can use the same display
    # format as the market detail page (for example ``fund:110022`` or
    # lowercase US tickers) without ever mixing account currencies.
    normalized_prices: dict[str, float] = {}
    for symbol, value in (prices or {}).items():
        normalized_prices[normalize_symbol(account_market, symbol)] = float(value)
    normalized_metadata: dict[str, Mapping[str, Optional[str]]] = {}
    for symbol, metadata in (price_metadata or {}).items():
        if not isinstance(metadata, Mapping):
            raise ValueError("price_metadata must map symbols to objects")
        normalized_metadata[normalize_symbol(account_market, symbol)] = metadata
    positions = db.query(PaperAccountPosition).filter(PaperAccountPosition.account_id == account_id).all()
    persisted_metadata: dict[str, dict[str, Any]] = {}
    for position in positions:
        if normalize_market(position.market) != account_market:
            raise ValueError("account_position_market_mismatch")
        if position.code not in normalized_prices:
            raise ValueError(f"missing_price_for_position:{position.code}")
        metadata = normalized_metadata.get(position.code, {})
        position_source = str(metadata.get("source") or price_source)
        position_as_of = metadata.get("as_of", price_as_of)
        position_freshness = str(metadata.get("freshness") or price_freshness)
        _validate_quote(price=normalized_prices[position.code], price_source=position_source,
                        price_as_of=position_as_of, price_freshness=position_freshness,
                        market=account_market, code=position.code, trade_date=valuation_day, db=db)
        persisted_metadata[position.code] = {
            "price": normalized_prices[position.code],
            "source": position_source,
            "as_of": position_as_of,
            "freshness": position_freshness,
        }
        position.last_price = normalized_prices[position.code]
        position.market_value = position.shares * position.last_price
        position.last_price_source = position_source
        position.last_price_as_of = position_as_of
        position.last_price_freshness = position_freshness
    market_value = sum(item.market_value for item in positions)
    equity = account.cash + market_value
    previous = (db.query(PaperValuation)
                .filter(PaperValuation.account_id == account_id,
                       PaperValuation.valuation_date < valuation_day.isoformat())
                .order_by(PaperValuation.valuation_date.desc()).first())
    baseline_equity = previous.equity if previous and previous.equity > 0 else account.initial_capital
    daily_return = equity / baseline_equity - 1.0 if baseline_equity > 0 else 0.0
    row = (db.query(PaperValuation)
           .filter(PaperValuation.account_id == account_id,
                  PaperValuation.valuation_date == valuation_day.isoformat()).first())
    if row:
        row.cash, row.market_value, row.equity, row.daily_return = account.cash, market_value, equity, daily_return
        row.price_source, row.price_as_of, row.price_freshness = price_source, price_as_of, price_freshness
        row.price_metadata = json.dumps(persisted_metadata, ensure_ascii=False, sort_keys=True)
    else:
        row = PaperValuation(account_id=account_id, valuation_date=valuation_day.isoformat(),
                             cash=account.cash, market_value=market_value, equity=equity,
                             daily_return=daily_return, price_source=price_source,
                             price_as_of=price_as_of, price_freshness=price_freshness,
                             price_metadata=json.dumps(persisted_metadata, ensure_ascii=False, sort_keys=True))
        db.add(row)
    db.commit()
    db.refresh(row)
    return {"date": row.valuation_date, "cash": row.cash, "market_value": row.market_value,
            "total_value": row.equity, "daily_return": row.daily_return,
            "price_source": row.price_source, "price_as_of": row.price_as_of,
            "price_freshness": row.price_freshness,
            "price_metadata": json.loads(row.price_metadata or "{}"),
            "execution_mode": "paper_only", "paper_only": True, "live_execution": False}


def _order_dict(order: PaperOrder) -> dict[str, Any]:
    metadata = market_metadata(order.market)
    return {"id": order.id, "account_id": order.account_id,
            **metadata,
            "idempotency_key": order.idempotency_key, "code": order.code,
            "side": order.side, "quantity": order.quantity, "status": order.status,
            "filled_quantity": order.filled_quantity, "fill_price": order.fill_price,
            "reject_reason": order.reject_reason, "price_source": order.price_source,
            "price_as_of": order.price_as_of, "price_freshness": order.price_freshness,
            "execution_mode": "paper_only", "paper_only": True, "live_execution": False,
            "created_at": order.created_at}
