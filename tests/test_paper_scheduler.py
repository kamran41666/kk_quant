import asyncio
import json
import threading
from datetime import date, datetime, timedelta, timezone

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from quant_engine.data.live import AKShareLiveMarketDataProvider, MarketDataUnavailableError, MarketQuote
from server.models.database import Base
from server.models.schema import PaperAccountPosition, PaperDailyReport, PaperValuation, StrategyObservation
from server.services.paper_trading import create_account, submit_order
from server.services.fund_nav_registry import register_fund_nav
from server.services.paper_scheduler import PaperDailyScheduler


class _Provider:
    supports_historical_dates = True

    def __init__(self):
        self.calls = 0
        self.batches = []

    def fetch_quotes(self, codes):
        self.calls += 1
        self.batches.append(list(codes))
        now = "2024-06-13T08:00:00+08:00"
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
    valuation = db.query(PaperValuation).filter_by(account_id=account["id"]).one()
    metadata = json.loads(valuation.price_metadata)
    assert metadata["000001.SZ"]["source"] == "test-feed"
    assert metadata["000001.SZ"]["freshness"] == "fresh"
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


def test_scheduler_blocks_when_market_calendar_is_unavailable(monkeypatch):
    db = _db()
    create_account(db, name="calendar-unavailable", initial_capital=100_000, max_position_weight=1.0)
    monkeypatch.setattr("server.services.paper_scheduler.SessionLocal", lambda: db)
    monkeypatch.setattr(
        "server.services.paper_scheduler.market_session_status",
        lambda *_args: "calendar_unavailable",
    )

    result = PaperDailyScheduler(_Provider(), interval_seconds=10).run_once(date(2024, 6, 14))

    assert result["status"] == "blocked"
    assert result["valuation_count"] == 0
    assert result["reason"] == "calendar_unavailable:a-share"


def test_scheduler_refetches_quotes_for_positions_created_by_observation(monkeypatch):
    db = _db()
    account = create_account(db, name="observation-position", initial_capital=100_000, max_position_weight=1.0)
    observation = StrategyObservation(
        account_id=account["id"],
        strategy_id="strategy-for-scheduler-regression",
        idempotency_key="scheduler-observation-new-position",
        duration_days=7,
        allocation_pct=1.0,
        allocated_capital=100_000,
        start_date="2024-06-14",
        end_date="2024-06-20",
        status="running",
        auto_trade=True,
    )
    db.add(observation)
    db.commit()

    def fake_tick(db_arg, **kwargs):
        submit_order(
            db_arg,
            account_id=kwargs["account_id"],
            idempotency_key="scheduler-observation-generated-order",
            code="000001.SZ",
            side="buy",
            quantity=100,
            price=10,
            trade_date=kwargs["as_of"],
        )
        return {"status": "completed", "live_execution": False, "orders_count": 1}

    monkeypatch.setattr("server.services.paper_scheduler.SessionLocal", lambda: db)
    monkeypatch.setattr("server.services.observation.run_observation_tick", fake_tick)
    provider = _Provider()
    result = PaperDailyScheduler(provider, interval_seconds=10).run_once(date(2024, 6, 14))

    assert result["status"] == "completed"
    assert result["valuation_count"] == 1
    assert provider.calls == 1
    assert db.query(PaperDailyReport).one().report_date == "2024-06-14"


def test_scheduler_does_not_send_non_a_share_codes_to_a_share_provider(monkeypatch):
    db = _db()
    a_share = create_account(db, name="scheduler-a-share", initial_capital=100_000, max_position_weight=1.0)
    fund = create_account(db, name="scheduler-fund", market="cn-fund", initial_capital=100_000, max_position_weight=1.0)
    submit_order(db, account_id=a_share["id"], idempotency_key="scheduler-mixed-a", code="000001.SZ", side="buy", quantity=100, price=10)
    register_fund_nav(code="110022", price=2.0, source="eastmoney:fund_nav",
                      as_of="2024-01-02T00:00:00+00:00", freshness="stale", db=db)
    submit_order(db, account_id=fund["id"], idempotency_key="scheduler-mixed-fund", code="110022", side="buy", quantity=1.0, price=2.0,
                 price_source="eastmoney:fund_nav", price_as_of="2024-01-02T00:00:00+00:00", price_freshness="stale")
    monkeypatch.setattr("server.services.paper_scheduler.SessionLocal", lambda: db)
    provider = _Provider()
    result = PaperDailyScheduler(provider, interval_seconds=10).run_once(date(2024, 6, 14))

    assert result["status"] == "completed"
    assert result["valuation_count"] == 1
    assert result["reason"] == "deferred_market_adapters:cn-fund"
    assert provider.calls == 1
    assert provider.batches == [["000001.SZ"]]
    assert db.query(PaperDailyReport).filter(PaperDailyReport.account_id == a_share["id"]).count() == 1
    assert db.query(PaperDailyReport).filter(PaperDailyReport.account_id == fund["id"]).count() == 0


def test_scheduler_uses_market_specific_fund_adapter_and_persists_nav_evidence(monkeypatch):
    db = _db()
    fund = create_account(db, name="scheduler-fund-adapter", market="cn-fund",
                          initial_capital=100_000, max_position_weight=1.0)
    nav_as_of = "2024-06-13T07:00:00+00:00"
    register_fund_nav(code="110022", price=2.0, source="eastmoney:fund_nav",
                      as_of=nav_as_of, freshness="stale", db=db)
    submit_order(
        db, account_id=fund["id"], idempotency_key="scheduler-fund-adapter-order",
        code="110022", side="buy", quantity=10.0, price=2.0,
        price_source="eastmoney:fund_nav", price_as_of=nav_as_of,
        price_freshness="stale",
    )

    class _FundProvider:
        source_name = "eastmoney:fund_nav"
        supports_historical_dates = True

        def fetch_quotes(self, codes):
            now = datetime.now(timezone.utc).isoformat()
            return [MarketQuote(
                code=code, name="demo fund", price=2.1, change_pct=0.0,
                volume=None, amount=None, source="eastmoney:fund_nav",
                as_of=nav_as_of, received_at=now, freshness="stale",
                is_fallback=False, asset_type="fund", market="CN-FUND",
                currency="CNY",
            ) for code in codes]

    monkeypatch.setattr("server.services.paper_scheduler.SessionLocal", lambda: db)
    result = PaperDailyScheduler(
        _Provider(), interval_seconds=10,
        market_providers={"cn-fund": _FundProvider()},
    ).run_once(date(2024, 6, 14))

    assert result["status"] == "completed"
    assert result["valuation_count"] == 1
    assert result["reason"] is None
    report = db.query(PaperDailyReport).filter(PaperDailyReport.account_id == fund["id"]).one()
    assert report.report_date == "2024-06-14"
    position = db.query(PaperAccountPosition).filter_by(account_id=fund["id"]).one()
    assert position.last_price == pytest.approx(2.1)


def test_scheduler_uses_market_specific_us_adapter(monkeypatch):
    db = _db()
    account = create_account(db, name="scheduler-us-adapter", market="us-equity",
                             initial_capital=100_000, max_position_weight=1.0)
    submit_order(
        db, account_id=account["id"], idempotency_key="scheduler-us-adapter-order",
        code="AAPL", side="buy", quantity=2, price=200.0,
        price_source="manual_input", price_freshness="manual",
        trade_date=date(2024, 6, 14),
    )

    quote_as_of = "2024-06-13T20:00:00+00:00"

    class _USProvider:
        source_name = "yahoo:chart"
        supports_historical_dates = True

        def fetch_quotes(self, codes):
            now = datetime.now(timezone.utc).isoformat()
            return [MarketQuote(
                code=code, name="Apple", price=210.0, change_pct=0.0,
                volume=1.0, amount=210.0, source="yahoo:chart",
                as_of=quote_as_of, received_at=now, freshness="delayed",
                is_fallback=False, asset_type="equity", market="US",
                currency="USD",
            ) for code in codes]

    monkeypatch.setattr("server.services.paper_scheduler.SessionLocal", lambda: db)
    result = PaperDailyScheduler(
        _Provider(), interval_seconds=10,
        market_providers={"us-equity": _USProvider()},
    ).run_once(date(2024, 6, 14))

    assert result["status"] == "completed"
    assert result["valuation_count"] == 1
    position = db.query(PaperAccountPosition).filter_by(account_id=account["id"]).one()
    assert position.last_price == pytest.approx(210.0)


def test_a_share_outage_does_not_block_available_fund_valuation(monkeypatch):
    db = _db()
    a_share = create_account(db, name="scheduler-a-outage", initial_capital=100_000, max_position_weight=1.0)
    fund = create_account(db, name="scheduler-fund-available", market="cn-fund",
                          initial_capital=100_000, max_position_weight=1.0)
    submit_order(db, account_id=a_share["id"], idempotency_key="scheduler-a-outage-order",
                 code="000001.SZ", side="buy", quantity=100, price=10)
    nav_as_of = "2024-06-13T07:00:00+00:00"
    register_fund_nav(code="110022", price=2.0, source="eastmoney:fund_nav",
                      as_of=nav_as_of, freshness="stale", db=db)
    submit_order(db, account_id=fund["id"], idempotency_key="scheduler-fund-available-order",
                 code="110022", side="buy", quantity=10, price=2.0,
                 price_source="eastmoney:fund_nav", price_as_of=nav_as_of,
                 price_freshness="stale")

    class _AUnavailable:
        def fetch_quotes(self, _codes):
            raise MarketDataUnavailableError([{"source": "a-share-test", "error": "offline"}])

    class _FundProvider:
        source_name = "eastmoney:fund_nav"
        supports_historical_dates = True

        def fetch_quotes(self, codes):
            now = datetime.now(timezone.utc).isoformat()
            return [MarketQuote(code=code, name="fund", price=2.2, change_pct=0.0,
                                volume=None, amount=None, source="eastmoney:fund_nav",
                                as_of=nav_as_of, received_at=now, freshness="stale",
                                is_fallback=False, asset_type="fund", market="CN-FUND",
                                currency="CNY") for code in codes]

    monkeypatch.setattr("server.services.paper_scheduler.SessionLocal", lambda: db)
    result = PaperDailyScheduler(
        _AUnavailable(), interval_seconds=10,
        market_providers={"cn-fund": _FundProvider()},
    ).run_once(date(2024, 6, 14))

    assert result["status"] == "completed"
    assert result["valuation_count"] == 1
    assert result["reason"] == "deferred_market_adapters:a-share"
    assert db.query(PaperDailyReport).filter(PaperDailyReport.account_id == fund["id"]).count() == 1


def test_deferred_cross_market_run_retries_after_provider_recovery(monkeypatch):
    db = _db()
    a_share = create_account(db, name="scheduler-retry-a", initial_capital=100_000, max_position_weight=1.0)
    fund = create_account(db, name="scheduler-retry-fund", market="cn-fund",
                          initial_capital=100_000, max_position_weight=1.0)
    submit_order(db, account_id=a_share["id"], idempotency_key="scheduler-retry-a-order",
                 code="000001.SZ", side="buy", quantity=100, price=10)
    nav_as_of = "2024-06-13T07:00:00+00:00"
    register_fund_nav(code="110022", price=2.0, source="eastmoney:fund_nav",
                      as_of=nav_as_of, freshness="stale", db=db)
    submit_order(db, account_id=fund["id"], idempotency_key="scheduler-retry-fund-order",
                 code="110022", side="buy", quantity=10, price=2.0,
                 price_source="eastmoney:fund_nav", price_as_of=nav_as_of,
                 price_freshness="stale")

    class _RecoveringFundProvider:
        source_name = "eastmoney:fund_nav"
        supports_historical_dates = True

        def __init__(self):
            self.calls = 0

        def fetch_quotes(self, codes):
            self.calls += 1
            if self.calls == 1:
                raise MarketDataUnavailableError([{"source": "eastmoney:fund_nav", "error": "offline"}])
            now = datetime.now(timezone.utc).isoformat()
            return [MarketQuote(code=code, name="fund", price=2.2, change_pct=0.0,
                                volume=None, amount=None, source="eastmoney:fund_nav",
                                as_of=nav_as_of, received_at=now, freshness="stale",
                                is_fallback=False, asset_type="fund", market="CN-FUND",
                                currency="CNY") for code in codes]

    monkeypatch.setattr("server.services.paper_scheduler.SessionLocal", lambda: db)
    recovering = _RecoveringFundProvider()
    scheduler = PaperDailyScheduler(_Provider(), interval_seconds=10,
                                    market_providers={"cn-fund": recovering})
    first = scheduler.run_once(date(2024, 6, 14))
    second = scheduler.run_once(date(2024, 6, 14))

    assert first["status"] == "completed"
    assert first["reason"] == "deferred_market_adapters:cn-fund"
    assert second["status"] == "completed"
    assert second["reason"] is None
    assert recovering.calls == 2
    assert db.query(PaperDailyReport).filter(PaperDailyReport.account_id == fund["id"]).count() == 1


def test_cross_market_scheduler_rejects_historical_snapshot_without_opt_in(monkeypatch):
    db = _db()
    fund = create_account(db, name="scheduler-history-gate", market="cn-fund",
                          initial_capital=100_000, max_position_weight=1.0)
    nav_as_of = "2024-06-13T07:00:00+00:00"
    register_fund_nav(code="110022", price=2.0, source="eastmoney:fund_nav",
                      as_of=nav_as_of, freshness="stale", db=db)
    submit_order(db, account_id=fund["id"], idempotency_key="scheduler-history-gate-order",
                 code="110022", side="buy", quantity=10, price=2.0,
                 price_source="eastmoney:fund_nav", price_as_of=nav_as_of,
                 price_freshness="stale")

    class _NoHistoryProvider:
        def fetch_quotes(self, _codes):
            raise AssertionError("historical snapshot provider must not be called")

    monkeypatch.setattr("server.services.paper_scheduler.SessionLocal", lambda: db)
    with pytest.raises(ValueError, match="cross_market_scheduler_date_must_be_today"):
        PaperDailyScheduler(_Provider(), interval_seconds=10,
                            market_providers={"cn-fund": _NoHistoryProvider()}).run_once(date(2024, 6, 14))


def test_scheduler_preserves_per_fund_nav_provenance_and_freshness(monkeypatch):
    db = _db()
    account = create_account(db, name="scheduler-fund-mixed-nav", market="cn-fund",
                             initial_capital=100_000, max_position_weight=1.0)
    first_as_of = "2024-06-13T07:00:00+00:00"
    second_as_of = "2024-06-12T07:00:00+00:00"
    for code, price, as_of in (("110022", 2.0, first_as_of), ("161725", 0.5, second_as_of)):
        register_fund_nav(code=code, price=price, source="eastmoney:fund_nav",
                          as_of=as_of, freshness="stale", db=db)
        submit_order(
            db, account_id=account["id"], idempotency_key=f"mixed-nav-{code}",
            code=code, side="buy", quantity=10.0, price=price,
            price_source="eastmoney:fund_nav", price_as_of=as_of,
            price_freshness="stale",
        )

    class _MixedFundProvider:
        source_name = "eastmoney:fund_nav"
        supports_historical_dates = True

        def fetch_quotes(self, codes):
            rows = {
                "110022": ("易方达消费行业股票", 2.1, first_as_of, "fresh"),
                "161725": ("招商中证白酒指数", 0.6, second_as_of, "delayed"),
            }
            now = datetime.now(timezone.utc).isoformat()
            return [
                MarketQuote(
                    code=code, name=rows[code][0], price=rows[code][1], change_pct=0.0,
                    volume=None, amount=None, source="eastmoney:fund_nav",
                    as_of=rows[code][2], received_at=now, freshness=rows[code][3],
                    is_fallback=False, asset_type="fund", market="CN-FUND", currency="CNY",
                )
                for code in codes
            ]

    monkeypatch.setattr("server.services.paper_scheduler.SessionLocal", lambda: db)
    result = PaperDailyScheduler(
        _Provider(), interval_seconds=10,
        market_providers={"cn-fund": _MixedFundProvider()},
    ).run_once(date(2024, 6, 14))

    assert result["status"] == "completed"
    assert result["valuation_count"] == 1
    valuation = db.query(PaperValuation).filter_by(account_id=account["id"]).one()
    assert valuation.price_freshness == "mixed"
    positions = {
        row.code: row.last_price
        for row in db.query(PaperAccountPosition).filter_by(account_id=account["id"]).all()
    }
    assert positions == {"110022": pytest.approx(2.1), "161725": pytest.approx(0.6)}


def test_invalid_a_share_timestamp_defers_only_a_share_market(monkeypatch):
    db = _db()
    a_share = create_account(db, name="scheduler-invalid-a-timestamp",
                             initial_capital=100_000, max_position_weight=1.0)
    fund = create_account(db, name="scheduler-valid-fund", market="cn-fund",
                          initial_capital=100_000, max_position_weight=1.0)
    submit_order(db, account_id=a_share["id"], idempotency_key="invalid-a-timestamp-order",
                 code="000001.SZ", side="buy", quantity=100, price=10)
    nav_as_of = "2024-06-13T07:00:00+00:00"
    register_fund_nav(code="110022", price=2.0, source="eastmoney:fund_nav",
                      as_of=nav_as_of, freshness="stale", db=db)
    submit_order(db, account_id=fund["id"], idempotency_key="valid-fund-order",
                 code="110022", side="buy", quantity=10, price=2.0,
                 price_source="eastmoney:fund_nav", price_as_of=nav_as_of,
                 price_freshness="stale")

    class _InvalidAProvider:
        supports_historical_dates = True

        def fetch_quotes(self, codes):
            now = datetime.now(timezone.utc).isoformat()
            return [
                MarketQuote(code=code, name="坏时间戳", price=10.0, change_pct=0.0,
                            volume=1.0, amount=10.0, source="test-feed",
                            as_of="not-an-iso-timestamp", received_at=now,
                            freshness="fresh", is_fallback=False)
                for code in codes
            ]

    class _FundProvider:
        source_name = "eastmoney:fund_nav"
        supports_historical_dates = True

        def fetch_quotes(self, codes):
            now = datetime.now(timezone.utc).isoformat()
            return [
                MarketQuote(code=code, name="基金", price=2.2, change_pct=0.0,
                            volume=None, amount=None, source="eastmoney:fund_nav",
                            as_of=nav_as_of, received_at=now, freshness="stale",
                            is_fallback=False, asset_type="fund", market="CN-FUND",
                            currency="CNY")
                for code in codes
            ]

    monkeypatch.setattr("server.services.paper_scheduler.SessionLocal", lambda: db)
    result = PaperDailyScheduler(
        _InvalidAProvider(), interval_seconds=10,
        market_providers={"cn-fund": _FundProvider()},
    ).run_once(date(2024, 6, 14))

    assert result["status"] == "completed"
    assert result["reason"] == "deferred_market_adapters:a-share"
    assert result["valuation_count"] == 1
    assert db.query(PaperDailyReport).filter(PaperDailyReport.account_id == fund["id"]).count() == 1


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
