"""Persistent, paper-only execution service for Phase 2.

The service deliberately keeps the broker boundary local. A paper order is
accepted with explicit quote provenance, written atomically to the ledger, and
can be replayed safely with the same idempotency key.
"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy.orm import Session

from quant_engine.data.calendar import TradingCalendar
from quant_engine.trading.gateway import OrderIntent, PaperBrokerGateway, RiskEngine, RiskLimits
from server.models.schema import (
    PaperAccount, PaperAccountPosition, PaperFill, PaperLedgerEvent,
    PaperLot, PaperOrder, PaperValuation,
)

COMMISSION_RATE = 0.00025
STAMP_DUTY_RATE = 0.001
MIN_COMMISSION = 5.0
MAX_QUOTE_AGE_SECONDS = 15 * 60
_account_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trade_date() -> date:
    return datetime.now().date()


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("price_as_of must be an ISO-8601 timestamp") from exc
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _validate_quote(*, price: float, price_source: str, price_as_of: Optional[str], price_freshness: str) -> None:
    if price <= 0:
        raise ValueError("price must be positive")
    source = (price_source or "").strip()
    freshness = (price_freshness or "").strip().lower()
    if not source:
        raise ValueError("price_source is required")
    if freshness in {"stale", "unknown", "expired"}:
        raise ValueError("quote_stale_or_unknown")
    parsed = _parse_iso(price_as_of)
    if freshness == "manual":
        if source != "manual_input":
            raise ValueError("manual quote must use price_source=manual_input")
        return
    if parsed is None:
        raise ValueError("quote_timestamp_required")
    age = (datetime.now(timezone.utc) - parsed).total_seconds()
    if age < -5 or age > MAX_QUOTE_AGE_SECONDS:
        raise ValueError("quote_stale_or_unknown")


def _next_unlock_date(buy_date: date) -> date:
    """Return the next A-share trading date; use weekday fallback if cache ends."""
    try:
        return TradingCalendar().next_trading_day(buy_date)
    except Exception:
        candidate = buy_date + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate


def _account_dict(account: PaperAccount) -> dict[str, Any]:
    return {
        "id": account.id, "name": account.name,
        "initial_capital": account.initial_capital, "cash": account.cash,
        "status": account.status,
        "risk": {"max_order_notional": account.max_order_notional,
                 "max_position_weight": account.max_position_weight,
                 "max_daily_loss": account.max_daily_loss},
        "created_at": account.created_at, "updated_at": account.updated_at,
    }


def create_account(db: Session, *, name: str, initial_capital: float,
                   max_order_notional: float = 100_000.0,
                   max_position_weight: float = 0.25,
                   max_daily_loss: float = 0.03) -> dict[str, Any]:
    if initial_capital <= 0:
        raise ValueError("initial_capital must be positive")
    if max_order_notional <= 0 or not 0 < max_position_weight <= 1 or max_daily_loss < 0:
        raise ValueError("invalid risk limits")
    account = PaperAccount(name=name, initial_capital=initial_capital, cash=initial_capital,
                           max_order_notional=max_order_notional,
                           max_position_weight=max_position_weight, max_daily_loss=max_daily_loss)
    db.add(account)
    db.commit()
    db.refresh(account)
    return _account_dict(account)


def list_accounts(db: Session) -> list[dict[str, Any]]:
    return [_account_dict(item) for item in db.query(PaperAccount).order_by(PaperAccount.created_at.desc()).all()]


def _latest_valuation(db: Session, account_id: str) -> Optional[PaperValuation]:
    return (db.query(PaperValuation).filter(PaperValuation.account_id == account_id)
            .order_by(PaperValuation.valuation_date.desc(), PaperValuation.created_at.desc()).first())


def account_snapshot(db: Session, account_id: str) -> dict[str, Any]:
    account = db.query(PaperAccount).filter(PaperAccount.id == account_id).first()
    if not account:
        raise KeyError("paper account not found")
    positions = db.query(PaperAccountPosition).filter(PaperAccountPosition.account_id == account_id).all()
    market_value = sum(item.market_value for item in positions)
    valuation = _latest_valuation(db, account_id)
    return {**_account_dict(account), "market_value": market_value,
            "equity": account.cash + market_value,
            "daily_return": valuation.daily_return if valuation else 0.0,
            "valuation_date": valuation.valuation_date if valuation else None,
            "positions": [{"code": item.code, "shares": item.shares,
                           "avg_cost": item.avg_cost, "market_value": item.market_value,
                           "last_price": item.last_price}
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
    return [{"id": item.id, "event_type": item.event_type, "reference_id": item.reference_id,
             "amount": item.amount, "payload": json.loads(item.payload or "{}"),
             "created_at": item.created_at} for item in rows]


def list_valuations(db: Session, account_id: str, limit: int = 60) -> list[dict[str, Any]]:
    if not db.query(PaperAccount.id).filter(PaperAccount.id == account_id).first():
        raise KeyError("paper account not found")
    rows = (db.query(PaperValuation).filter(PaperValuation.account_id == account_id)
            .order_by(PaperValuation.valuation_date.desc(), PaperValuation.created_at.desc())
            .limit(limit).all())
    return [{"date": row.valuation_date, "cash": row.cash, "market_value": row.market_value,
             "total_value": row.equity, "daily_return": row.daily_return,
             "price_source": row.price_source, "price_freshness": row.price_freshness}
            for row in reversed(rows)]


def account_report(db: Session, account_id: str) -> dict[str, Any]:
    """Build a beginner-readable account report from durable checkpoints."""
    account = db.query(PaperAccount).filter(PaperAccount.id == account_id).first()
    if not account:
        raise KeyError("paper account not found")
    rows = (db.query(PaperValuation).filter(PaperValuation.account_id == account_id)
            .order_by(PaperValuation.valuation_date.asc()).all())
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
        "initial_capital": account.initial_capital,
        "equity": last.equity if last else account.cash,
        "total_return": ((last.equity / account.initial_capital) - 1.0) if last and account.initial_capital > 0 else 0.0,
        "max_drawdown": max_drawdown,
        "valuation_count": len(rows),
        "order_count": order_count,
        "fill_count": fill_count,
        "last_valuation_date": last.valuation_date if last else None,
    }


def _ensure_legacy_lot(db: Session, account_id: str, code: str,
                       position: PaperAccountPosition, trade_date: date) -> list[PaperLot]:
    lots = (db.query(PaperLot).filter(PaperLot.account_id == account_id,
                                     PaperLot.code == code,
                                     PaperLot.remaining_quantity > 0)
            .order_by(PaperLot.buy_at.asc()).all())
    if lots or position.shares <= 0:
        return lots
    # Existing Phase-1 positions have no lot history. They are already settled
    # because the legacy engine only exposed end-of-day holdings.
    legacy = PaperLot(account_id=account_id, code=code, quantity=position.shares,
                      remaining_quantity=position.shares, buy_price=position.avg_cost,
                      buy_at=f"{trade_date.isoformat()}T00:00:00+00:00",
                      unlock_date=trade_date.isoformat())
    db.add(legacy)
    db.flush()
    return [legacy]


def _available_lots(db: Session, account_id: str, code: str, trade_date: date,
                    position: Optional[PaperAccountPosition]) -> tuple[int, list[PaperLot]]:
    lots = _ensure_legacy_lot(db, account_id, code, position, trade_date) if position else []
    available = sum(item.remaining_quantity for item in lots if item.unlock_date <= trade_date.isoformat())
    return available, lots


def _consume_lots(lots: list[PaperLot], quantity: int, trade_date: date) -> None:
    remaining = quantity
    for lot in sorted(lots, key=lambda item: item.buy_at):
        if lot.unlock_date > trade_date.isoformat() or lot.remaining_quantity <= 0:
            continue
        consumed = min(remaining, lot.remaining_quantity)
        lot.remaining_quantity -= consumed
        remaining -= consumed
        if remaining == 0:
            return
    if remaining:
        raise ValueError("insufficient_settled_position")


def submit_order(db: Session, *, account_id: str, idempotency_key: str, code: str,
                 side: str, quantity: int, price: float,
                 price_source: str = "manual_input", price_as_of: Optional[str] = None,
                 price_freshness: str = "manual") -> dict[str, Any]:
    """Submit one paper order atomically from the user's perspective."""
    _validate_quote(price=price, price_source=price_source, price_as_of=price_as_of,
                    price_freshness=price_freshness)
    with _account_lock:
        existing = db.query(PaperOrder).filter(PaperOrder.idempotency_key == idempotency_key).first()
        if existing:
            same = (existing.account_id == account_id and existing.code == code
                    and existing.side == side and existing.quantity == quantity
                    and existing.limit_price is not None
                    and abs(existing.limit_price - price) <= 1e-9)
            if not same:
                raise ValueError("idempotency_key already belongs to a different order")
            return _order_dict(existing)

        account = db.query(PaperAccount).filter(PaperAccount.id == account_id).first()
        if not account:
            raise KeyError("paper account not found")
        if account.status != "active":
            raise ValueError("paper account is not active")
        rows = db.query(PaperAccountPosition).filter(PaperAccountPosition.account_id == account_id).all()
        position = next((item for item in rows if item.code == code), None)
        trade_date = _trade_date()
        available, lots = _available_lots(db, account_id, code, trade_date, position)
        if side == "sell" and quantity > available:
            total = position.shares if position else 0
            reason = "t_plus_one_lock" if total >= quantity and available < quantity else "insufficient_settled_position"
            order = PaperOrder(account_id=account_id, idempotency_key=idempotency_key, code=code,
                               side=side, quantity=quantity, status="rejected", limit_price=price,
                               filled_quantity=0, reject_reason=reason, price_source=price_source,
                               price_as_of=price_as_of, price_freshness=price_freshness)
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
        gateway.set_price(code, price)
        notional = quantity * price
        commission = max(notional * COMMISSION_RATE, MIN_COMMISSION)
        stamp = notional * STAMP_DUTY_RATE if side == "sell" else 0.0
        intent = OrderIntent(idempotency_key, code, side, quantity, price)
        reason = risk.check(intent, reference_price=price, equity=equity,
                            current_position_value=(position.shares * price if position else 0.0),
                            daily_return=daily_return)
        if side == "buy" and notional + commission > account.cash + 1e-9:
            reason = reason or "insufficient_cash_including_fees"
        report = None if reason else gateway.submit(intent)
        status, filled = (("rejected", 0) if reason else (report.status.value, report.filled_quantity))
        reject_reason = reason or (report.reason if report else None)
        order = PaperOrder(account_id=account_id, idempotency_key=idempotency_key, code=code,
                           side=side, quantity=quantity, status=status, limit_price=price,
                           filled_quantity=filled, fill_price=report.fill_price if report else None,
                           reject_reason=reject_reason, price_source=price_source,
                           price_as_of=price_as_of, price_freshness=price_freshness)
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
                    position = PaperAccountPosition(account_id=account_id, code=code, shares=filled,
                                                    avg_cost=(gross + fees) / filled, last_price=fill_price,
                                                    market_value=gross)
                    db.add(position)
                unlock = _next_unlock_date(trade_date).isoformat()
                db.add(PaperLot(account_id=account_id, code=code, quantity=filled,
                                remaining_quantity=filled, buy_price=fill_price, buy_at=_now(),
                                unlock_date=unlock))
            else:
                _consume_lots(lots, filled, trade_date)
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
            db.add(PaperFill(order_id=order.id, account_id=account_id, code=code, side=side,
                             quantity=filled, price=fill_price, amount=gross))
            unlock = _next_unlock_date(trade_date).isoformat() if side == "buy" else None
            db.add(PaperLedgerEvent(account_id=account_id, event_type="fill", reference_id=order.id,
                                    amount=(-gross - fees if side == "buy" else gross - fees),
                                    payload=json.dumps({"code": code, "side": side,
                                                        "quantity": filled, "price": fill_price,
                                                        "fees": fees, "price_source": price_source,
                                                        "price_as_of": price_as_of,
                                                        "price_freshness": price_freshness,
                                                        "unlock_date": unlock})))
        account.updated_at = _now()
        db.commit()
        db.refresh(order)
        return _order_dict(order)


def mark_to_market(db: Session, *, account_id: str, prices: Mapping[str, float],
                   valuation_date: Optional[date] = None,
                   price_source: str = "manual_input", price_as_of: Optional[str] = None,
                   price_freshness: str = "manual") -> dict[str, Any]:
    with _account_lock:
        return _mark_to_market_unlocked(db, account_id=account_id, prices=prices,
                                        valuation_date=valuation_date,
                                        price_source=price_source, price_as_of=price_as_of,
                                        price_freshness=price_freshness)


def _mark_to_market_unlocked(db: Session, *, account_id: str, prices: Mapping[str, float],
                             valuation_date: Optional[date] = None,
                             price_source: str = "manual_input", price_as_of: Optional[str] = None,
                             price_freshness: str = "manual") -> dict[str, Any]:
    """Record a valuation and the daily return used by the loss breaker."""
    valuation_day = valuation_date or _trade_date()
    if valuation_day > _trade_date():
        raise ValueError("valuation_date_in_future")
    account = db.query(PaperAccount).filter(PaperAccount.id == account_id).first()
    if not account:
        raise KeyError("paper account not found")
    positions = db.query(PaperAccountPosition).filter(PaperAccountPosition.account_id == account_id).all()
    for position in positions:
        if position.code not in prices:
            raise ValueError(f"missing_price_for_position:{position.code}")
        _validate_quote(price=float(prices[position.code]), price_source=price_source,
                        price_as_of=price_as_of, price_freshness=price_freshness)
        position.last_price = float(prices[position.code])
        position.market_value = position.shares * position.last_price
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
    else:
        row = PaperValuation(account_id=account_id, valuation_date=valuation_day.isoformat(),
                             cash=account.cash, market_value=market_value, equity=equity,
                             daily_return=daily_return, price_source=price_source,
                             price_as_of=price_as_of, price_freshness=price_freshness)
        db.add(row)
    db.commit()
    db.refresh(row)
    return {"date": row.valuation_date, "cash": row.cash, "market_value": row.market_value,
            "total_value": row.equity, "daily_return": row.daily_return,
            "price_source": row.price_source, "price_freshness": row.price_freshness}


def _order_dict(order: PaperOrder) -> dict[str, Any]:
    return {"id": order.id, "account_id": order.account_id,
            "idempotency_key": order.idempotency_key, "code": order.code,
            "side": order.side, "quantity": order.quantity, "status": order.status,
            "filled_quantity": order.filled_quantity, "fill_price": order.fill_price,
            "reject_reason": order.reject_reason, "price_source": order.price_source,
            "price_as_of": order.price_as_of, "price_freshness": order.price_freshness,
            "created_at": order.created_at}
