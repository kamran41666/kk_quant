from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from server.api.observations import CreateObservationRequest, create_paper_observation
from server.models.database import Base
from server.models.schema import PaperAccount, Run, Strategy, StrategyObservation
from server.services.observation import create_observation
from server.services.paper_scheduler import PaperDailyScheduler
from server.services.paper_trading import create_account


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _completed_strategy(db):
    strategy = Strategy(name="跨市场边界测试", strategy_class="strategies.fake.Fake", params="{}")
    db.add(strategy)
    db.flush()
    db.add(Run(strategy_id=strategy.id, run_type="backtest", status="completed"))
    db.commit()
    return strategy


def test_non_a_share_observation_creation_is_rejected_before_strategy_setup():
    db = _db()
    account = create_account(db, name="基金观察账户", market="cn-fund", initial_capital=10_000)
    strategy = _completed_strategy(db)
    with pytest.raises(ValueError, match="strategy_observation_requires_a_share_account"):
        create_observation(
            db, account_id=account["id"], strategy_id=strategy.id,
            idempotency_key="fund-observation-1", duration_days=7,
        )
    assert db.query(StrategyObservation).count() == 0


def test_observation_http_endpoint_returns_stable_409_for_non_a_share_account():
    db = _db()
    account = create_account(db, name="美股观察账户", market="us-equity", initial_capital=10_000)
    strategy = _completed_strategy(db)
    request = CreateObservationRequest(
        strategy_id=strategy.id, idempotency_key="us-observation-1", duration_days=7,
    )
    with pytest.raises(HTTPException) as error:
        create_paper_observation(account["id"], request, db)
    assert error.value.status_code == 409
    assert error.value.detail == "strategy_observation_requires_a_share_account"


def test_scheduler_does_not_invoke_a_share_observation_engine_for_non_a_share_account(monkeypatch):
    db = _db()
    account = create_account(db, name="基金调度账户", market="cn-fund", initial_capital=10_000)
    strategy = _completed_strategy(db)
    # Simulate a legacy/corrupt row written before the creation guard. The
    # scheduler must still fail closed and skip it rather than relying only on
    # the API guard.
    db.add(StrategyObservation(
        account_id=account["id"], strategy_id=strategy.id,
        idempotency_key="legacy-fund-observation", duration_days=7,
        allocation_pct=1.0, allocated_capital=10_000,
        start_date="2024-06-14", end_date="2024-06-20", status="running",
        auto_trade=True, managed_codes="[]",
    ))
    db.commit()
    calls = []

    def unexpected_tick(*args, **kwargs):
        calls.append(kwargs)
        raise AssertionError("non-A-share observation was scheduled")

    monkeypatch.setattr("server.services.observation.run_observation_tick", unexpected_tick)
    monkeypatch.setattr("server.services.paper_scheduler.SessionLocal", lambda: db)
    result = PaperDailyScheduler(provider=_EmptyProvider(), interval_seconds=10).run_once(date(2024, 6, 14))
    assert result["status"] == "completed"
    assert calls == []


class _EmptyProvider:
    def fetch_quotes(self, codes):
        assert codes == []
        return []
