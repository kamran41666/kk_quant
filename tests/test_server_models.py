"""Test server ORM models — uses in-memory SQLite"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from server.models.database import Base
from server.models.schema import Strategy, Run, PaperSnapshot


@pytest.fixture(scope="function")
def db_session():
    """Create a fresh in-memory SQLite database per test"""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


class TestStrategyModel:
    def test_create_strategy(self, db_session):
        s = Strategy(name="Test Strategy", strategy_class="test.MyStrategy", params='{"top_n":50}')
        db_session.add(s)
        db_session.commit()
        db_session.refresh(s)
        assert s.id is not None
        assert s.name == "Test Strategy"

    def test_list_strategies(self, db_session):
        # Explicitly insert test data — no dependency on other tests
        s = Strategy(name="Test", strategy_class="test.X", params="{}")
        db_session.add(s)
        db_session.commit()

        strategies = db_session.query(Strategy).all()
        assert len(strategies) >= 1

    def test_strategy_json_params(self, db_session):
        s = Strategy(name="JSON Test", strategy_class="test.JSON", params='{"top": 50, "lookback": 60}')
        db_session.add(s)
        db_session.commit()
        db_session.refresh(s)
        import json
        p = json.loads(s.params)
        assert p["top"] == 50
        assert p["lookback"] == 60


class TestRunModel:
    def test_create_run(self, db_session):
        r = Run(run_type="backtest", status="pending", initial_capital=1_000_000.0)
        db_session.add(r)
        db_session.commit()
        db_session.refresh(r)
        assert r.id is not None
        assert r.status == "pending"

    def test_run_lifecycle(self, db_session):
        r = Run(run_type="backtest", status="pending")
        db_session.add(r)
        db_session.commit()

        r.status = "running"
        db_session.commit()
        db_session.refresh(r)
        assert r.status == "running"

        r.status = "completed"
        r.total_return = 0.15
        db_session.commit()
        db_session.refresh(r)
        assert r.status == "completed"
        assert r.total_return == 0.15


class TestPaperSnapshot:
    def test_create_snapshot(self, db_session):
        ps = PaperSnapshot(
            date="2024-06-14",
            cash=500000.0,
            market_value=500000.0,
            total_value=1_000_000.0,
            daily_return=0.01,
            n_positions=10,
        )
        db_session.add(ps)
        db_session.commit()
        db_session.refresh(ps)
        assert ps.total_value == 1_000_000.0
        assert ps.n_positions == 10

    def test_multiple_snapshots(self, db_session):
        for i in range(3):
            ps = PaperSnapshot(
                date=f"2024-06-{14+i}",
                cash=1000000.0 - i * 10000,
                market_value=500000.0 + i * 5000,
                total_value=1500000.0 - i * 5000,
                daily_return=0.01 - i * 0.001,
                n_positions=10 + i,
            )
            db_session.add(ps)
        db_session.commit()

        all_snapshots = db_session.query(PaperSnapshot).all()
        assert len(all_snapshots) == 3
