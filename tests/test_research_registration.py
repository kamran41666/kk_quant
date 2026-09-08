import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from scripts import run_ashare_research as runner
from server.models import database
from server.models.schema import Run, Strategy


def test_multiple_experiments_register_one_strategy_without_autoflush(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'research.db'}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(database, "init_db", lambda: None)
    monkeypatch.setattr(runner, "compare_payload", lambda directory, benchmarks: {"labels": [], "series": [], "validated": False})
    runs = []
    for index in range(2):
        directory = tmp_path / f"run-{index}"
        directory.mkdir()
        (directory / "summary.json").write_text(json.dumps({"initial_capital": 1_000_000,
            "execution_model": "research_raw_open_corporate_actions_v1", "validated": False}))
        runs.append({"run_id": f"run-{index}", "status": "completed", "implementation": runner.STRATEGIES["low_volatility"],
                     "result_dir": str(directory), "label": f"experiment {index}", "period": "discovery",
                     "cost_scenario": "baseline", "parameters": {}, "start_date": "2015-01-05", "end_date": "2018-12-28",
                     "metrics": {"final_value": 1_000_000, "total_return": 0., "sharpe": 0., "max_drawdown": 0., "end_date": "2018-12-28"}})
    matrix = {"runs": runs, "benchmark_files": {}}
    runner.register_runs(matrix)
    runner.register_runs(matrix)
    with factory() as db:
        assert db.query(Strategy).count() == 1
        assert db.query(Run).count() == 2
        assert all(not row.eligible_for_observation for row in db.query(Run).all())
    engine.dispose()
