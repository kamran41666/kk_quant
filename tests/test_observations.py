from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from quant_engine.data.live import LiveMarketDataProvider, MarketQuote
from server.models.database import Base
from server.models.schema import PaperLot, PaperOrder, PaperRebalanceItem, PaperRebalancePlan, Run, Strategy, StrategyObservation, StrategyObservationEvent
from server.services.observation import (
    create_observation,
    get_observation,
    list_observation_events,
    pause_observation,
    resume_observation,
    run_observation_tick,
    start_observation,
    stop_observation,
    _execute_rebalance_plan,
    _get_or_create_rebalance_plan,
    retry_rebalance_plan,
)
from server.services.paper_scheduler import PaperDailyScheduler
from server.services.paper_trading import create_account, submit_order
from server.services.strategy_evidence import build_manifest, serialize_manifest, strategy_fingerprint
from server.services.fund_nav_archive import archive_fund_nav_dataset


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _calendar_evidence():
    return {
        "source": "test:calendar",
        "content_hash": "c" * 64,
        "coverage_start": "2024-01-01",
        "coverage_end": "2024-12-31",
        "verified": True,
    }


def _strategy(db, *, completed=True):
    strategy = Strategy(name="观察策略", strategy_class="strategies.fake.Fake", params="{}")
    db.add(strategy)
    db.flush()
    if completed:
        db.add(Run(strategy_id=strategy.id, run_type="backtest", status="completed"))
        db.commit()
    return strategy


def _observation(db, *, duration=7, key="observation-key-1", start=date(2024, 6, 14)):
    account = create_account(db, name="观察账户", initial_capital=100_000, max_position_weight=1.0)
    strategy = _strategy(db)
    row = create_observation(
        db,
        account_id=account["id"],
        strategy_id=strategy.id,
        idempotency_key=key,
        duration_days=duration,
        allocation_pct=0.5,
        start_date=start,
    )
    return account, strategy, row


def _quote(code="000001.SZ", price=10.0, freshness="fresh"):
    stamp = "2024-06-13T08:00:00+08:00"
    return MarketQuote(
        code=code, name="测试证券", price=price, change_pct=0.0,
        volume=1000.0, amount=10000.0, source="test-feed",
        as_of=stamp, received_at=stamp, freshness=freshness, is_fallback=False,
    )


class _Quotes:
    def __init__(self, rows):
        self.rows = rows

    def fetch_quotes(self, codes):
        return [row for row in self.rows if row.code in codes]


def test_observation_requires_completed_backtest_and_create_is_idempotent():
    db = _db()
    account = create_account(db, name="未回测", initial_capital=100_000)
    strategy = _strategy(db, completed=False)
    with pytest.raises(ValueError, match="completed_backtest"):
        create_observation(db, account_id=account["id"], strategy_id=strategy.id,
                           idempotency_key="no-backtest", duration_days=7)

    db.add(Run(strategy_id=strategy.id, run_type="backtest", status="completed"))
    db.commit()
    first = create_observation(db, account_id=account["id"], strategy_id=strategy.id,
                               idempotency_key="same-observation", duration_days=7,
                               allocation_pct=0.25)
    replay = create_observation(db, account_id=account["id"], strategy_id=strategy.id,
                                idempotency_key="same-observation", duration_days=7,
                                allocation_pct=0.25)
    assert replay["id"] == first["id"]
    with pytest.raises(ValueError, match="different observation"):
        create_observation(db, account_id=account["id"], strategy_id=strategy.id,
                           idempotency_key="same-observation", duration_days=30,
                           allocation_pct=0.25)
    with pytest.raises(ValueError, match="different observation"):
        create_observation(db, account_id=account["id"], strategy_id=strategy.id,
                           idempotency_key="same-observation", duration_days=7,
                           allocation_pct=0.25, auto_trade=False)
    with pytest.raises(ValueError, match="different observation"):
        create_observation(db, account_id=account["id"], strategy_id=strategy.id,
                           idempotency_key="same-observation", duration_days=7,
                           allocation_pct=0.25, start_date=date(2024, 6, 14))
    with pytest.raises(ValueError, match="different observation"):
        create_observation(db, account_id=account["id"], strategy_id=strategy.id,
                           idempotency_key="same-observation", duration_days=7,
                           allocation_pct=0.5)


def test_observation_start_rejects_strategy_revision_after_evidence_run():
    db = _db()
    account = create_account(db, name="evidence-bound", initial_capital=100_000)
    strategy = _strategy(db)
    run = db.query(Run).filter(Run.strategy_id == strategy.id).one()
    manifest = build_manifest(
        market="a-share", strategy_class=strategy.strategy_class,
        params=strategy.params, start_date="2024-06-14", end_date="2024-06-20",
        benchmark="000300.SH", rebalance_frequency="weekly",
    )
    manifest["calendar_evidence"] = _calendar_evidence()
    run.market = "a-share"
    run.strategy_fingerprint = strategy_fingerprint(strategy.strategy_class, strategy.params)
    run.data_manifest = serialize_manifest(manifest)
    run.data_end = "2024-06-20"
    run.calendar_version = manifest["calendar_version"]
    run.execution_model = manifest["execution_model"]
    run.eligible_for_observation = True
    db.commit()
    row = create_observation(
        db, account_id=account["id"], strategy_id=strategy.id,
        idempotency_key="evidence-revision-check", duration_days=7,
        start_date=date(2024, 6, 14),
    )
    strategy.params = '{"top_n": 999}'
    db.commit()
    with pytest.raises(ValueError, match="evidence_stale"):
        start_observation(db, account["id"], row["id"])


def test_live_observation_stages_signal_then_executes_next_checkpoint(monkeypatch):
    db = _db()
    account, _, row = _observation(db, key="two-phase-live", start=date(2024, 6, 14))
    start_observation(db, account["id"], row["id"])

    class _LiveQuotes(LiveMarketDataProvider):
        def __init__(self):
            self.day = date(2024, 6, 14)

        def fetch_quotes(self, codes):
            stamp = f"{self.day.isoformat()}T08:00:00+08:00"
            return [MarketQuote(
                code=code, name="测试证券", price=10.0, change_pct=0.0,
                volume=1000.0, amount=10000.0, source="test-feed",
                as_of=stamp, received_at=stamp, freshness="fresh", is_fallback=False,
            ) for code in codes]

        def health(self):
            return []

    provider = _LiveQuotes()
    current_day = [date(2024, 6, 14)]
    monkeypatch.setattr("server.services.observation._trade_date", lambda: current_day[0])
    first = run_observation_tick(
        db, account_id=account["id"], observation_id=row["id"],
        signal_provider=lambda _row, _day: {"000001.SZ": 0.5},
        quote_provider=provider,
    )
    assert first["status"] == "pending"
    assert first["orders_count"] == 0
    assert db.query(PaperOrder).count() == 0

    replay = run_observation_tick(
        db, account_id=account["id"], observation_id=row["id"],
        signal_provider=lambda _row, _day: {"000001.SZ": 0.5},
        quote_provider=provider,
    )
    assert replay["reason"] == "signals_already_staged"
    assert db.query(PaperOrder).count() == 0

    current_day[0] = date(2024, 6, 17)
    provider.day = current_day[0]
    second = run_observation_tick(
        db, account_id=account["id"], observation_id=row["id"],
        signal_provider=lambda _row, _day: {},
        quote_provider=provider,
    )
    assert second["status"] == "completed"
    assert second["orders_count"] == 1
    assert second["data"]["signal_date"] == "2024-06-17"
    assert second["data"]["execution_date"] == "2024-06-17"
    plan = db.query(PaperRebalancePlan).one()
    assert plan.market == "a-share" and plan.status == "completed" and plan.order_count == 1 and plan.filled_count == 1
    item = db.query(PaperRebalanceItem).one()
    assert item.status == "completed" and item.order_id == second["orders"][0]["id"]

    current_day[0] = date(2024, 6, 18)
    provider.day = current_day[0]
    third = run_observation_tick(
        db, account_id=account["id"], observation_id=row["id"],
        signal_provider=lambda _row, _day: {},
        quote_provider=provider,
    )
    assert third["orders_count"] == 1
    assert third["orders"][0]["side"] == "sell"
    assert db.query(PaperOrder).count() == 2


def test_a_share_final_trading_day_signal_survives_window_boundary(monkeypatch):
    """A target staged on the calendar end date executes on the next session."""
    db = _db()
    account, _, row = _observation(db, key="a-share-final-day", start=date(2024, 6, 14))
    start_observation(db, account["id"], row["id"])

    class _LiveQuotes(LiveMarketDataProvider):
        def __init__(self): self.day = date(2024, 6, 20)
        def fetch_quotes(self, codes):
            stamp = f"{self.day.isoformat()}T08:00:00+08:00"
            return [MarketQuote(code=code, name="测试证券", price=10.0, change_pct=0.0,
                                volume=1000.0, amount=10000.0, source="test-feed",
                                as_of=stamp, received_at=stamp, freshness="fresh", is_fallback=False)
                    for code in codes]
        def health(self): return []

    provider = _LiveQuotes()
    current = [date(2024, 6, 20)]
    monkeypatch.setattr("server.services.observation._trade_date", lambda: current[0])
    staged = run_observation_tick(
        db, account_id=account["id"], observation_id=row["id"],
        signal_provider=lambda _row, _day: {"000001.SZ": 0.5}, quote_provider=provider,
    )
    assert staged["status"] == "pending"
    assert staged["observation"]["status"] == "running"

    # Weekend checkpoint is skipped without expiring the pending target.
    current[0] = date(2024, 6, 22)
    skipped = run_observation_tick(
        db, account_id=account["id"], observation_id=row["id"],
        signal_provider=lambda _row, _day: {}, quote_provider=provider,
    )
    assert skipped["status"] == "skipped"

    current[0] = date(2024, 6, 24)
    provider.day = current[0]
    executed = run_observation_tick(
        db, account_id=account["id"], observation_id=row["id"],
        signal_provider=lambda _row, _day: {}, quote_provider=provider,
    )
    assert executed["status"] == "completed"
    assert executed["orders_count"] == 1
    assert executed["orders"][0]["side"] == "buy"
    assert executed["observation"]["status"] == "completed"


def test_a_share_rebalance_cash_preflight_includes_minimum_fee(monkeypatch):
    db = _db()
    account = create_account(db, name="含费预检", initial_capital=1_004, max_position_weight=1.0)
    strategy = _strategy(db)
    row = create_observation(
        db, account_id=account["id"], strategy_id=strategy.id,
        idempotency_key="cash-fee-preflight", duration_days=7,
        allocation_pct=1.0, start_date=date(2024, 6, 14),
    )
    start_observation(db, account["id"], row["id"])

    class _LiveQuotes(LiveMarketDataProvider):
        def __init__(self): self.day = date(2024, 6, 14)
        def fetch_quotes(self, codes):
            stamp = f"{self.day.isoformat()}T08:00:00+08:00"
            return [MarketQuote(code=code, name="测试证券", price=10.0, change_pct=0.0,
                                volume=1000.0, amount=10000.0, source="test-feed",
                                as_of=stamp, received_at=stamp, freshness="fresh", is_fallback=False)
                    for code in codes]
        def health(self): return []

    provider = _LiveQuotes()
    current = [date(2024, 6, 14)]
    monkeypatch.setattr("server.services.observation._trade_date", lambda: current[0])
    staged = run_observation_tick(
        db, account_id=account["id"], observation_id=row["id"],
        signal_provider=lambda _row, _day: {"000001.SZ": 1.0}, quote_provider=provider,
    )
    assert staged["status"] == "pending"
    current[0] = date(2024, 6, 17)
    provider.day = current[0]
    blocked = run_observation_tick(
        db, account_id=account["id"], observation_id=row["id"],
        signal_provider=lambda _row, _day: {}, quote_provider=provider,
    )
    assert blocked["status"] == "blocked"
    assert "REBALANCE_CASH_PREFLIGHT" in blocked["reason"]
    assert db.query(PaperOrder).count() == 0


def test_cross_day_recovery_claims_order_committed_before_worker_crash(monkeypatch):
    """A fill committed before a crash is reconciled without a duplicate order."""
    db = _db()
    account, _, row = _observation(db, key="cross-day-committed-fill", start=date(2024, 6, 14))
    start_observation(db, account["id"], row["id"])

    class _LiveQuotes(LiveMarketDataProvider):
        def __init__(self): self.day = date(2024, 6, 14)
        def fetch_quotes(self, codes):
            stamp = f"{self.day.isoformat()}T08:00:00+08:00"
            price = 10.0 if self.day in {date(2024, 6, 14), date(2024, 6, 17)} else 12.0
            return [MarketQuote(code=code, name="测试证券", price=price, change_pct=0.0,
                                volume=1000.0, amount=10000.0, source="test-feed",
                                as_of=stamp, received_at=stamp, freshness="fresh", is_fallback=False)
                    for code in codes]
        def health(self): return []

    provider = _LiveQuotes()
    current = [date(2024, 6, 14)]
    monkeypatch.setattr("server.services.observation._trade_date", lambda: current[0])
    run_observation_tick(
        db, account_id=account["id"], observation_id=row["id"],
        signal_provider=lambda _row, _day: {"000001.SZ": 0.5}, quote_provider=provider,
    )

    current[0] = date(2024, 6, 17)
    provider.day = current[0]
    from server.services import observation as observation_module
    real_submit = observation_module.submit_order

    def crash_after_commit(*args, **kwargs):
        result = real_submit(*args, **kwargs)
        raise RuntimeError("simulated crash after paper fill commit")

    monkeypatch.setattr(observation_module, "submit_order", crash_after_commit)
    partial = run_observation_tick(
        db, account_id=account["id"], observation_id=row["id"],
        signal_provider=lambda _row, _day: {}, quote_provider=provider,
    )
    assert partial["status"] == "partial"
    assert db.query(PaperOrder).count() == 1
    item = db.query(PaperRebalanceItem).one()
    assert item.status == "pending" and item.order_id is None

    monkeypatch.setattr(observation_module, "submit_order", real_submit)
    current[0] = date(2024, 6, 18)
    provider.day = current[0]
    resumed = run_observation_tick(
        db, account_id=account["id"], observation_id=row["id"],
        signal_provider=lambda _row, _day: {}, quote_provider=provider,
    )
    assert resumed["status"] == "pending"
    assert db.query(PaperOrder).count() == 1
    plan = db.query(PaperRebalancePlan).one()
    item = db.query(PaperRebalanceItem).one()
    assert plan.status == "completed" and plan.execution_date == "2024-06-17"
    assert plan.filled_count == 1
    assert item.status == "completed" and item.order_id is not None and item.price == 10.0


def test_observation_cannot_be_read_or_controlled_through_another_account():
    db = _db()
    account, _, row = _observation(db, key="owner-observation")
    other = create_account(db, name="另一个账户", initial_capital=100_000)
    with pytest.raises(KeyError, match="observation not found"):
        get_observation(db, other["id"], row["id"])


def test_fund_observation_stages_nav_signal_then_executes_next_nav(monkeypatch):
    db = _db()
    account = create_account(db, name="基金观察", market="cn-fund", initial_capital=1_000, max_position_weight=1.0)
    strategy = Strategy(name="基金动量", market="cn-fund", strategy_class="strategies.fund_nav_momentum.FundNavMomentumStrategy", params="{}")
    db.add(strategy); db.flush()
    dataset = archive_fund_nav_dataset(db, code="110022", rows=[
        {"date": "2024-06-13", "nav": 2.0}, {"date": "2024-06-14", "nav": 2.1}, {"date": "2024-06-17", "nav": 2.2}, {"date": "2024-06-18", "nav": 2.3}
    ], start_date=date(2024, 6, 13), end_date=date(2024, 6, 18))
    manifest = build_manifest(market="cn-fund", strategy_class=strategy.strategy_class, params="{}", start_date="2024-06-13", end_date="2024-06-18", benchmark=None, rebalance_frequency="daily", universe=["110022"], dataset_manifests=[dataset], nav_rule="published_nav_next_valid_day")
    manifest["calendar_evidence"] = _calendar_evidence()
    run = Run(strategy_id=strategy.id, run_type="backtest", status="completed", market="cn-fund", strategy_fingerprint=strategy_fingerprint(strategy.strategy_class, strategy.params), data_manifest=serialize_manifest(manifest), data_end="2024-06-18", calendar_version=manifest["calendar_version"], execution_model=manifest["execution_model"], eligible_for_observation=True)
    db.add(run); db.commit()
    row = create_observation(db, account_id=account["id"], strategy_id=strategy.id, idempotency_key="fund-observation-1", duration_days=7, allocation_pct=1.0, start_date=date(2024, 6, 14))
    start_observation(db, account["id"], row["id"])

    class _FundQuotes:
        def __init__(self): self.day = date(2024, 6, 14)
        def fetch_quotes(self, codes):
            stamp = f"{self.day.isoformat()}T15:00:00+08:00"
            return [MarketQuote(code=code, name="测试基金", price={date(2024, 6, 14): 2.1, date(2024, 6, 17): 2.2, date(2024, 6, 18): 2.3}[self.day], change_pct=0.0, volume=None, amount=None, source="eastmoney:fund_nav", as_of=stamp, received_at=stamp, freshness="stale", is_fallback=False, asset_type="fund", market="CN-FUND", currency="CNY") for code in codes]
    provider = _FundQuotes()
    current = [date(2024, 6, 17)]
    monkeypatch.setattr("server.services.observation._trade_date", lambda: current[0])
    first = run_observation_tick(db, account_id=account["id"], observation_id=row["id"], as_of=date(2024, 6, 14), signal_provider=lambda _row, _day: {"110022": 1.0}, quote_provider=provider)
    assert first["status"] == "pending" and first["orders_count"] == 0
    current[0] = date(2024, 6, 17); provider.day = current[0]
    second = run_observation_tick(db, account_id=account["id"], observation_id=row["id"], as_of=current[0], signal_provider=lambda _row, _day: {}, quote_provider=provider)
    assert second["orders_count"] == 1 and second["orders"][0]["market"] == "cn-fund"
    assert db.query(PaperOrder).count() == 1
    plan = db.query(PaperRebalancePlan).one()
    assert plan.status == "completed" and plan.order_count == 1 and plan.filled_count == 1
    item = db.query(PaperRebalanceItem).one()
    assert item.status == "completed" and item.order_id == second["orders"][0]["id"]
    current[0] = date(2024, 6, 18); provider.day = current[0]
    third = run_observation_tick(db, account_id=account["id"], observation_id=row["id"], as_of=current[0], signal_provider=lambda _row, _day: {}, quote_provider=provider)
    assert third["orders_count"] == 1 and third["orders"][0]["side"] == "sell"


def test_fund_rebalance_plan_resumes_after_submit_failure(monkeypatch):
    db = _db()
    account = create_account(db, name="基金计划恢复", market="cn-fund", initial_capital=1_000, max_position_weight=1.0)
    strategy = Strategy(name="基金动量恢复", market="cn-fund", strategy_class="strategies.fund_nav_momentum.FundNavMomentumStrategy", params="{}")
    db.add(strategy); db.flush()
    dataset = archive_fund_nav_dataset(db, code="110022", rows=[
        {"date": "2024-06-13", "nav": 2.0}, {"date": "2024-06-14", "nav": 2.1}, {"date": "2024-06-17", "nav": 2.2}
    ], start_date=date(2024, 6, 13), end_date=date(2024, 6, 17))
    manifest = build_manifest(market="cn-fund", strategy_class=strategy.strategy_class, params="{}", start_date="2024-06-13", end_date="2024-06-17", benchmark=None, rebalance_frequency="daily", universe=["110022"], dataset_manifests=[dataset], nav_rule="published_nav_next_valid_day")
    manifest["calendar_evidence"] = _calendar_evidence()
    db.add(Run(strategy_id=strategy.id, run_type="backtest", status="completed", market="cn-fund", strategy_fingerprint=strategy_fingerprint(strategy.strategy_class, strategy.params), data_manifest=serialize_manifest(manifest), data_end="2024-06-17", calendar_version=manifest["calendar_version"], execution_model=manifest["execution_model"], eligible_for_observation=True))
    db.commit()
    row = create_observation(db, account_id=account["id"], strategy_id=strategy.id, idempotency_key="fund-plan-retry", duration_days=7, allocation_pct=1.0, start_date=date(2024, 6, 14))
    start_observation(db, account["id"], row["id"])

    class _FundQuotes:
        def __init__(self): self.day = date(2024, 6, 14)
        def fetch_quotes(self, codes):
            stamp = f"{self.day.isoformat()}T15:00:00+08:00"
            values = {date(2024, 6, 14): 2.1, date(2024, 6, 17): 2.2, date(2024, 6, 18): 2.05}
            return [MarketQuote(code=code, name="测试基金", price=values[self.day], change_pct=0.0, volume=None, amount=None, source="eastmoney:fund_nav", as_of=stamp, received_at=stamp, freshness="stale", is_fallback=False, asset_type="fund", market="CN-FUND", currency="CNY") for code in codes]
    provider = _FundQuotes()
    current = [date(2024, 6, 14)]
    monkeypatch.setattr("server.services.observation._trade_date", lambda: current[0])
    first = run_observation_tick(db, account_id=account["id"], observation_id=row["id"], signal_provider=lambda _row, _day: {"110022": 1.0}, quote_provider=provider)
    assert first["status"] == "pending"

    current[0] = date(2024, 6, 17); provider.day = current[0]
    from server.services import observation as observation_module
    real_submit = observation_module.submit_order
    attempts = {"count": 0}
    def flaky_submit(*args, **kwargs):
        if attempts["count"] == 0:
            attempts["count"] += 1
            raise RuntimeError("simulated worker interruption")
        return real_submit(*args, **kwargs)
    monkeypatch.setattr(observation_module, "submit_order", flaky_submit)
    partial = run_observation_tick(db, account_id=account["id"], observation_id=row["id"], signal_provider=lambda _row, _day: {}, quote_provider=provider)
    assert partial["status"] == "partial" and db.query(PaperOrder).count() == 0
    assert db.query(PaperRebalancePlan).one().status == "partial"

    monkeypatch.setattr(observation_module, "submit_order", real_submit)
    # The worker resumes on the next valid day. The durable item must use the
    # refreshed quote and new execution date rather than yesterday's evidence.
    current[0] = date(2024, 6, 18)
    provider.day = current[0]
    resumed = run_observation_tick(db, account_id=account["id"], observation_id=row["id"], signal_provider=lambda _row, _day: {}, quote_provider=provider)
    assert resumed["status"] == "completed" and resumed["orders_count"] == 1, resumed
    assert db.query(PaperOrder).count() == 1
    plan = db.query(PaperRebalancePlan).one()
    item = db.query(PaperRebalanceItem).one()
    assert plan.status == "completed" and plan.execution_date == "2024-06-18"
    assert plan.filled_count == 1
    assert item.price == 2.05 and item.price_as_of.startswith("2024-06-18")


def test_rebalance_plan_active_lease_blocks_second_worker(monkeypatch):
    """A concurrent scheduler must not submit while another lease is live."""
    db = _db()
    account, _, observation = _observation(db, key="lease-active")
    future = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    plan = PaperRebalancePlan(
        id="lease-active-plan", observation_id=observation["id"], account_id=account["id"],
        market="a-share", signal_date="2024-06-14", execution_date="2024-06-17",
        status="planned", order_count=1, lease_owner="another-worker", lease_until=future,
    )
    db.add(plan)
    db.add(PaperRebalanceItem(
        plan_id=plan.id, code="000001.SZ", side="buy", quantity=100, price=10,
        price_source="test-feed", price_as_of="2024-06-17T08:00:00+08:00",
        price_freshness="fresh", idempotency_key="lease-active-item", status="pending",
    ))
    db.commit()
    monkeypatch.setattr("server.services.observation.submit_order", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("lease owner must submit")))
    assert _execute_rebalance_plan(db, plan) == []
    db.refresh(plan)
    assert plan.status == "planned"
    assert plan.lease_owner == "another-worker"
    assert db.query(PaperOrder).count() == 0


def test_rebalance_plan_expired_lease_is_reclaimed(monkeypatch):
    """A crashed worker's expired lease is safely taken over and released."""
    db = _db()
    account, _, observation = _observation(db, key="lease-expired")
    past = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    plan = PaperRebalancePlan(
        id="lease-expired-plan", observation_id=observation["id"], account_id=account["id"],
        market="a-share", signal_date="2024-06-14", execution_date="2024-06-17",
        status="partial", order_count=1, lease_owner="crashed-worker", lease_until=past,
    )
    db.add(plan)
    db.add(PaperRebalanceItem(
        plan_id=plan.id, code="000001.SZ", side="buy", quantity=100, price=10,
        price_source="test-feed", price_as_of="2024-06-17T08:00:00+08:00",
        price_freshness="fresh", idempotency_key="lease-expired-item", status="pending",
    ))
    db.commit()
    monkeypatch.setattr("server.services.observation.submit_order", lambda *_args, **_kwargs: {
        "id": "paper-order-lease", "status": "filled", "filled_quantity": 100,
    })
    assert _execute_rebalance_plan(db, plan) == [{"id": "paper-order-lease", "status": "filled", "filled_quantity": 100}]
    db.refresh(plan)
    assert plan.status == "completed"
    assert plan.lease_owner is None and plan.lease_until is None


def test_blocked_rebalance_plan_has_explicit_operator_retry():
    db = _db()
    account, _, observation = _observation(db, key="blocked-plan-retry")
    plan = PaperRebalancePlan(
        id="blocked-plan-retry", observation_id=observation["id"], account_id=account["id"],
        market="a-share", signal_date="2024-06-14", execution_date="2024-06-17",
        status="blocked", order_count=1, last_error="t_plus_one_lock",
    )
    db.add(plan)
    db.add(PaperRebalanceItem(
        plan_id=plan.id, code="000001.SZ", side="sell", quantity=100, price=10,
        price_source="test-feed", price_as_of="2024-06-17T08:00:00+08:00",
        price_freshness="fresh", idempotency_key="blocked-plan-item", status="blocked",
        error="t_plus_one_lock", order_id="rejected-order",
    ))
    db.commit()
    result = retry_rebalance_plan(
        db, account_id=account["id"], observation_id=observation["id"], plan_id=plan.id,
    )
    assert result["status"] == "partial" and result["reset_items"] == 1
    db.refresh(plan)
    item = db.query(PaperRebalanceItem).one()
    assert plan.status == "partial" and plan.last_error is None
    assert item.status == "pending" and item.order_id is None and "-retry-" in item.idempotency_key


def test_blocked_rebalance_plan_is_not_forked_on_later_market_day():
    """A blocked envelope stays an explicit operator decision point."""
    db = _db()
    account, _, observation = _observation(db, key="blocked-no-fork")
    plan = PaperRebalancePlan(
        id="blocked-no-fork-plan", observation_id=observation["id"], account_id=account["id"],
        market="a-share", signal_date="2024-06-14", execution_date="2024-06-17",
        status="blocked", order_count=1, last_error="t_plus_one_lock",
    )
    db.add(plan)
    db.add(PaperRebalanceItem(
        plan_id=plan.id, code="000001.SZ", side="sell", quantity=100, price=10,
        price_source="test-feed", price_as_of="2024-06-17T08:00:00+08:00",
        price_freshness="fresh", idempotency_key="blocked-no-fork-item", status="blocked",
        error="t_plus_one_lock",
    ))
    db.commit()
    quote = _quote(price=11.0)
    kept = _get_or_create_rebalance_plan(
        db, observation=db.query(StrategyObservation).filter(StrategyObservation.id == observation["id"]).one(),
        account_id=account["id"], market="a-share", signal_date="2024-06-14", execution_date="2024-06-18",
        pending={"000001.SZ": 0.5}, quotes={"000001.SZ": quote},
    )
    assert kept.id == plan.id
    assert kept.status == "blocked"
    assert kept.execution_date == "2024-06-17"
    assert db.query(PaperRebalancePlan).count() == 1


def test_fund_final_day_signal_executes_after_calendar_end(monkeypatch):
    """A Friday signal must remain executable when the seven-day window ends Sunday."""
    db = _db()
    account = create_account(db, name="基金周末结算", market="cn-fund", initial_capital=1_000, max_position_weight=1.0)
    strategy = Strategy(name="基金周末策略", market="cn-fund", strategy_class="strategies.fund_nav_momentum.FundNavMomentumStrategy", params="{}")
    db.add(strategy); db.flush()
    dataset = archive_fund_nav_dataset(db, code="110022", rows=[
        {"date": "2024-06-21", "nav": 2.2}, {"date": "2024-06-24", "nav": 2.3}
    ], start_date=date(2024, 6, 21), end_date=date(2024, 6, 24))
    manifest = build_manifest(market="cn-fund", strategy_class=strategy.strategy_class, params="{}", start_date="2024-06-21", end_date="2024-06-24", benchmark=None, rebalance_frequency="daily", universe=["110022"], dataset_manifests=[dataset], nav_rule="published_nav_next_valid_day")
    manifest["calendar_evidence"] = _calendar_evidence()
    db.add(Run(strategy_id=strategy.id, run_type="backtest", status="completed", market="cn-fund", strategy_fingerprint=strategy_fingerprint(strategy.strategy_class, strategy.params), data_manifest=serialize_manifest(manifest), data_end="2024-06-24", calendar_version=manifest["calendar_version"], execution_model=manifest["execution_model"], eligible_for_observation=True))
    db.commit()
    row = create_observation(db, account_id=account["id"], strategy_id=strategy.id, idempotency_key="fund-weekend-end", duration_days=7, allocation_pct=1.0, start_date=date(2024, 6, 17))
    start_observation(db, account["id"], row["id"])

    class _FundQuotes:
        def __init__(self): self.day = date(2024, 6, 21)
        def fetch_quotes(self, codes):
            stamp = f"{self.day.isoformat()}T15:00:00+08:00"
            price = {date(2024, 6, 21): 2.2, date(2024, 6, 24): 2.3}[self.day]
            return [MarketQuote(code=code, name="测试基金", price=price, change_pct=0.0, volume=None, amount=None, source="eastmoney:fund_nav", as_of=stamp, received_at=stamp, freshness="stale", is_fallback=False, asset_type="fund", market="CN-FUND", currency="CNY") for code in codes]

    provider = _FundQuotes()
    current = [date(2024, 6, 21)]
    monkeypatch.setattr("server.services.observation._trade_date", lambda: current[0])
    staged = run_observation_tick(db, account_id=account["id"], observation_id=row["id"], signal_provider=lambda _row, _day: {"110022": 1.0}, quote_provider=provider)
    assert staged["status"] == "pending"

    current[0] = date(2024, 6, 24)
    provider.day = current[0]
    executed = run_observation_tick(db, account_id=account["id"], observation_id=row["id"], signal_provider=lambda *_args: (_ for _ in ()).throw(AssertionError("final-day execution must not request a fresh signal")), quote_provider=provider)
    assert executed["status"] == "completed"
    assert executed["orders_count"] == 1
    assert executed["data"]["signal_date"] == "2024-06-24"
    assert executed["data"]["execution_date"] == "2024-06-24"
    assert db.query(PaperRebalancePlan).one().status == "completed"


def test_lifecycle_actions_are_bounded_and_repeatable():
    db = _db()
    account, _, row = _observation(db)
    oid = row["id"]
    assert start_observation(db, account["id"], oid)["status"] == "running"
    assert start_observation(db, account["id"], oid)["status"] == "running"
    assert pause_observation(db, account["id"], oid)["status"] == "paused"
    assert pause_observation(db, account["id"], oid)["status"] == "paused"
    assert resume_observation(db, account["id"], oid)["status"] == "running"
    assert resume_observation(db, account["id"], oid)["status"] == "running"
    assert stop_observation(db, account["id"], oid)["status"] == "stopped"
    assert stop_observation(db, account["id"], oid)["status"] == "stopped"
    events = list_observation_events(db, account["id"], oid)
    assert [item["event_type"] for item in events] == ["created", "started", "paused", "resumed", "stopped"]


def test_tick_uses_paper_order_chain_and_manual_order_can_coexist():
    db = _db()
    account, _, row = _observation(db)
    oid = row["id"]
    start_observation(db, account["id"], oid)
    result = run_observation_tick(
        db,
        account_id=account["id"],
        observation_id=oid,
        as_of=date(2024, 6, 14),
        signal_provider=lambda _row, _day: {"000001.SZ": 0.5},
        quote_provider=_Quotes([_quote()]),
    )
    assert result["status"] == "completed"
    assert result["live_execution"] is False
    assert result["orders_count"] == 1
    assert result["orders"][0]["status"] == "filled"
    order = db.query(PaperOrder).one()
    lot = db.query(PaperLot).one()
    assert order.created_at.startswith("2024-06-14T")
    assert lot.buy_at == order.created_at
    assert lot.unlock_date == "2024-06-17"

    # A user order remains available during an active observation and still
    # goes through the same durable paper ledger and risk checks.
    manual = submit_order(db, account_id=account["id"], idempotency_key="manual-coexist-1",
                          code="000002.SZ", side="buy", quantity=100, price=10)
    assert manual["status"] == "filled"

    replay = run_observation_tick(
        db, account_id=account["id"], observation_id=oid, as_of=date(2024, 6, 14),
        signal_provider=lambda _row, _day: {"000001.SZ": 0.5},
        quote_provider=_Quotes([_quote()]),
    )
    assert replay["reason"] == "tick_already_recorded"
    assert db.query(StrategyObservationEvent).filter(StrategyObservationEvent.observation_id == oid).count() == 3


def test_manual_position_is_not_sold_by_strategy_rebalance():
    db = _db()
    account, _, row = _observation(db, key="manual-position-protected", start=date(2024, 6, 14))
    oid = row["id"]
    start_observation(db, account["id"], oid)
    first = run_observation_tick(
        db,
        account_id=account["id"],
        observation_id=oid,
        as_of=date(2024, 6, 14),
        signal_provider=lambda _row, _day: {"000001.SZ": 0.5},
        quote_provider=_Quotes([_quote("000001.SZ", 10.0), _quote("000002.SZ", 20.0)]),
    )
    assert first["status"] == "completed"
    manual = submit_order(
        db,
        account_id=account["id"],
        idempotency_key="manual-protected-buy",
        code="000002.SZ",
        side="buy",
        quantity=100,
        price=20,
    )
    assert manual["status"] == "filled"

    second = run_observation_tick(
        db,
        account_id=account["id"],
        observation_id=oid,
        as_of=date(2024, 6, 17),
        signal_provider=lambda _row, _day: {"000001.SZ": 0.5},
        quote_provider=_Quotes([_quote("000001.SZ", 10.0), _quote("000002.SZ", 20.0)]),
    )
    assert second["status"] == "completed"
    assert "000002.SZ" in second["data"]["manual_positions_protected"]
    assert db.query(PaperOrder).filter(PaperOrder.code == "000002.SZ", PaperOrder.side == "sell").count() == 0


def test_manual_add_on_strategy_code_is_kept_out_of_strategy_sell():
    db = _db()
    account, _, row = _observation(db, key="manual-add-same-code", start=date(2024, 6, 14))
    oid = row["id"]
    start_observation(db, account["id"], oid)
    first = run_observation_tick(
        db, account_id=account["id"], observation_id=oid, as_of=date(2024, 6, 14),
        signal_provider=lambda _row, _day: {"000001.SZ": 0.5},
        quote_provider=_Quotes([_quote("000001.SZ", 10.0)]),
    )
    assert first["orders_count"] == 1
    manual = submit_order(
        db, account_id=account["id"], idempotency_key="manual-add-same-code-order",
        code="000001.SZ", side="buy", quantity=100, price=10,
    )
    assert manual["status"] == "filled"

    second = run_observation_tick(
        db, account_id=account["id"], observation_id=oid, as_of=date(2024, 6, 17),
        signal_provider=lambda _row, _day: {"000001.SZ": 0.2},
        quote_provider=_Quotes([_quote("000001.SZ", 10.0)]),
    )
    assert second["status"] == "completed"
    # The strategy sleeve moves from 2,500 to 1,000 shares; the 100-share
    # manual add remains untouched in its own lot.
    lots = db.query(PaperLot).filter(PaperLot.code == "000001.SZ").order_by(PaperLot.owner).all()
    assert [(lot.owner, lot.remaining_quantity) for lot in lots] == [("manual", 100), ("strategy", 1000)]


def test_two_observations_never_consume_each_others_strategy_lots():
    db = _db()
    account, strategy, first_row = _observation(db, key="isolated-observation-1", start=date(2024, 6, 14))
    second_row = create_observation(
        db, account_id=account["id"], strategy_id=strategy.id,
        idempotency_key="isolated-observation-2", duration_days=7,
        allocation_pct=0.5, start_date=date(2024, 6, 14),
    )
    start_observation(db, account["id"], first_row["id"])
    start_observation(db, account["id"], second_row["id"])
    quote_provider = _Quotes([_quote("000001.SZ", 10.0)])

    first = run_observation_tick(
        db, account_id=account["id"], observation_id=first_row["id"],
        as_of=date(2024, 6, 14),
        signal_provider=lambda _row, _day: {"000001.SZ": 0.5},
        quote_provider=quote_provider,
    )
    second = run_observation_tick(
        db, account_id=account["id"], observation_id=second_row["id"],
        as_of=date(2024, 6, 14),
        signal_provider=lambda _row, _day: {"000001.SZ": 0.5},
        quote_provider=quote_provider,
    )
    assert first["orders_count"] == 1
    assert second["orders_count"] == 1
    lots = db.query(PaperLot).filter(PaperLot.owner == "strategy").all()
    assert {lot.owner_id for lot in lots} == {first_row["id"], second_row["id"]}

    reduced = run_observation_tick(
        db, account_id=account["id"], observation_id=first_row["id"],
        as_of=date(2024, 6, 17),
        signal_provider=lambda _row, _day: {"000001.SZ": 0.2},
        quote_provider=quote_provider,
    )
    assert reduced["orders_count"] == 1
    own = db.query(PaperLot).filter(PaperLot.owner == "strategy", PaperLot.owner_id == first_row["id"]).all()
    other = db.query(PaperLot).filter(PaperLot.owner == "strategy", PaperLot.owner_id == second_row["id"]).all()
    assert sum(lot.remaining_quantity for lot in own) == 1000
    assert sum(lot.remaining_quantity for lot in other) == 2500


def test_tick_fails_closed_on_stale_or_missing_quotes_without_orders():
    db = _db()
    account, _, row = _observation(db, key="blocked-observation")
    oid = row["id"]
    start_observation(db, account["id"], oid)
    result = run_observation_tick(
        db, account_id=account["id"], observation_id=oid, as_of=date(2024, 6, 14),
        signal_provider=lambda _row, _day: {"000001.SZ": 0.5},
        quote_provider=_Quotes([_quote(freshness="stale")]),
    )
    assert result["status"] == "blocked"
    assert "LIVE_QUOTE_INCOMPLETE" in result["reason"]
    assert db.query(StrategyObservationEvent).filter(StrategyObservationEvent.event_type == "tick_blocked").count() == 1


def test_fund_tick_fails_closed_when_trading_calendar_is_unavailable(monkeypatch):
    """A fund observation must block, rather than record a false closure."""
    db = _db()
    account = create_account(db, name="基金日历不可用", market="cn-fund", initial_capital=1_000, max_position_weight=1.0)
    strategy = Strategy(
        name="基金日历策略", market="cn-fund",
        strategy_class="strategies.fund_nav_momentum.FundNavMomentumStrategy", params="{}",
    )
    db.add(strategy); db.flush()
    dataset = archive_fund_nav_dataset(
        db, code="110022",
        rows=[{"date": "2024-06-14", "nav": 2.1}],
        start_date=date(2024, 6, 14), end_date=date(2024, 6, 14),
    )
    manifest = build_manifest(
        market="cn-fund", strategy_class=strategy.strategy_class, params="{}",
        start_date="2024-06-14", end_date="2024-06-14", benchmark=None,
        rebalance_frequency="daily", universe=["110022"],
        dataset_manifests=[dataset], nav_rule="published_nav_next_valid_day",
    )
    manifest["calendar_evidence"] = _calendar_evidence()
    db.add(Run(
        strategy_id=strategy.id, run_type="backtest", status="completed", market="cn-fund",
        strategy_fingerprint=strategy_fingerprint(strategy.strategy_class, strategy.params),
        data_manifest=serialize_manifest(manifest), data_end="2024-06-14",
        calendar_version=manifest["calendar_version"], execution_model=manifest["execution_model"],
        eligible_for_observation=True,
    ))
    db.commit()
    row = create_observation(
        db, account_id=account["id"], strategy_id=strategy.id,
        idempotency_key="fund-calendar-unavailable", duration_days=7,
        allocation_pct=1.0, start_date=date(2024, 6, 14),
    )
    start_observation(db, account["id"], row["id"])
    monkeypatch.setattr("server.services.observation.market_session_status", lambda *_args: "calendar_unavailable")

    result = run_observation_tick(
        db, account_id=account["id"], observation_id=row["id"], as_of=date(2024, 6, 14),
        signal_provider=lambda *_args: {"110022": 1.0},
        quote_provider=_Quotes([]),
    )

    assert result["status"] == "blocked"
    assert "TRADING_CALENDAR_UNAVAILABLE" in result["reason"]
    event = db.query(StrategyObservationEvent).filter(
        StrategyObservationEvent.observation_id == row["id"],
        StrategyObservationEvent.event_type == "tick_blocked",
    ).one()
    assert "TRADING_CALENDAR_UNAVAILABLE" in event.reason


def test_tick_expiry_is_terminal():
    db = _db()
    account, _, row = _observation(db, key="expiry-observation")
    oid = row["id"]
    start_observation(db, account["id"], oid)
    result = run_observation_tick(db, account_id=account["id"], observation_id=oid, as_of=date(2024, 6, 30))
    assert result["status"] == "completed"
    assert result["observation"]["status"] == "completed"
    assert result["reason"] == "observation_end_date_reached"


def test_live_observation_rejects_historical_as_of_without_replay_provider():
    db = _db()
    account, _, row = _observation(db, key="live-historical-date",
                                   start=date.today() - timedelta(days=6))
    start_observation(db, account["id"], row["id"])
    with pytest.raises(ValueError, match="live_observation_date_must_be_today"):
        run_observation_tick(
            db,
            account_id=account["id"],
            observation_id=row["id"],
            as_of=date.today() - timedelta(days=1),
            signal_provider=lambda _row, _day: {"000001.SZ": 0.5},
        )


def test_paper_scheduler_forwards_close_to_running_observation(monkeypatch):
    db = _db()
    account, _, row = _observation(db, key="scheduler-observation")
    start_observation(db, account["id"], row["id"])
    calls = []

    def fake_tick(db_arg, **kwargs):
        calls.append(kwargs)
        return {"status": "blocked", "live_execution": False}

    monkeypatch.setattr("server.services.observation.run_observation_tick", fake_tick)
    monkeypatch.setattr("server.services.paper_scheduler.SessionLocal", lambda: db)
    scheduler = PaperDailyScheduler(_Quotes([]), interval_seconds=10)
    result = scheduler.run_once(date(2024, 6, 14))
    assert result["status"] == "completed"
    assert calls == [{"account_id": account["id"], "observation_id": row["id"], "as_of": date(2024, 6, 14), "quote_provider": scheduler.provider}]
