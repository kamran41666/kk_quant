"""Automatic paper-account valuation task.

The scheduler is deliberately fail-closed: if the live provider cannot return
fresh, timestamped prices, no account is valued and the run is recorded as
skipped. It never substitutes a fabricated price.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import date, datetime, time, timezone
from typing import Optional

from quant_engine.data.calendar import TradingCalendar
from quant_engine.data.live import LiveMarketDataProvider, MarketDataUnavailableError, MarketQuote, SHANGHAI_TZ
from server.models.database import SessionLocal
from server.models.schema import PaperAccount, PaperSchedulerRun
from server.services.paper_trading import build_daily_report, mark_to_market

logger = logging.getLogger(__name__)


def _as_utc(value: str) -> datetime:
    """Normalize legacy naive and newer timezone-aware audit timestamps."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        # An unreadable timestamp must never suppress a safety re-run.
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        # Existing ORM defaults use server-local naive timestamps (Shanghai in
        # the supported deployment).  Interpret them in the application TZ.
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed.astimezone(timezone.utc)


def _is_trading_day(day: date) -> bool:
    try:
        calendar = TradingCalendar()
        # A cached calendar may legitimately end before today's date. Do not
        # silently label every future weekday as a holiday in that case.
        if calendar.all_dates and day > calendar.all_dates[-1]:
            return day.weekday() < 5
        return calendar.is_trading_day(day)
    except Exception:
        return day.weekday() < 5


class PaperDailyScheduler:
    """Idempotent trading-day runner used by the application lifespan."""

    def __init__(self, provider: Optional[LiveMarketDataProvider] = None, interval_seconds: int = 300):
        if interval_seconds < 10:
            raise ValueError("interval_seconds must be at least 10 seconds")
        self.provider = provider
        self.interval_seconds = interval_seconds
        self._stop = asyncio.Event()
        self._run_lock = threading.Lock()

    def run_once(self, run_date: Optional[date] = None) -> dict:
        if not self._run_lock.acquire(blocking=False):
            return {"run_date": (run_date or datetime.now(SHANGHAI_TZ).date()).isoformat(), "status": "busy", "account_count": 0, "valuation_count": 0, "reason": "another_scheduler_run_in_progress", "created_at": datetime.now(timezone.utc).isoformat()}
        try:
            return self._run_once(run_date)
        finally:
            self._run_lock.release()

    def _run_once(self, run_date: Optional[date] = None) -> dict:
        automatic = run_date is None
        today = datetime.now(SHANGHAI_TZ).date()
        day = run_date or today
        if day > today:
            raise ValueError("run_date_in_future")
        # Live providers normally expose current snapshots.  It is unsafe to
        # apply those prices to a historical date, so a live implementation
        # must explicitly opt in if it can serve historical data.  Lightweight
        # test/replay providers need not inherit the live-provider contract.
        if (isinstance(self.provider, LiveMarketDataProvider)
                and not getattr(self.provider, "supports_historical_dates", False)
                and day != today):
            raise ValueError("live_scheduler_date_must_be_today")
        if automatic and datetime.now(SHANGHAI_TZ).time() < time(15, 5):
            return {"run_date": day.isoformat(), "status": "skipped", "account_count": 0, "valuation_count": 0, "reason": "market_not_closed", "created_at": datetime.now(timezone.utc).isoformat()}
        db = SessionLocal()
        try:
            existing = db.query(PaperSchedulerRun).filter(PaperSchedulerRun.run_date == day.isoformat()).first()
            if existing and existing.status == "completed":
                from server.models.schema import PaperOrder
                latest_order = db.query(PaperOrder).order_by(PaperOrder.created_at.desc()).first()
                watermark = existing.last_run_at or existing.created_at
                if not latest_order or _as_utc(latest_order.created_at) <= _as_utc(watermark):
                    return self._run_dict(existing)
            if not _is_trading_day(day):
                row = existing or PaperSchedulerRun(run_date=day.isoformat(), status="skipped")
                row.status, row.account_count, row.valuation_count, row.reason = "skipped", 0, 0, "not_a_trading_day"
                if not existing:
                    db.add(row)
                db.commit()
                return self._run_dict(row)

            accounts = db.query(PaperAccount).filter(PaperAccount.status == "active").all()
            codes = sorted({position.code for account in accounts for position in account_positions(db, account.id)})
            quote_map: dict[str, MarketQuote] = {}
            if codes:
                if self.provider is None:
                    raise MarketDataUnavailableError([{"source": "scheduler", "error": "provider_not_configured"}])
                try:
                    quotes = self.provider.fetch_quotes(codes)
                except MarketDataUnavailableError:
                    raise
                except Exception as exc:
                    raise MarketDataUnavailableError([{"source": "scheduler", "error": f"{type(exc).__name__}:{exc}"}]) from exc
                quote_map = {quote.code: quote for quote in quotes if quote.price and quote.freshness in {"fresh", "realtime", "delayed"} and quote.as_of}
                if any(code not in quote_map for code in codes):
                    raise MarketDataUnavailableError([{"source": "scheduler", "error": "missing_fresh_quote"}])

            row = existing or PaperSchedulerRun(run_date=day.isoformat(), status="running")
            row.status, row.account_count, row.valuation_count, row.reason = "running", len(accounts), 0, None
            if not existing:
                db.add(row)
            db.commit()
            valuation_count = 0
            for account in accounts:
                account_codes = {position.code for position in account_positions(db, account.id)}
                prices = {code: float(quote_map[code].price) for code in account_codes}
                quote_times = [quote_map[code].as_of for code in account_codes]
                sources = {quote_map[code].source for code in account_codes}
                source = next(iter(sources)) if len(sources) == 1 else "scheduler:mixed"
                if not sources:
                    source = "scheduler:cash"
                as_of = min(quote_times) if quote_times else None
                freshness = "delayed" if any(quote_map[code].freshness == "delayed" for code in account_codes) else (quote_map[next(iter(account_codes))].freshness if account_codes else "fresh")
                mark_to_market(db, account_id=account.id, prices=prices, valuation_date=day, price_source=source, price_as_of=as_of, price_freshness=freshness)
                build_daily_report(db, account_id=account.id, report_date=day)
                valuation_count += 1
            row.status, row.valuation_count, row.reason = "completed", valuation_count, None
            row.last_run_at = datetime.now(timezone.utc).isoformat()
            db.commit()
            return self._run_dict(row)
        except MarketDataUnavailableError as exc:
            db.rollback()
            row = db.query(PaperSchedulerRun).filter(PaperSchedulerRun.run_date == day.isoformat()).first() or PaperSchedulerRun(run_date=day.isoformat())
            row.status, row.account_count, row.valuation_count, row.reason = "skipped", db.query(PaperAccount).filter(PaperAccount.status == "active").count(), 0, f"market_data_unavailable:{exc}"
            if row not in db:
                db.add(row)
            db.commit()
            return self._run_dict(row)
        except Exception as exc:
            db.rollback()
            logger.exception("paper scheduler run failed")
            row = db.query(PaperSchedulerRun).filter(PaperSchedulerRun.run_date == day.isoformat()).first() or PaperSchedulerRun(run_date=day.isoformat())
            row.status, row.reason = "failed", f"{type(exc).__name__}:{exc}"
            if row not in db:
                db.add(row)
            db.commit()
            return self._run_dict(row)
        finally:
            db.close()

    async def run_forever(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
                except asyncio.TimeoutError:
                    # Await the worker.  This makes application shutdown
                    # deterministic: no background thread can outlive the
                    # lifespan and write to the database after shutdown.
                    await asyncio.to_thread(self.run_once)
        finally:
            self.close()

    async def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        close = getattr(self.provider, "close", None)
        if close:
            close()

    @staticmethod
    def _run_dict(row: PaperSchedulerRun) -> dict:
        return {"run_date": row.run_date, "status": row.status, "account_count": row.account_count, "valuation_count": row.valuation_count, "reason": row.reason, "created_at": row.created_at, "last_run_at": row.last_run_at}


def account_positions(db, account_id: str):
    from server.models.schema import PaperAccountPosition
    return db.query(PaperAccountPosition).filter(PaperAccountPosition.account_id == account_id, PaperAccountPosition.shares > 0).all()


def scheduler_runs(limit: int = 30) -> list[dict]:
    db = SessionLocal()
    try:
        rows = db.query(PaperSchedulerRun).order_by(PaperSchedulerRun.run_date.desc()).limit(limit).all()
        return [PaperDailyScheduler._run_dict(row) for row in rows]
    finally:
        db.close()
