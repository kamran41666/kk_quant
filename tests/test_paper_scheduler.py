import asyncio
import threading
from datetime import date, datetime, timedelta, timezone

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from quant_engine.data.live import AKShareLiveMarketDataProvider, MarketDataUnavailableError, MarketQuote
from server.models.database import Base
from server.models.schema import PaperDailyReport
from server.services.paper_trading import create_account, submit_order
from server.services.paper_scheduler import PaperDailyScheduler


class _Provider:
    def __init__(self):
        self.calls = 0

    def fetch_quotes(self, codes):
        self.calls += 1
        now = datetime.now(timezone.utc).isoformat()
        return [MarketQuote(code=code, name="demo", price=10.0, change_pct=0.0, volume=1.0, amount=10.0, source="test-feed", as_of=now, received_at=now, freshness="fresh", is_fallback=False) for code in codes]


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_scheduler_values_accounts_and_is_idempotent(monkeypatch):
    db = _db()
    account = create_account(db, name="scheduled", initial_capital=100_000, max_position_weight=1.0)
    submit_order(db, account_id=account["id"], idempotency_key="scheduler-buy-1", code="000001.SZ", side="buy", quantity=100, price=10)
    monkeypatch.setattr("server.services.paper_scheduler.SessionLocal", lambda: db)
    provider = _Provider()
    scheduler = PaperDailyScheduler(provider, interval_seconds=10)
    first = scheduler.run_once(date(2024, 6, 14))
    second = scheduler.run_once(date(2024, 6, 14))
    assert first["status"] == "completed"
    assert first["valuation_count"] == 1
    assert second["status"] == "completed"
    assert second["valuation_count"] == 1
    assert provider.calls == 1
    # A later order advances the same-day watermark and causes exactly one
    # fresh valuation; the following poll is idempotent again.
    submit_order(db, account_id=account["id"], idempotency_key="scheduler-buy-3", code="000001.SZ", side="buy", quantity=100, price=10)
    scheduler.run_once(date(2024, 6, 14))
    scheduler.run_once(date(2024, 6, 14))
    assert provider.calls == 2
    assert db.query(PaperDailyReport).count() == 1


def test_scheduler_fails_closed_when_quotes_are_unavailable(monkeypatch):
    db = _db()
    account = create_account(db, name="unavailable", initial_capital=100_000, max_position_weight=1.0)
    submit_order(db, account_id=account["id"], idempotency_key="scheduler-buy-2", code="000001.SZ", side="buy", quantity=100, price=10)
    monkeypatch.setattr("server.services.paper_scheduler.SessionLocal", lambda: db)

    class _Unavailable:
        def fetch_quotes(self, codes):
            raise MarketDataUnavailableError([{"source": "test", "error": "offline"}])

    result = PaperDailyScheduler(_Unavailable(), interval_seconds=10).run_once(date(2024, 6, 17))
    assert result["status"] == "skipped"
    assert "market_data_unavailable" in result["reason"]


def test_scheduler_rejects_future_date():
    with pytest.raises(ValueError, match="run_date_in_future"):
        PaperDailyScheduler(_Provider(), interval_seconds=10).run_once(date.today() + timedelta(days=1))


def test_live_scheduler_rejects_historical_date():
    provider = AKShareLiveMarketDataProvider(eastmoney_fetcher=lambda: None, sina_fetcher=lambda: None)
    try:
        with pytest.raises(ValueError, match="live_scheduler_date_must_be_today"):
            PaperDailyScheduler(provider, interval_seconds=10).run_once(date.today() - timedelta(days=1))
    finally:
        provider.close()


def test_scheduler_shutdown_waits_for_inflight_worker(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    real_wait_for = asyncio.wait_for

    class _BlockingScheduler(PaperDailyScheduler):
        def run_once(self, run_date=None):
            started.set()
            release.wait(2)
            return {"status": "completed"}

    wait_calls = 0

    async def trigger_worker(awaitable, timeout):
        nonlocal wait_calls
        awaitable.close()
        wait_calls += 1
        if wait_calls == 1:
            raise asyncio.TimeoutError
        return None

    monkeypatch.setattr("server.services.paper_scheduler.asyncio.wait_for", trigger_worker)
    scheduler = _BlockingScheduler(None, interval_seconds=10)

    async def exercise():
        task = asyncio.create_task(scheduler.run_forever())
        assert await asyncio.to_thread(started.wait, 1)
        await scheduler.stop()
        assert not task.done()
        release.set()
        await real_wait_for(task, 1)

    asyncio.run(exercise())
