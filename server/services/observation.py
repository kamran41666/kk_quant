"""Paper-only strategy observation lifecycle.

An observation is deliberately a small control plane around the existing
persistent paper-order service.  It never imports a broker adapter and every
automatic order goes through ``submit_order`` so the existing A-share lot,
T+1, cash, position and quote-freshness checks remain authoritative.
"""
from __future__ import annotations

import json
import hashlib
import math
import re
import threading
from uuid import uuid4
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional, Protocol

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from quant_engine.backtest.context import StrategyContext
from quant_engine.backtest.data_handler import DataHandler
from quant_engine.data.api import DataAPI
from quant_engine.data.calendar import TradingCalendar
from quant_engine.data.live import AKShareLiveMarketDataProvider, LiveMarketDataProvider, MarketQuote, SHANGHAI_TZ
from server.models.schema import (
    PaperAccount,
    PaperAccountPosition,
    PaperLot,
    PaperOrder,
    Run,
    Strategy,
    StrategyObservation,
    StrategyObservationEvent,
    PaperRebalanceItem,
    PaperRebalancePlan,
)
from server.services.paper_market_rules import A_SHARE, CN_FUND, fee_for, is_market_trading_day, market_session_status, normalize_market, normalize_symbol
from server.services.paper_trading import submit_order
from server.services.strategy_evidence import manifest_is_complete, strategy_fingerprint

_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
_DURATIONS = frozenset({7, 30})
_ACTIVE = frozenset({"running", "paused"})
_TERMINAL = frozenset({"stopped", "completed"})
_observation_lock = threading.RLock()


def _trade_date() -> date:
    """Return the application trading date in the configured CN timezone."""
    return datetime.now(SHANGHAI_TZ).date()


def _verified_trading_calendar(day: date) -> TradingCalendar:
    """Load a dated exchange calendar and fail closed when provenance is weak."""
    calendar = TradingCalendar(start_year=day.year, end_year=day.year)
    report = calendar.ensure_coverage(day, day)
    if not report.get("complete"):
        raise ObservationBlocked(
            "TRADING_CALENDAR_UNAVAILABLE",
            "A verified mainland trading calendar is required for paper observation.",
        )
    return calendar


class ObservationBlocked(RuntimeError):
    """A safe, expected inability to produce a live observation tick."""

    def __init__(self, code: str, reason: str):
        self.code = code
        self.reason = reason
        super().__init__(reason)


class QuoteProvider(Protocol):
    def fetch_quotes(self, codes: list[str]) -> list[MarketQuote]: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _observation_dict(row: StrategyObservation) -> dict[str, Any]:
    try:
        managed_codes = json.loads(getattr(row, "managed_codes", "[]") or "[]")
    except (TypeError, ValueError):
        managed_codes = []
    if not isinstance(managed_codes, list):
        managed_codes = []
    observation_market = normalize_market(getattr(row, "market", None))
    managed_codes = sorted({str(code) for code in managed_codes if _valid_code(observation_market, str(code))})
    try:
        pending = json.loads(getattr(row, "pending_signals", "{}") or "{}")
    except (TypeError, ValueError):
        pending = {}
    if not isinstance(pending, dict):
        pending = {}
    return {
        "id": row.id,
        "account_id": row.account_id,
        "strategy_id": row.strategy_id,
        "backtest_run_id": getattr(row, "backtest_run_id", None),
        "market": getattr(row, "market", None),
        "strategy_fingerprint": getattr(row, "strategy_fingerprint", None),
        "pending_signal_date": getattr(row, "pending_signal_date", None),
        "pending_signal_count": len(pending),
        "duration_days": row.duration_days,
        "allocation_pct": row.allocation_pct,
        "allocated_capital": row.allocated_capital,
        "start_date": row.start_date,
        "end_date": row.end_date,
        "status": row.status,
        "auto_trade": bool(row.auto_trade),
        "started_at": row.started_at,
        "paused_at": row.paused_at,
        "stopped_at": row.stopped_at,
        "last_tick_date": row.last_tick_date,
        "last_tick_at": row.last_tick_at,
        "last_tick_status": row.last_tick_status,
        "last_error": row.last_error,
        "managed_codes": managed_codes,
        "manual_positions_protected": True,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        # Explicitly expose the execution boundary to a beginner-facing UI.
        "execution_mode": "paper_only",
        "live_execution": False,
    }


def _managed_codes(row: StrategyObservation) -> set[str]:
    """Read the strategy-owned universe, failing safe on old/corrupt rows."""
    try:
        value = json.loads(getattr(row, "managed_codes", "[]") or "[]")
    except (TypeError, ValueError):
        return set()
    if not isinstance(value, list):
        return set()
    market = normalize_market(getattr(row, "market", None))
    return {str(code) for code in value if _valid_code(market, str(code))}


def _valid_code(market: str, code: str) -> bool:
    if market == A_SHARE:
        return bool(_CODE_RE.fullmatch(code))
    if market == CN_FUND:
        try:
            normalize_symbol(CN_FUND, code)
            return True
        except ValueError:
            return False
    return False


def _pending_signals(row: StrategyObservation) -> dict[str, float]:
    """Read the durable D-day target, rejecting corrupt state safely."""
    try:
        value = json.loads(getattr(row, "pending_signals", "{}") or "{}")
    except (TypeError, ValueError) as exc:
        raise ObservationBlocked("INVALID_PENDING_SIGNAL", "The stored target weights are not valid JSON.") from exc
    if not isinstance(value, dict):
        raise ObservationBlocked("INVALID_PENDING_SIGNAL", "The stored target weights are not an object.")
    market = normalize_market(getattr(row, "market", None))
    normalized: dict[str, float] = {}
    for code, weight in value.items():
        try:
            numeric = float(weight)
        except (TypeError, ValueError) as exc:
            raise ObservationBlocked("INVALID_PENDING_SIGNAL", "The stored target weight is not numeric.") from exc
        if not _valid_code(market, str(code)) or not math.isfinite(numeric) or numeric < 0:
            raise ObservationBlocked("INVALID_PENDING_SIGNAL", "The stored target weight is invalid.")
        normalized[str(code)] = numeric
    if sum(normalized.values()) > 1.0 + 1e-9:
        raise ObservationBlocked("INVALID_PENDING_SIGNAL", "The stored target weights exceed 100%.")
    return normalized


def _strategy_shares(db: Session, account_id: str, code: str, observation_id: str) -> float:
    """Return lots owned by this exact observation, never another strategy."""
    rows = (db.query(PaperLot)
            .filter(PaperLot.account_id == account_id,
                    PaperLot.code == code,
                    PaperLot.remaining_quantity > 0,
                    PaperLot.owner == "strategy",
                    PaperLot.owner_id == observation_id)
            .all())
    return sum(float(row.remaining_quantity) for row in rows)


def _fund_rebalance_plan_id(observation_id: str, signal_date: str, execution_date: str) -> str:
    """Return a stable plan id for one D-day -> next-NAV window."""
    raw = f"{observation_id}|{signal_date}|{execution_date}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:36]


def _get_or_create_rebalance_plan(
    db: Session,
    *,
    observation: StrategyObservation,
    account_id: str,
    market: str,
    signal_date: str,
    execution_date: str,
    pending: dict[str, float],
    quotes: dict[str, MarketQuote],
) -> PaperRebalancePlan:
    """Persist the complete order proposal before submitting any order.

    The proposal captures the price evidence and a deterministic idempotency
    key.  If the worker stops after one fill, the next tick reuses the same
    items and ``submit_order`` replays the existing order instead of creating
    a duplicate.
    """
    plan_id = _fund_rebalance_plan_id(observation.id, signal_date, execution_date)
    plan = db.query(PaperRebalancePlan).filter(PaperRebalancePlan.id == plan_id).first()
    if plan:
        return plan
    # A worker can fail after creating or partially executing yesterday's
    # plan. Reuse that durable envelope on the next valid checkpoint, but
    # refresh only still-pending items with new quote evidence and date.
    active = (db.query(PaperRebalancePlan)
              .filter(PaperRebalancePlan.observation_id == observation.id,
                      PaperRebalancePlan.signal_date == signal_date,
                      PaperRebalancePlan.status.in_(["planned", "partial", "executing", "blocked"]))
              .order_by(PaperRebalancePlan.updated_at.desc())
              .first())
    # A blocked plan is an explicit operator decision point. Keep the same
    # durable envelope across later market days so the scheduler cannot
    # silently fork a new attempt; only the retry endpoint may reset it.
    if active is not None and active.status == "blocked":
        return active
    if active is not None and active.execution_date != execution_date:
        # Do not fork a second plan while another worker still owns the old
        # execution lease. The caller will observe ``executing`` and retry on
        # the next scheduler tick; only an expired lease may be refreshed.
        if (active.status == "executing" and active.lease_until
                and active.lease_until >= _now()):
            return active
        items = db.query(PaperRebalanceItem).filter(PaperRebalanceItem.plan_id == active.id).all()
        # Reconcile a paper order that committed immediately before a worker
        # crashed. The old idempotency key is authoritative for that attempt;
        # it must be claimed as completed (or blocked) before any new quote is
        # considered, otherwise a changed price would make replay look like a
        # conflicting order and strand the plan forever.
        for item in items:
            if item.status != "pending":
                continue
            existing_order = (db.query(PaperOrder)
                              .filter(PaperOrder.idempotency_key == item.idempotency_key)
                              .first())
            if existing_order is None:
                continue
            item.order_id = existing_order.id
            item.filled_quantity = float(existing_order.filled_quantity or 0.0)
            if existing_order.status == "filled":
                item.status = "completed"
                item.error = None
            else:
                item.status = "blocked"
                item.error = str(existing_order.reject_reason or "paper_order_rejected")
            item.updated_at = _now()
        pending_items = [item for item in items if item.status == "pending"]
        blocked_items = [item for item in items if item.status == "blocked"]
        for item in pending_items:
            quote = quotes.get(item.code)
            if quote is None:
                raise ObservationBlocked("REBALANCE_QUOTE_INCOMPLETE", "A pending plan item has no refreshed quote evidence.")
            item.price = float(quote.price)
            item.price_source = str(quote.source)
            item.price_as_of = quote.as_of
            item.price_freshness = str(quote.freshness)
            item.updated_at = _now()
        active.execution_date = execution_date if pending_items else active.execution_date
        all_items = db.query(PaperRebalanceItem).filter(PaperRebalanceItem.plan_id == active.id).all()
        active.filled_count = sum(1 for item in all_items if item.status == "completed")
        active.status = "partial" if pending_items else ("blocked" if blocked_items or any(item.status == "blocked" for item in all_items) else "completed")
        active.last_error = None
        active.updated_at = _now()
        db.commit()
        db.refresh(active)
        return active
    plan = PaperRebalancePlan(
        id=plan_id,
        observation_id=observation.id,
        account_id=account_id,
        market=normalize_market(market),
        signal_date=signal_date,
        execution_date=execution_date,
        status="planned",
        order_count=0,
        filled_count=0,
    )
    db.add(plan)
    try:
        db.flush()
    except IntegrityError:
        # Two scheduler workers may observe the missing deterministic plan at
        # the same time.  The unique primary key makes one winner; the other
        # rolls back its provisional row and resumes the already committed
        # plan instead of producing a second proposal.
        db.rollback()
        existing = db.query(PaperRebalancePlan).filter(PaperRebalancePlan.id == plan_id).first()
        if existing is not None:
            return existing
        raise
    proposals: list[dict[str, Any]] = []
    buy_total = 0.0
    sell_proceeds = 0.0
    account = db.query(PaperAccount).filter(PaperAccount.id == account_id).first()
    if account is None:
        raise KeyError("paper account not found")
    positions = db.query(PaperAccountPosition).filter(PaperAccountPosition.account_id == account_id).all()
    equity = float(account.cash) + sum(float(item.market_value) for item in positions)
    for code in sorted(pending):
        quote = quotes[code]
        current = _strategy_shares(db, account_id, code, observation.id)
        target_amount = max(0.0, observation.allocated_capital * pending.get(code, 0.0))
        if normalize_market(market) == CN_FUND:
            target = math.floor(target_amount / float(quote.price) * 100.0 + 1e-9) / 100.0
        else:
            target = max(0, int(target_amount / float(quote.price) / 100) * 100)
        diff = round(target - current, 2)
        item_status = "skipped" if abs(diff) < 0.005 else "pending"
        side = "buy" if diff > 0 else "sell"
        notional = abs(diff) * float(quote.price)
        if item_status == "pending":
            commission, stamp = fee_for(market, notional, side)
            if side == "buy":
                buy_total += notional + commission + stamp
            else:
                sell_proceeds += max(0.0, notional - commission - stamp)
        proposals.append({"code": code, "side": side, "quantity": abs(diff), "price": float(quote.price), "source": str(quote.source), "as_of": quote.as_of, "freshness": str(quote.freshness), "status": item_status})
    # Preflight the complete proposal before any order can mutate the ledger.
    # Selling first below handles a rebalance that frees cash for buys; a
    # plan still blocks when even that conservative projected cash is short.
    if buy_total > float(account.cash) + sell_proceeds + 1e-6:
        db.rollback()
        raise ObservationBlocked("REBALANCE_CASH_PREFLIGHT", "The rebalance needs more cash than the account can release after fees.")
    max_position_value = max(0.0, float(account.max_position_weight) * equity)
    existing_position_values = {str(item.code): max(0.0, float(item.shares) * float(item.last_price or item.avg_cost or 0.0)) for item in positions}
    for proposal in proposals:
        projected_value = existing_position_values.get(proposal["code"], 0.0)
        if proposal["status"] == "pending":
            delta = proposal["quantity"] * proposal["price"] * (1.0 if proposal["side"] == "buy" else -1.0)
            projected_value = max(0.0, projected_value + delta)
        if proposal["status"] == "pending" and proposal["side"] == "buy" and projected_value > max_position_value + 1e-6:
            db.rollback()
            raise ObservationBlocked("REBALANCE_POSITION_PREFLIGHT", "A strategy target exceeds the account position limit.")
    actionable = 0
    for proposal in sorted(proposals, key=lambda item: (item["side"] != "sell", item["code"])):
        if proposal["status"] == "pending":
            actionable += 1
        item = PaperRebalanceItem(
            plan_id=plan.id, code=proposal["code"], side=proposal["side"],
            quantity=proposal["quantity"], price=proposal["price"],
            price_source=proposal["source"], price_as_of=proposal["as_of"],
            price_freshness=proposal["freshness"],
            idempotency_key=f"observation-{observation.id}-{signal_date}-{execution_date}-{proposal['code']}-{proposal['side']}",
            status=proposal["status"], filled_quantity=0.0,
        )
        db.add(item)
    plan.order_count = actionable
    plan.status = "completed" if actionable == 0 else "planned"
    plan.updated_at = _now()
    db.commit()
    db.refresh(plan)
    return plan


def _get_or_create_fund_rebalance_plan(
    db: Session,
    *,
    observation: StrategyObservation,
    account_id: str,
    signal_date: str,
    execution_date: str,
    pending: dict[str, float],
    quotes: dict[str, MarketQuote],
) -> PaperRebalancePlan:
    """Backward-compatible wrapper for the domestic-fund plan path."""
    return _get_or_create_rebalance_plan(
        db, observation=observation, account_id=account_id, market=CN_FUND,
        signal_date=signal_date, execution_date=execution_date,
        pending=pending, quotes=quotes,
    )


def _get_or_create_a_share_rebalance_plan(
    db: Session,
    *,
    observation: StrategyObservation,
    account_id: str,
    signal_date: str,
    execution_date: str,
    pending: dict[str, float],
    quotes: dict[str, MarketQuote],
) -> PaperRebalancePlan:
    """Create an A-share plan with the same preflight and recovery contract."""
    return _get_or_create_rebalance_plan(
        db, observation=observation, account_id=account_id, market=A_SHARE,
        signal_date=signal_date, execution_date=execution_date,
        pending=pending, quotes=quotes,
    )


def _execute_rebalance_plan(db: Session, plan: PaperRebalancePlan) -> list[dict[str, Any]]:
    """Resume a durable plan item-by-item with idempotent retries.

    Claiming is an atomic conditional update. This matters when the daily
    scheduler runs in more than one process: only the worker that owns the
    unexpired lease may submit orders. The lease is renewed between items and
    released for every terminal/partial result; a crashed worker is reclaimed
    after the short timeout.
    """
    if plan.status == "completed":
        return []
    worker = f"paper-rebalance-{uuid4().hex}"
    now = _now()
    lease_until = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    claimed = db.execute(
        update(PaperRebalancePlan)
        .where(
            PaperRebalancePlan.id == plan.id,
            PaperRebalancePlan.status != "completed",
            or_(
                PaperRebalancePlan.lease_owner.is_(None),
                PaperRebalancePlan.lease_until.is_(None),
                PaperRebalancePlan.lease_until < now,
                PaperRebalancePlan.lease_owner == worker,
            ),
        )
        .values(lease_owner=worker, lease_until=lease_until, status="executing", updated_at=now)
    )
    if claimed.rowcount != 1:
        # Another scheduler owns the plan. Keep the durable status intact so
        # the caller reports a retryable partial state without submitting.
        db.rollback()
        db.refresh(plan)
        return []
    db.refresh(plan)
    # Preserve a prior rejection reason while allowing pending items to retry.
    if not any(item.status == "blocked" for item in db.query(PaperRebalanceItem).filter(PaperRebalanceItem.plan_id == plan.id).all()):
        plan.last_error = None
    plan.updated_at = _now()
    plan.lease_until = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    db.commit()
    results: list[dict[str, Any]] = []
    items = (db.query(PaperRebalanceItem)
             .filter(PaperRebalanceItem.plan_id == plan.id)
             .order_by(PaperRebalanceItem.id.asc()).all())
    for item in items:
        if item.status in {"completed", "skipped", "blocked"}:
            continue
        try:
            # Heartbeat immediately before every paper order. The conditional
            # update also detects a lease takeover after a long interruption.
            renewed_until = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
            renewed = db.execute(
                update(PaperRebalancePlan)
                .where(PaperRebalancePlan.id == plan.id, PaperRebalancePlan.lease_owner == worker)
                .values(lease_until=renewed_until, updated_at=_now())
            )
            if renewed.rowcount != 1:
                db.rollback()
                db.refresh(plan)
                return results
            db.commit()
            result = submit_order(
                db,
                account_id=plan.account_id,
                idempotency_key=item.idempotency_key,
                market=normalize_market(plan.market),
                code=item.code,
                side=item.side,
                quantity=item.quantity,
                price=item.price,
                price_source=item.price_source,
                price_as_of=item.price_as_of,
                price_freshness=item.price_freshness,
                trade_date=date.fromisoformat(plan.execution_date),
                lot_owner="strategy",
                lot_owner_id=plan.observation_id,
            )
            results.append(result)
            item.order_id = result.get("id")
            item.filled_quantity = float(result.get("filled_quantity") or 0.0)
            if result.get("status") == "filled":
                item.status = "completed"
                item.error = None
            else:
                item.status = "blocked"
                item.error = str(result.get("reject_reason") or "paper_order_rejected")
                plan.last_error = item.error
        except Exception as exc:
            # Keep transient failures pending.  A later scheduler tick can
            # resume this exact item; the idempotency key protects a fill that
            # happened just before a process crash.
            item.status = "pending"
            item.error = f"{type(exc).__name__}: {exc}"
            plan.last_error = item.error
        item.updated_at = _now()
        db.commit()
    refreshed = db.query(PaperRebalanceItem).filter(PaperRebalanceItem.plan_id == plan.id).all()
    plan.filled_count = sum(1 for item in refreshed if item.status == "completed")
    if any(item.status == "pending" for item in refreshed):
        plan.status = "partial"
    elif any(item.status == "blocked" for item in refreshed):
        plan.status = "blocked"
        plan.last_error = next((item.error for item in refreshed if item.status == "blocked" and item.error), "paper_order_rejected")
    else:
        plan.status = "completed"
        plan.last_error = None
    plan.lease_owner = None
    plan.lease_until = None
    plan.updated_at = _now()
    db.commit()
    db.refresh(plan)
    return results


def _execute_fund_rebalance_plan(db: Session, plan: PaperRebalancePlan) -> list[dict[str, Any]]:
    """Backward-compatible name for the shared plan executor."""
    return _execute_rebalance_plan(db, plan)


def retry_rebalance_plan(
    db: Session,
    *,
    account_id: str,
    observation_id: str,
    plan_id: str,
) -> dict[str, Any]:
    """Reset rejected plan items for an explicit operator retry.

    A retry receives a new idempotency key because the original attempt may
    already be stored as a rejected paper order. The original order and plan
    item remain in the audit trail; only the item becomes eligible for the
    next scheduler tick.
    """
    _load_row(db, account_id, observation_id)
    plan = (db.query(PaperRebalancePlan)
            .filter(PaperRebalancePlan.id == plan_id,
                    PaperRebalancePlan.account_id == account_id,
                    PaperRebalancePlan.observation_id == observation_id)
            .first())
    if plan is None:
        raise KeyError("rebalance plan not found")
    if plan.status != "blocked":
        raise ValueError("rebalance_plan_not_blocked")
    items = db.query(PaperRebalanceItem).filter(PaperRebalanceItem.plan_id == plan.id).all()
    reset = 0
    for item in items:
        if item.status != "blocked":
            continue
        item.status = "pending"
        item.order_id = None
        item.filled_quantity = 0.0
        item.error = None
        item.idempotency_key = f"{item.idempotency_key}-retry-{uuid4().hex[:12]}"
        item.updated_at = _now()
        reset += 1
    if reset == 0:
        raise ValueError("rebalance_plan_has_no_blocked_items")
    plan.status = "partial"
    plan.last_error = None
    plan.lease_owner = None
    plan.lease_until = None
    plan.updated_at = _now()
    db.commit()
    db.refresh(plan)
    return {
        "id": plan.id,
        "account_id": plan.account_id,
        "observation_id": plan.observation_id,
        "market": normalize_market(plan.market),
        "signal_date": plan.signal_date,
        "execution_date": plan.execution_date,
        "status": plan.status,
        "reset_items": reset,
        "paper_only": True,
        "live_execution": False,
    }


def _event_dict(row: StrategyObservationEvent) -> dict[str, Any]:
    try:
        payload = json.loads(row.payload or "{}")
    except (TypeError, ValueError):
        payload = {"raw_payload": row.payload}
    return {
        "id": row.id,
        "observation_id": row.observation_id,
        "event_type": row.event_type,
        "event_date": row.event_date,
        "status": row.status,
        "signal_count": row.signal_count,
        "order_count": row.order_count,
        "reason": row.reason,
        "payload": payload,
        "created_at": row.created_at,
    }


def _add_event(
    db: Session,
    observation: StrategyObservation,
    *,
    event_type: str,
    status: str,
    event_date: Optional[date] = None,
    signal_count: int = 0,
    order_count: int = 0,
    reason: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> StrategyObservationEvent:
    event = StrategyObservationEvent(
        observation_id=observation.id,
        event_type=event_type,
        event_date=event_date.isoformat() if event_date else None,
        status=status,
        signal_count=signal_count,
        order_count=order_count,
        reason=reason,
        payload=json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
        created_at=_now(),
    )
    db.add(event)
    return event


def _require_account(db: Session, account_id: str) -> PaperAccount:
    account = db.query(PaperAccount).filter(PaperAccount.id == account_id).first()
    if not account:
        raise KeyError("paper account not found")
    if account.status != "active":
        raise ValueError("paper account is not active")
    return account


def _require_strategy_with_backtest(db: Session, strategy_id: str, *, account_market: Optional[str] = None) -> tuple[Strategy, Run]:
    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not strategy:
        raise KeyError("strategy not found")
    requested_market = normalize_market(account_market) if account_market else None
    strategy_market = normalize_market(getattr(strategy, "market", A_SHARE))
    if requested_market and strategy_market != requested_market:
        raise ValueError("strategy_market_mismatch")
    runs = (db.query(Run)
            .filter(Run.strategy_id == strategy_id, Run.run_type == "backtest", Run.status == "completed")
            .order_by(Run.created_at.desc()).all())
    if requested_market:
        runs = [row for row in runs if normalize_market(getattr(row, "market", None)) == requested_market]
    if not runs:
        raise ValueError("strategy_requires_completed_backtest")
    return strategy, runs[0]


def _validate_backtest_evidence(
    strategy: Strategy,
    run: Run,
    *,
    account_market: str,
) -> None:
    """Validate immutable evidence when a run contains the new fields.

    Legacy A-share runs created before evidence binding remain readable for
    backwards compatibility, but any partially populated evidence is treated
    as invalid rather than silently downgraded.
    """
    run_market = getattr(run, "market", None)
    fingerprint = getattr(run, "strategy_fingerprint", None)
    manifest = getattr(run, "data_manifest", None)
    has_evidence = any(value not in (None, "") for value in (run_market, fingerprint, manifest))
    if not has_evidence:
        if account_market != A_SHARE:
            raise ValueError("backtest_evidence_not_available")
        return
    if normalize_market(run_market) != account_market:
        raise ValueError("backtest_market_mismatch")
    current = strategy_fingerprint(strategy.strategy_class, strategy.params or "{}")
    if not fingerprint or fingerprint != current:
        raise ValueError("strategy_backtest_evidence_stale")
    if not manifest_is_complete(manifest) or not bool(getattr(run, "eligible_for_observation", False)):
        raise ValueError("backtest_evidence_not_eligible")


def _revalidate_observation_evidence(
    db: Session,
    observation: StrategyObservation,
    account: PaperAccount,
) -> None:
    """Re-check the exact run before starting or ticking an observation."""
    run_id = getattr(observation, "backtest_run_id", None)
    if not run_id:
        return
    run = db.query(Run).filter(Run.id == run_id).first()
    if run is None or run.status != "completed":
        raise ValueError("backtest_evidence_not_available")
    strategy = db.query(Strategy).filter(Strategy.id == observation.strategy_id).first()
    if strategy is None:
        raise KeyError("strategy not found")
    _validate_backtest_evidence(
        strategy,
        run,
        account_market=normalize_market(account.market),
    )


def create_observation(
    db: Session,
    *,
    account_id: str,
    strategy_id: str,
    idempotency_key: str,
    duration_days: int,
    allocation_pct: float = 1.0,
    allocated_capital: Optional[float] = None,
    auto_trade: bool = True,
    start_date: Optional[date] = None,
) -> dict[str, Any]:
    """Create one bounded observation, safely replayable by idempotency key."""
    if duration_days not in _DURATIONS:
        raise ValueError("duration_days must be 7 or 30")
    if not idempotency_key or len(idempotency_key) < 8 or len(idempotency_key) > 160:
        raise ValueError("idempotency_key must contain 8-160 characters")
    if not math.isfinite(float(allocation_pct)) or not 0 < allocation_pct <= 1:
        raise ValueError("allocation_pct must be within (0, 1]")

    account = _require_account(db, account_id)
    account_market = normalize_market(account.market)
    # Fund observations are enabled only for the dedicated NAV backtest
    # evidence path.  US remains fail-closed until its adjusted-price and
    # exchange-calendar evidence is implemented.
    if account_market == "us-equity":
        raise ValueError("strategy_observation_requires_a_share_account")
    if account_market == CN_FUND:
        candidate = db.query(Strategy).filter(Strategy.id == strategy_id).first()
        if candidate is None:
            raise KeyError("strategy not found")
        if normalize_market(getattr(candidate, "market", A_SHARE)) != CN_FUND:
            # Preserve the existing public boundary for an A-share strategy
            # accidentally selected from a fund account.
            raise ValueError("strategy_observation_requires_a_share_account")
    strategy, backtest_run = _require_strategy_with_backtest(db, strategy_id, account_market=account_market)
    _validate_backtest_evidence(strategy, backtest_run, account_market=account_market)
    if allocated_capital is None:
        capital = account.initial_capital * float(allocation_pct)
    else:
        capital = float(allocated_capital)
        if not math.isfinite(capital) or capital <= 0:
            raise ValueError("allocated_capital must be positive and finite")
        if capital > account.initial_capital + 1e-9:
            raise ValueError("allocated_capital cannot exceed account initial capital")
        # The stored percentage is always an auditable ratio of the account,
        # avoiding two conflicting sources of truth in later sizing decisions.
        allocation_pct = capital / account.initial_capital

    begin = start_date or _trade_date()
    if begin > _trade_date():
        raise ValueError("start_date_in_future")

    # Idempotency must cover every behavior-changing input.  Replaying a key
    # with a different start date or auto-trade switch must never silently
    # return the old task as if the request had been accepted.
    existing = db.query(StrategyObservation).filter(StrategyObservation.idempotency_key == idempotency_key).first()
    if existing:
        same = (
            existing.account_id == account_id
            and existing.strategy_id == strategy_id
            and existing.duration_days == duration_days
            and abs(existing.allocation_pct - float(allocation_pct)) <= 1e-9
            and abs(existing.allocated_capital - capital) <= 1e-9
            and existing.start_date == begin.isoformat()
            and bool(existing.auto_trade) == bool(auto_trade)
        )
        if not same:
            raise ValueError("idempotency_key already belongs to a different observation")
        return _observation_dict(existing)

    finish = begin + timedelta(days=duration_days - 1)
    stamp = _now()
    row = StrategyObservation(
        account_id=account_id,
        strategy_id=strategy_id,
        idempotency_key=idempotency_key,
        duration_days=duration_days,
        allocation_pct=float(allocation_pct),
        allocated_capital=capital,
        start_date=begin.isoformat(),
        end_date=finish.isoformat(),
        status="draft",
        auto_trade=bool(auto_trade),
        backtest_run_id=backtest_run.id,
        market=account_market,
        strategy_fingerprint=getattr(backtest_run, "strategy_fingerprint", None),
        managed_codes="[]",
        created_at=stamp,
        updated_at=stamp,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        # Cross-process callers can race between the idempotency lookup and
        # INSERT.  Resolve the unique-key winner as a normal replay.
        db.rollback()
        existing = db.query(StrategyObservation).filter(StrategyObservation.idempotency_key == idempotency_key).first()
        if existing is not None:
            same = (
                existing.account_id == account_id
                and existing.strategy_id == strategy_id
                and existing.duration_days == duration_days
                and abs(existing.allocation_pct - float(allocation_pct)) <= 1e-9
                and abs(existing.allocated_capital - capital) <= 1e-9
                and existing.start_date == begin.isoformat()
                and bool(existing.auto_trade) == bool(auto_trade)
            )
            if same:
                return _observation_dict(existing)
            raise ValueError("idempotency_key already belongs to a different observation")
        raise
    _add_event(db, row, event_type="created", status="draft", payload={"paper_only": True})
    db.commit()
    db.refresh(row)
    return _observation_dict(row)


def list_observations(db: Session, account_id: str) -> list[dict[str, Any]]:
    _require_account(db, account_id)
    rows = (
        db.query(StrategyObservation)
        .filter(StrategyObservation.account_id == account_id)
        .order_by(StrategyObservation.created_at.desc())
        .all()
    )
    result = []
    for row in rows:
        payload = _observation_dict(row)
        plan = (db.query(PaperRebalancePlan)
                .filter(PaperRebalancePlan.observation_id == row.id)
                .order_by(PaperRebalancePlan.updated_at.desc())
                .first())
        if plan:
            payload["latest_rebalance_plan_id"] = plan.id
            payload["latest_rebalance_plan_status"] = plan.status
        result.append(payload)
    return result


def get_observation(db: Session, account_id: str, observation_id: str) -> dict[str, Any]:
    _require_account(db, account_id)
    row = (
        db.query(StrategyObservation)
        .filter(StrategyObservation.id == observation_id, StrategyObservation.account_id == account_id)
        .first()
    )
    if not row:
        raise KeyError("observation not found")
    payload = _observation_dict(row)
    plan = (db.query(PaperRebalancePlan)
            .filter(PaperRebalancePlan.observation_id == row.id)
            .order_by(PaperRebalancePlan.updated_at.desc())
            .first())
    if plan:
        payload["latest_rebalance_plan_id"] = plan.id
        payload["latest_rebalance_plan_status"] = plan.status
    return payload


def list_observation_events(db: Session, account_id: str, observation_id: str, limit: int = 100) -> list[dict[str, Any]]:
    get_observation(db, account_id, observation_id)
    rows = (
        db.query(StrategyObservationEvent)
        .filter(StrategyObservationEvent.observation_id == observation_id)
        .order_by(StrategyObservationEvent.created_at.desc())
        .limit(max(1, min(limit, 500)))
        .all()
    )
    return [_event_dict(row) for row in reversed(rows)]


def _load_row(db: Session, account_id: str, observation_id: str) -> StrategyObservation:
    _require_account(db, account_id)
    row = (
        db.query(StrategyObservation)
        .filter(StrategyObservation.id == observation_id, StrategyObservation.account_id == account_id)
        .first()
    )
    if not row:
        raise KeyError("observation not found")
    return row


def _transition(
    db: Session,
    *,
    account_id: str,
    observation_id: str,
    action: str,
) -> dict[str, Any]:
    with _observation_lock:
        row = _load_row(db, account_id, observation_id)
        status = row.status
        if action == "start":
            account = _require_account(db, account_id)
            _revalidate_observation_evidence(db, row, account)
            if status == "draft":
                row.status = "running"
                row.started_at = _now()
                _add_event(db, row, event_type="started", status="running")
            elif status in {"running", "paused"}:
                if status == "paused":
                    row.status = "running"
                    row.paused_at = None
                    _add_event(db, row, event_type="resumed", status="running")
                # Starting a running observation is an idempotent no-op.
            else:
                raise ValueError(f"observation_is_{status}")
        elif action == "pause":
            if status == "running":
                row.status = "paused"
                row.paused_at = _now()
                _add_event(db, row, event_type="paused", status="paused")
            elif status in {"paused", "stopped", "completed"}:
                return _observation_dict(row)
            else:
                raise ValueError("observation_must_be_running")
        elif action == "resume":
            if status == "paused":
                row.status = "running"
                row.paused_at = None
                _add_event(db, row, event_type="resumed", status="running")
            elif status == "running":
                return _observation_dict(row)
            else:
                raise ValueError(f"observation_is_{status}")
        elif action == "stop":
            if status in {"draft", "running", "paused"}:
                row.status = "stopped"
                row.stopped_at = _now()
                _add_event(db, row, event_type="stopped", status="stopped")
            elif status in _TERMINAL:
                return _observation_dict(row)
            else:
                raise ValueError(f"observation_is_{status}")
        else:
            raise ValueError("unknown_observation_action")
        row.updated_at = _now()
        db.commit()
        db.refresh(row)
        return _observation_dict(row)


def start_observation(db: Session, account_id: str, observation_id: str) -> dict[str, Any]:
    return _transition(db, account_id=account_id, observation_id=observation_id, action="start")


def pause_observation(db: Session, account_id: str, observation_id: str) -> dict[str, Any]:
    return _transition(db, account_id=account_id, observation_id=observation_id, action="pause")


def resume_observation(db: Session, account_id: str, observation_id: str) -> dict[str, Any]:
    return _transition(db, account_id=account_id, observation_id=observation_id, action="resume")


def stop_observation(db: Session, account_id: str, observation_id: str) -> dict[str, Any]:
    return _transition(db, account_id=account_id, observation_id=observation_id, action="stop")


def _default_signal_provider(db: Session, observation: StrategyObservation, as_of: date) -> dict[str, float]:
    """Generate a signal only when a complete point-in-time local window exists."""
    strategy = db.query(Strategy).filter(Strategy.id == observation.strategy_id).first()
    if not strategy:
        raise ObservationBlocked("STRATEGY_NOT_FOUND", "The strategy definition is no longer available.")
    try:
        params = json.loads(strategy.params or "{}")
        from server.api.backtest import _import_strategy
        strategy_cls = _import_strategy(strategy.strategy_class)
        api = DataAPI()
        codes = api.index_components("000300", as_of)[:50]
        if not codes:
            raise ObservationBlocked("PIT_STOCK_POOL_EMPTY", "No point-in-time stock pool is available for this observation date.")
        handler = DataHandler(codes=codes, start=as_of - timedelta(days=90), end=as_of)
        if handler._daily_data.empty or as_of not in set(handler._daily_data.index.get_level_values("date")):
            raise ObservationBlocked("OBSERVATION_HISTORY_UNAVAILABLE", "A complete point-in-time history window is not available for this date.")
        context = StrategyContext(_verified_trading_calendar(as_of))
        context._data_handler = handler
        context.set_date(as_of)
        strategy_instance = strategy_cls(context, **params)
        strategy_instance.initialize()
        signals = strategy_instance.generate_signals(as_of)
    except ObservationBlocked:
        raise
    except Exception as exc:
        raise ObservationBlocked("STRATEGY_DATA_UNAVAILABLE", f"Strategy signal generation was blocked: {type(exc).__name__}.") from exc
    if not isinstance(signals, dict):
        raise ObservationBlocked("INVALID_STRATEGY_SIGNAL", "The strategy did not return a target-weight mapping.")
    normalized: dict[str, float] = {}
    for code, weight in signals.items():
        if not isinstance(code, str) or not _CODE_RE.fullmatch(code):
            raise ObservationBlocked("INVALID_STRATEGY_SIGNAL", "The strategy returned a non-A-share code.")
        try:
            value = float(weight)
        except (TypeError, ValueError) as exc:
            raise ObservationBlocked("INVALID_STRATEGY_SIGNAL", "The strategy returned a non-numeric weight.") from exc
        if not math.isfinite(value) or value < 0:
            raise ObservationBlocked("INVALID_STRATEGY_SIGNAL", "The strategy returned an invalid target weight.")
        if value > 0:
            normalized[code] = value
    if sum(normalized.values()) > 1.0 + 1e-9:
        raise ObservationBlocked("INVALID_STRATEGY_SIGNAL", "The strategy target weights exceed 100%.")
    return normalized


def _default_fund_signal_provider(db: Session, observation: StrategyObservation, as_of: date) -> dict[str, float]:
    """Generate fund targets from the exact archived datasets in the run."""
    run_id = getattr(observation, "backtest_run_id", None)
    run = db.query(Run).filter(Run.id == run_id).first() if run_id else None
    strategy = db.query(Strategy).filter(Strategy.id == observation.strategy_id).first()
    if run is None or strategy is None:
        raise ObservationBlocked("FUND_EVIDENCE_NOT_AVAILABLE", "A completed fund backtest evidence record is required.")
    try:
        manifest = json.loads(run.data_manifest or "{}")
    except (TypeError, ValueError) as exc:
        raise ObservationBlocked("FUND_EVIDENCE_INVALID", "The fund backtest manifest is invalid.") from exc
    dataset_manifests = manifest.get("dataset_manifests") if isinstance(manifest, dict) else None
    if not isinstance(dataset_manifests, list) or not dataset_manifests:
        raise ObservationBlocked("FUND_EVIDENCE_INVALID", "No archived fund dataset is bound to this run.")
    from server.services.fund_nav_archive import get_fund_nav_dataset
    history: dict[str, list[dict[str, Any]]] = {}
    try:
        for item in dataset_manifests:
            dataset_id = item.get("dataset_id") if isinstance(item, dict) else None
            if not dataset_id:
                raise ValueError("dataset id missing")
            dataset = get_fund_nav_dataset(db, dataset_id, include_rows=True)
            if dataset.get("content_hash") != item.get("content_hash"):
                raise ValueError("dataset content hash mismatch")
            history[dataset["code"]] = dataset.get("rows", [])
    except (KeyError, TypeError, ValueError) as exc:
        raise ObservationBlocked("FUND_EVIDENCE_INVALID", "The archived fund dataset could not be verified.") from exc
    try:
        params = json.loads(strategy.params or "{}")
        from server.api.backtest import _import_strategy
        strategy_cls = _import_strategy(strategy.strategy_class)
        context = StrategyContext(_verified_trading_calendar(as_of))
        strategy_instance = strategy_cls(context, **params)
        strategy_instance.initialize()
        strategy_instance._fund_history = history
        signals = strategy_instance.generate_signals(as_of)
    except ObservationBlocked:
        raise
    except Exception as exc:
        raise ObservationBlocked("FUND_STRATEGY_UNAVAILABLE", f"Fund strategy signal generation was blocked: {type(exc).__name__}.") from exc
    if not isinstance(signals, dict):
        raise ObservationBlocked("INVALID_STRATEGY_SIGNAL", "The fund strategy did not return target weights.")
    normalized: dict[str, float] = {}
    for code, weight in signals.items():
        try:
            normalized_code = normalize_symbol(CN_FUND, code)
            value = float(weight)
        except (TypeError, ValueError) as exc:
            raise ObservationBlocked("INVALID_STRATEGY_SIGNAL", "The fund strategy returned an invalid code or weight.") from exc
        if not math.isfinite(value) or value < 0:
            raise ObservationBlocked("INVALID_STRATEGY_SIGNAL", "The fund strategy returned an invalid weight.")
        if value > 0:
            normalized[normalized_code] = value
    if sum(normalized.values()) > 1.0 + 1e-9:
        raise ObservationBlocked("INVALID_STRATEGY_SIGNAL", "The fund strategy target weights exceed 100%.")
    return normalized


def _fund_quote_map(db: Session, provider: object, codes: list[str], day: date, not_before: Optional[date] = None) -> dict[str, MarketQuote]:
    if not codes:
        return {}
    try:
        rows = provider.fetch_quotes(codes)
    except Exception as exc:
        raise ObservationBlocked("FUND_NAV_UNAVAILABLE", f"No usable fund NAV was returned: {type(exc).__name__}.") from exc
    result: dict[str, MarketQuote] = {}
    for quote in rows or []:
        try:
            code = normalize_symbol(CN_FUND, quote.code)
            price = float(quote.price)
            observed = datetime.fromisoformat(str(quote.as_of).replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            continue
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=SHANGHAI_TZ)
        observed = observed.astimezone(timezone.utc)
        if (
            code not in codes or not math.isfinite(price) or price <= 0
            or str(quote.source) != "eastmoney:fund_nav"
            or not quote.as_of or str(quote.freshness or "").lower() not in {"fresh", "realtime", "delayed", "stale"}
            or observed > datetime.now(timezone.utc) + timedelta(seconds=5)
            or observed.astimezone(SHANGHAI_TZ).date() > day
            or (not_before is not None and observed.astimezone(SHANGHAI_TZ).date() <= not_before)
        ):
            continue
        # The paper order path must be able to prove this exact NAV came from
        # the provider before it is allowed to create a fill.
        from server.services.fund_nav_registry import register_fund_nav
        register_fund_nav(code=code, price=price, source=quote.source, as_of=quote.as_of,
                          freshness=quote.freshness, received_at=quote.received_at, db=db)
        result[code] = quote
    if any(code not in result for code in codes):
        raise ObservationBlocked("FUND_NAV_INCOMPLETE", "Fund NAVs are missing, future-dated, or not provider-verified.")
    return result


def _run_fund_observation_tick(
    db: Session,
    *,
    account_id: str,
    observation_id: str,
    as_of: Optional[date],
    signal_provider: Optional[Callable[[StrategyObservation, date], dict[str, float]]],
    quote_provider: Optional[object],
) -> dict[str, Any]:
    """Run one two-phase domestic-fund NAV observation checkpoint."""
    account = _require_account(db, account_id)
    observation = _load_row(db, account_id, observation_id)
    _revalidate_observation_evidence(db, observation, account)
    if observation.status != "running":
        return _tick_response(observation, status=observation.status, reason="observation_not_running")
    today = _trade_date()
    day = as_of or today
    if day > today:
        raise ValueError("observation_date_in_future")
    if day < date.fromisoformat(observation.start_date):
        return _tick_response(observation, status="not_started", reason="observation_start_date_not_reached")
    # Fund NAV execution is always a two-phase checkpoint, including explicit
    # historical replay.  A historical `as_of` call stages D-day targets and
    # a later replay call executes them through the same durable plan path.
    live_checkpoint = True
    end_day = date.fromisoformat(observation.end_date)
    pending = _pending_signals(observation)
    pending_date = getattr(observation, "pending_signal_date", None)
    pending_due = bool(live_checkpoint and observation.auto_trade and pending and pending_date and pending_date < day.isoformat())
    # A signal staged on the final observation day is executable on the next
    # valid NAV day.  Do not expire the task before that durable signal has had
    # a chance to execute (weekends/holidays commonly push execution past the
    # calendar end date).
    if day > end_day and not pending_due:
        observation.status = "completed"
        observation.last_tick_status = "completed"
        _add_event(db, observation, event_type="expired", status="completed", event_date=day, reason="observation_end_date_reached")
        observation.updated_at = _now()
        db.commit(); db.refresh(observation)
        return _tick_response(observation, status="completed", reason="observation_end_date_reached")
    if observation.last_tick_date == day.isoformat() and observation.last_tick_status in {"completed", "skipped"}:
        return _tick_response(observation, status=observation.last_tick_status, reason="tick_already_recorded")
    provider = quote_provider
    owns_provider = provider is None
    if provider is None:
        from quant_engine.data.global_markets import EastmoneyFundDataProvider
        provider = EastmoneyFundDataProvider(timeout_seconds=10)
    # Injected providers are allowed for deterministic historical replay. The
    # production provider is a current snapshot source and cannot be replayed.
    if as_of is not None and day < today and getattr(provider, "supports_historical_dates", False) is False and quote_provider is None:
        raise ValueError("live_observation_date_must_be_today")
    session_status = market_session_status(CN_FUND, day)
    if session_status == "calendar_unavailable":
        observation.last_tick_date = day.isoformat(); observation.last_tick_at = _now()
        observation.last_tick_status = "blocked"; observation.last_error = "TRADING_CALENDAR_UNAVAILABLE:verified_calendar_required"
        _add_event(db, observation, event_type="tick_blocked", status="blocked", event_date=day, reason=observation.last_error, payload={"code": "TRADING_CALENDAR_UNAVAILABLE"})
        observation.updated_at = _now(); db.commit(); db.refresh(observation)
        return _tick_response(observation, status="blocked", reason=observation.last_error)
    if session_status == "closed":
        observation.last_tick_date = day.isoformat(); observation.last_tick_at = _now()
        observation.last_tick_status = "skipped"; observation.last_error = "not_a_trading_day"
        _add_event(db, observation, event_type="tick_skipped", status="skipped", event_date=day, reason="not_a_trading_day")
        observation.updated_at = _now(); db.commit(); db.refresh(observation)
        return _tick_response(observation, status="skipped", reason="not_a_trading_day")
    if live_checkpoint and observation.pending_signal_date == day.isoformat():
        return _tick_response(observation, status="pending", reason="signals_already_staged", data={"signal_date": observation.pending_signal_date})
    try:
        try:
            # When only a final-day pending signal remains, execution should
            # use that exact stored target; do not request a fresh signal after
            # the observation window has ended.
            signals = {} if day > end_day and pending_due else (signal_provider(observation, day) if signal_provider else _default_fund_signal_provider(db, observation, day))
        except ObservationBlocked as exc:
            observation.last_tick_status = "blocked"; observation.last_error = f"{exc.code}:{exc.reason}"; observation.last_tick_at = _now()
            _add_event(db, observation, event_type="tick_blocked", status="blocked", event_date=day, reason=observation.last_error, payload={"code": exc.code})
            observation.updated_at = _now(); db.commit(); db.refresh(observation)
            return _tick_response(observation, status="blocked", reason=observation.last_error)
        if not isinstance(signals, dict):
            raise ObservationBlocked("INVALID_STRATEGY_SIGNAL", "The fund strategy did not return a target-weight mapping.")
        normalized: dict[str, float] = {}
        for code, weight in signals.items():
            try:
                canonical = normalize_symbol(CN_FUND, code); value = float(weight)
            except (TypeError, ValueError) as exc:
                raise ObservationBlocked("INVALID_STRATEGY_SIGNAL", "The fund strategy returned an invalid code or weight.") from exc
            if not math.isfinite(value) or value < 0:
                raise ObservationBlocked("INVALID_STRATEGY_SIGNAL", "The fund strategy returned an invalid weight.")
            if value > 0:
                normalized[canonical] = value
        if sum(normalized.values()) > 1.0 + 1e-9:
            raise ObservationBlocked("INVALID_STRATEGY_SIGNAL", "The fund strategy target weights exceed 100%.")
        managed = _managed_codes(observation) | set(normalized)
        if pending_date and pending_date > day.isoformat():
            raise ObservationBlocked("INVALID_PENDING_SIGNAL", "The stored signal date is in the future.")
        order_results: list[dict[str, Any]] = []
        quote_rows: dict[str, MarketQuote] = {}
        if live_checkpoint:
            if pending:
                managed |= set(pending)
            if observation.auto_trade and pending and pending_date and pending_date < day.isoformat():
                pending_codes = sorted(pending)
                quote_rows = _fund_quote_map(db, provider, pending_codes, day, not_before=date.fromisoformat(pending_date))
                plan = _get_or_create_fund_rebalance_plan(
                    db, observation=observation, account_id=account_id,
                    signal_date=pending_date, execution_date=day.isoformat(),
                    pending=pending, quotes=quote_rows,
                )
                order_results.extend(_execute_fund_rebalance_plan(db, plan))
                # Do not replace the durable signal while a plan is partial or
                # blocked. Keeping the old target visible lets a later tick
                # resume the exact item set after a process/network failure.
                if plan.status != "completed":
                    observation.last_tick_date = day.isoformat()
                    observation.last_tick_at = _now()
                    observation.last_tick_status = "partial" if plan.status in {"partial", "executing"} else "blocked"
                    observation.last_error = plan.last_error or f"rebalance_{plan.status}"
                    _add_event(db, observation, event_type="rebalance_partial" if plan.status == "partial" else "tick_blocked", status=observation.last_tick_status, event_date=day,
                               reason=observation.last_error, payload={"paper_only": True, "rebalance_plan_id": plan.id, "order_count": plan.order_count, "filled_count": plan.filled_count})
                    observation.updated_at = _now(); db.commit(); db.refresh(observation)
                    return _tick_response(observation, status=observation.last_tick_status, reason=observation.last_error, orders=order_results,
                                          data={"rebalance_plan_id": plan.id, "signal_date": pending_date, "execution_date": day.isoformat(), "managed_codes": sorted(managed)})
            observation.managed_codes = json.dumps(sorted(managed), ensure_ascii=False)
            # Include the calendar end date itself. If it is a valid
            # checkpoint, its target must execute on the next valid day rather
            # than being silently discarded at the boundary.
            should_stage = bool(observation.auto_trade and day <= end_day)
            if should_stage:
                observation.pending_signals = json.dumps({code: normalized.get(code, 0.0) for code in managed}, ensure_ascii=False, sort_keys=True)
                observation.pending_signal_date = day.isoformat()
            else:
                observation.pending_signals = "{}"; observation.pending_signal_date = None
            observation.last_tick_date = day.isoformat(); observation.last_tick_at = _now(); observation.last_tick_status = "completed" if order_results or not should_stage else "pending"; observation.last_error = None
            if day > end_day or (day == end_day and not should_stage):
                observation.status = "completed"
            event_type = "tick_executed_and_staged" if order_results and should_stage else ("tick_staged" if should_stage else "tick_completed")
            _add_event(db, observation, event_type=event_type, status=observation.last_tick_status, event_date=day,
                       signal_count=len(normalized), order_count=len(order_results), payload={
                           "auto_trade": bool(observation.auto_trade), "paper_only": True,
                           "signal_date": day.isoformat(), "execution_date": day.isoformat() if order_results else None,
                           "pending_signal_date": observation.pending_signal_date,
                           "rebalance_plan_id": (plan.id if 'plan' in locals() else None),
                       })
            observation.updated_at = _now(); db.commit(); db.refresh(observation)
            return _tick_response(observation, status=observation.last_tick_status, reason=None, signals=len(normalized), orders=order_results,
                                  data={"quote_sources": sorted({item.source for item in quote_rows.values()}), "quote_as_of": sorted({item.as_of for item in quote_rows.values() if item.as_of}), "managed_codes": sorted(managed), "signal_date": day.isoformat(), "execution_date": day.isoformat() if order_results else None})

    except ObservationBlocked as exc:
        db.rollback(); observation = _load_row(db, account_id, observation_id)
        observation.last_tick_status = "blocked"; observation.last_error = f"{exc.code}:{exc.reason}"; observation.last_tick_at = _now()
        _add_event(db, observation, event_type="tick_blocked", status="blocked", event_date=day, reason=observation.last_error, payload={"code": exc.code})
        observation.updated_at = _now(); db.commit(); db.refresh(observation)
        return _tick_response(observation, status="blocked", reason=observation.last_error)
    except ValueError as exc:
        db.rollback(); observation = _load_row(db, account_id, observation_id)
        observation.last_tick_status = "blocked"; observation.last_error = f"ORDER_BLOCKED:{exc}"; observation.last_tick_at = _now()
        _add_event(db, observation, event_type="tick_blocked", status="blocked", event_date=day, reason=observation.last_error, payload={"code": "ORDER_BLOCKED"})
        observation.updated_at = _now(); db.commit(); db.refresh(observation)
        return _tick_response(observation, status="blocked", reason=observation.last_error)
    finally:
        if owns_provider:
            close = getattr(provider, "close", None)
            if close:
                close()


def _quote_map(
    provider: QuoteProvider,
    codes: list[str],
    day: Optional[date] = None,
    not_before: Optional[date] = None,
) -> dict[str, MarketQuote]:
    if not codes:
        return {}
    try:
        rows = provider.fetch_quotes(codes)
    except Exception as exc:
        raise ObservationBlocked("LIVE_QUOTE_UNAVAILABLE", f"No usable live quote was returned: {type(exc).__name__}.") from exc
    result: dict[str, MarketQuote] = {}
    for quote in rows or []:
        try:
            price = float(quote.price)
        except (TypeError, ValueError):
            continue
        if quote.code not in codes or not math.isfinite(price) or price <= 0:
            continue
        if not quote.as_of or str(quote.freshness or "").lower() not in {"fresh", "realtime", "delayed"}:
            continue
        try:
            observed_at = datetime.fromisoformat(str(quote.as_of).replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            continue
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=SHANGHAI_TZ)
        observed_at = observed_at.astimezone(timezone.utc)
        if observed_at > datetime.now(timezone.utc) + timedelta(seconds=5):
            continue
        observed_day = observed_at.astimezone(SHANGHAI_TZ).date()
        if day is not None and observed_day > day:
            continue
        # Execution evidence must be strictly newer than the D-day signal.
        # This is especially important for fund NAV, where a published value
        # is not an intraday executable price.
        if not_before is not None and observed_day <= not_before:
            continue
        result[quote.code] = quote
    missing = [code for code in codes if code not in result]
    if missing:
        raise ObservationBlocked("LIVE_QUOTE_INCOMPLETE", f"Live quotes are missing or expired for {len(missing)} security(ies).")
    return result


def _tick_response(observation: StrategyObservation, *, status: str, reason: Optional[str], signals: int = 0, orders: Optional[list[dict[str, Any]]] = None, data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return {
        "observation": _observation_dict(observation),
        "status": status,
        "reason": reason,
        "signals_count": signals,
        "orders": orders or [],
        "orders_count": len(orders or []),
        "data": data or {},
        "execution_mode": "paper_only",
        "live_execution": False,
    }


def run_observation_tick(
    db: Session,
    *,
    account_id: str,
    observation_id: str,
    as_of: Optional[date] = None,
    signal_provider: Optional[Callable[[StrategyObservation, date], dict[str, float]]] = None,
    quote_provider: Optional[QuoteProvider] = None,
) -> dict[str, Any]:
    """Run at most one idempotent paper tick for the selected date.

    The injectable providers make the contract testable without network access;
    production calls use the existing local strategy/data path and the bounded
    AKShare quote provider.  A blocked tick remains retryable on the same date.
    """
    with _observation_lock:
        account = _require_account(db, account_id)
        account_market = normalize_market(account.market)
        if account_market == CN_FUND:
            return _run_fund_observation_tick(
                db, account_id=account_id, observation_id=observation_id,
                as_of=as_of, signal_provider=signal_provider,
                quote_provider=quote_provider,
            )
        if account_market != A_SHARE:
            raise ValueError("strategy_observation_requires_supported_market")
        observation = _load_row(db, account_id, observation_id)
        _revalidate_observation_evidence(db, observation, account)
        if observation.status != "running":
            return _tick_response(observation, status=observation.status, reason="observation_not_running")
        today = _trade_date()
        day = as_of or today
        if day > today:
            raise ValueError("observation_date_in_future")
        if day < date.fromisoformat(observation.start_date):
            return _tick_response(observation, status="not_started", reason="observation_start_date_not_reached")
        end_day = date.fromisoformat(observation.end_date)
        pending = _pending_signals(observation)
        pending_date = getattr(observation, "pending_signal_date", None)
        pending_due = bool(as_of is None and observation.auto_trade and pending and pending_date and pending_date < day.isoformat())
        # Keep a final-day live target alive until the next valid exchange day;
        # a seven-day window may end on a weekend or holiday.
        if day > end_day and not pending_due:
            observation.status = "completed"
            observation.last_tick_status = "completed"
            observation.last_error = None
            _add_event(db, observation, event_type="expired", status="completed", event_date=day, reason="observation_end_date_reached")
            observation.updated_at = _now()
            db.commit()
            db.refresh(observation)
            return _tick_response(observation, status="completed", reason="observation_end_date_reached")
        if observation.last_tick_date == day.isoformat() and observation.last_tick_status in {"completed", "skipped"}:
            return _tick_response(observation, status=observation.last_tick_status, reason="tick_already_recorded")

        # The production quote path is a current snapshot provider. A caller
        # must not accidentally replay a historical date against that live
        # source. Tests/replay callers may inject a non-live provider (or a
        # live provider that explicitly supports historical dates), in which
        # case the supplied day is propagated to every paper side effect.
        # This guard comes after no-I/O lifecycle no-ops (not-started,
        # expired, and already-recorded) so those operations remain safe to
        # inspect for a historical date.
        if (as_of is not None and day < today and
                (quote_provider is None or
                 (isinstance(quote_provider, LiveMarketDataProvider) and
                  not getattr(quote_provider, "supports_historical_dates", False)))):
            raise ValueError("live_observation_date_must_be_today")

        try:
            calendar_data = _verified_trading_calendar(day)
        except ObservationBlocked as exc:
            observation.last_tick_date = day.isoformat()
            observation.last_tick_at = _now()
            observation.last_tick_status = "blocked"
            observation.last_error = f"{exc.code}:{exc.reason}"
            _add_event(
                db, observation, event_type="tick_blocked", status="blocked",
                event_date=day, reason=observation.last_error,
                payload={"code": exc.code},
            )
            observation.updated_at = _now()
            db.commit()
            db.refresh(observation)
            return _tick_response(observation, status="blocked", reason=observation.last_error)
        if not calendar_data.is_trading_day(day):
            observation.last_tick_date = day.isoformat()
            observation.last_tick_at = _now()
            observation.last_tick_status = "skipped"
            observation.last_error = "not_a_trading_day"
            _add_event(db, observation, event_type="tick_skipped", status="skipped", event_date=day, reason="not_a_trading_day")
            observation.updated_at = _now()
            db.commit()
            db.refresh(observation)
            return _tick_response(observation, status="skipped", reason="not_a_trading_day")

        provider = quote_provider
        owns_provider = provider is None
        if provider is None:
            provider = AKShareLiveMarketDataProvider(timeout_seconds=10)
        live_checkpoint = as_of is None and isinstance(provider, LiveMarketDataProvider)
        try:
            # A live checkpoint is intentionally two-phase: repeated calls on
            # D must not regenerate or execute the same target.  The target
            # is durable and will be consumed by the next valid checkpoint.
            if live_checkpoint and observation.pending_signal_date == day.isoformat():
                return _tick_response(
                    observation,
                    status="pending",
                    reason="signals_already_staged",
                    data={"signal_date": observation.pending_signal_date},
                )
            try:
                signals = signal_provider(observation, day) if signal_provider else _default_signal_provider(db, observation, day)
            except ObservationBlocked as exc:
                observation.last_tick_status = "blocked"
                observation.last_error = f"{exc.code}:{exc.reason}"
                observation.last_tick_at = _now()
                _add_event(db, observation, event_type="tick_blocked", status="blocked", event_date=day, reason=observation.last_error, payload={"code": exc.code})
                observation.updated_at = _now()
                db.commit()
                db.refresh(observation)
                return _tick_response(observation, status="blocked", reason=observation.last_error)
            if not isinstance(signals, dict):
                raise ObservationBlocked("INVALID_STRATEGY_SIGNAL", "The strategy did not return a target-weight mapping.")
            normalized = {}
            for code, weight in signals.items():
                value = float(weight)
                if not _CODE_RE.fullmatch(str(code)) or not math.isfinite(value) or value < 0:
                    raise ObservationBlocked("INVALID_STRATEGY_SIGNAL", "The strategy returned invalid A-share weights.")
                if value > 0:
                    normalized[str(code)] = value
            if sum(normalized.values()) > 1.0 + 1e-9:
                raise ObservationBlocked("INVALID_STRATEGY_SIGNAL", "The strategy target weights exceed 100%.")
            # Keep a durable distinction between strategy-managed holdings and
            # positions the user bought manually during the observation.  New
            # signal codes become managed; previously managed codes remain in
            # the rebalance universe even when their current target is zero.
            managed = _managed_codes(observation) | set(normalized)
            if pending_date and pending_date > day.isoformat():
                raise ObservationBlocked("INVALID_PENDING_SIGNAL", "The stored signal date is in the future.")
            if live_checkpoint:
                # Consume the previous signal using a quote published after
                # its signal date, then stage today's target for the next
                # valid checkpoint.  This prevents D-day close/NAV look-ahead.
                if pending:
                    managed |= set(pending)
                observation.managed_codes = json.dumps(sorted(managed), ensure_ascii=False)
                held = [row.code for row in db.query(PaperAccountPosition).filter(PaperAccountPosition.account_id == account_id, PaperAccountPosition.shares > 0).all()]
                manual_held = sorted(set(held) - managed)
                order_results: list[dict[str, Any]] = []
                quote_rows: dict[str, MarketQuote] = {}
                plan: Optional[PaperRebalancePlan] = None
                if observation.auto_trade and pending and pending_date and pending_date < day.isoformat():
                    pending_codes = sorted(set(pending))
                    quote_rows = _quote_map(
                        provider,
                        pending_codes,
                        day,
                        not_before=date.fromisoformat(pending_date),
                    )
                    plan = _get_or_create_a_share_rebalance_plan(
                        db, observation=observation, account_id=account_id,
                        signal_date=pending_date, execution_date=day.isoformat(),
                        pending=pending, quotes=quote_rows,
                    )
                    order_results.extend(_execute_rebalance_plan(db, plan))
                    # Keep the pending target visible when a plan is waiting
                    # for retry or has been blocked by authoritative risk
                    # rules. A later checkpoint can resume the same item set.
                    if plan.status != "completed":
                        observation.last_tick_date = day.isoformat()
                        observation.last_tick_at = _now()
                        observation.last_tick_status = "partial" if plan.status in {"partial", "executing"} else "blocked"
                        observation.last_error = plan.last_error or f"rebalance_{plan.status}"
                        _add_event(
                            db, observation,
                            event_type="rebalance_partial" if plan.status == "partial" else "tick_blocked",
                            status=observation.last_tick_status,
                            event_date=day,
                            reason=observation.last_error,
                            payload={"paper_only": True, "rebalance_plan_id": plan.id, "order_count": plan.order_count, "filled_count": plan.filled_count},
                        )
                        observation.updated_at = _now()
                        db.commit()
                        db.refresh(observation)
                        return _tick_response(
                            observation, status=observation.last_tick_status,
                            reason=observation.last_error, orders=order_results,
                            data={"rebalance_plan_id": plan.id, "signal_date": pending_date, "execution_date": day.isoformat(), "managed_codes": sorted(managed), "manual_positions_protected": manual_held},
                        )
                # Keep a final trading-day target pending for the next valid
                # checkpoint; otherwise a calendar end that happens to be a
                # market day would skip the last strategy signal.
                should_stage = bool(observation.auto_trade and day <= end_day)
                if should_stage:
                    # Persist explicit zero targets for previously managed
                    # codes so the next checkpoint can sell them rather than
                    # silently leaving an obsolete strategy position behind.
                    pending_targets = {code: normalized.get(code, 0.0) for code in managed}
                    observation.pending_signals = json.dumps(pending_targets, ensure_ascii=False, sort_keys=True)
                    observation.pending_signal_date = day.isoformat()
                else:
                    observation.pending_signals = "{}"
                    observation.pending_signal_date = None
                observation.last_tick_date = day.isoformat()
                observation.last_tick_at = _now()
                observation.last_tick_status = "completed" if order_results or not should_stage else "pending"
                observation.last_error = None
                if day > end_day or (day == end_day and not should_stage):
                    observation.status = "completed"
                event_type = "tick_executed_and_staged" if order_results and should_stage else ("tick_staged" if should_stage else "tick_completed")
                _add_event(
                    db,
                    observation,
                    event_type=event_type,
                    status=observation.last_tick_status,
                    event_date=day,
                    signal_count=len(normalized),
                    order_count=len(order_results),
                    payload={
                        "auto_trade": bool(observation.auto_trade),
                        "paper_only": True,
                        "signal_date": day.isoformat(),
                        "execution_date": day.isoformat() if order_results else None,
                        "pending_signal_date": observation.pending_signal_date,
                        "rebalance_plan_id": plan.id if plan else None,
                    },
                )
                observation.updated_at = _now()
                db.commit()
                db.refresh(observation)
                return _tick_response(
                    observation,
                    status=observation.last_tick_status,
                    reason=None,
                    signals=len(normalized),
                    orders=order_results,
                    data={
                        "quote_sources": sorted({item.source for item in quote_rows.values()}),
                        "quote_as_of": sorted({item.as_of for item in quote_rows.values() if item.as_of}),
                        "managed_codes": sorted(managed),
                        "manual_positions_protected": manual_held,
                        "signal_date": day.isoformat(),
                        "execution_date": day.isoformat() if order_results else None,
                        "rebalance_plan_id": plan.id if plan else None,
                    },
                )
            observation.managed_codes = json.dumps(sorted(managed), ensure_ascii=False)
            held = [row.code for row in db.query(PaperAccountPosition).filter(PaperAccountPosition.account_id == account_id, PaperAccountPosition.shares > 0).all()]
            manual_held = sorted(set(held) - managed)
            codes = sorted(managed)
            quotes = _quote_map(provider, codes, day)
            order_results: list[dict[str, Any]] = []
            if observation.auto_trade:
                for code in codes:
                    quote = quotes[code]
                    current_shares = _strategy_shares(db, account_id, code, observation.id)
                    target_amount = observation.allocated_capital * normalized.get(code, 0.0)
                    target_shares = max(0, int(target_amount / float(quote.price) / 100) * 100)
                    diff = target_shares - current_shares
                    if diff == 0:
                        continue
                    side = "buy" if diff > 0 else "sell"
                    quantity = abs(diff)
                    result = submit_order(
                        db,
                        account_id=account_id,
                        idempotency_key=f"observation-{observation.id}-{day.isoformat()}-{code}-{side}",
                        code=code,
                        side=side,
                        quantity=quantity,
                        price=float(quote.price),
                        price_source=quote.source,
                        price_as_of=quote.as_of,
                        price_freshness=quote.freshness,
                        trade_date=day,
                        lot_owner="strategy",
                        lot_owner_id=observation.id,
                    )
                    order_results.append(result)
            observation.last_tick_date = day.isoformat()
            observation.last_tick_at = _now()
            observation.last_tick_status = "completed"
            observation.last_error = None
            status = "completed"
            if day >= date.fromisoformat(observation.end_date):
                observation.status = "completed"
                _add_event(db, observation, event_type="tick_completed", status="completed", event_date=day, signal_count=len(normalized), order_count=len(order_results), payload={"auto_trade": bool(observation.auto_trade), "paper_only": True, "expired_after_tick": True})
            else:
                _add_event(db, observation, event_type="tick_completed", status="completed", event_date=day, signal_count=len(normalized), order_count=len(order_results), payload={"auto_trade": bool(observation.auto_trade), "paper_only": True})
            observation.updated_at = _now()
            db.commit()
            db.refresh(observation)
            return _tick_response(observation, status=status, reason=None, signals=len(normalized), orders=order_results, data={"quote_sources": sorted({item.source for item in quotes.values()}), "quote_as_of": sorted({item.as_of for item in quotes.values() if item.as_of}), "managed_codes": sorted(managed), "manual_positions_protected": manual_held})
        except ObservationBlocked as exc:
            observation.last_tick_status = "blocked"
            observation.last_error = f"{exc.code}:{exc.reason}"
            observation.last_tick_at = _now()
            _add_event(db, observation, event_type="tick_blocked", status="blocked", event_date=day, reason=observation.last_error, payload={"code": exc.code})
            observation.updated_at = _now()
            db.commit()
            db.refresh(observation)
            return _tick_response(observation, status="blocked", reason=observation.last_error)
        except ValueError as exc:
            db.rollback()
            observation = _load_row(db, account_id, observation_id)
            observation.last_tick_status = "blocked"
            observation.last_error = f"ORDER_BLOCKED:{exc}"
            observation.last_tick_at = _now()
            _add_event(db, observation, event_type="tick_blocked", status="blocked", event_date=day, reason=observation.last_error, payload={"code": "ORDER_BLOCKED"})
            observation.updated_at = _now()
            db.commit()
            db.refresh(observation)
            return _tick_response(observation, status="blocked", reason=observation.last_error)
        finally:
            if owns_provider:
                close = getattr(provider, "close", None)
                if close:
                    close()
