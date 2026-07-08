"""Backtest run endpoints"""
import json
import traceback
from datetime import date, datetime
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Optional

from server.models.database import get_db
from server.models.schema import Run, Strategy

router = APIRouter(prefix="/backtest", tags=["backtest"])


# ---- Request/Response Models ----

class BacktestRunRequest(BaseModel):
    strategy_id: str
    start_date: str  # "YYYY-MM-DD"
    end_date: str
    initial_capital: float = 1_000_000.0
    benchmark: str = "000300.SH"
    rebalance_frequency: str = "weekly"  # daily | weekly | monthly


class RunResponse(BaseModel):
    id: str
    strategy_id: Optional[str] = None
    run_type: str
    status: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    initial_capital: Optional[float] = None
    final_value: Optional[float] = None
    total_return: Optional[float] = None
    sharpe_ratio: Optional[float] = None
    max_drawdown: Optional[float] = None
    error_message: Optional[str] = None
    created_at: str
    completed_at: Optional[str] = None

    class Config:
        from_attributes = True


# ---- Helpers ----

def _import_strategy(class_path: str):
    """Dynamically import a strategy class from a dotted path.

    Example: "quant_engine.strategies.momentum.MomentumStrategy"
    """
    parts = class_path.rsplit(".", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid strategy class path: {class_path}. Expected format: module.ClassName")
    module_name, class_name = parts
    import importlib
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def _execute_backtest(run_id: str, req: BacktestRunRequest):
    """Background task: run the backtest engine"""
    from server.models.database import SessionLocal
    db = SessionLocal()

    try:
        run = db.query(Run).filter(Run.id == run_id).first()
        if not run:
            return
        run.status = "running"
        db.commit()

        # Load strategy
        strategy_def = db.query(Strategy).filter(Strategy.id == req.strategy_id).first()
        if not strategy_def:
            run.status = "failed"
            run.error_message = "Strategy not found"
            db.commit()
            return

        strategy_cls = _import_strategy(strategy_def.strategy_class)
        params = json.loads(strategy_def.params) if strategy_def.params else {}

        # Run backtest
        from quant_engine.backtest.engine import BacktestEngine

        engine = BacktestEngine(strategy_cls, **params)
        result_dir = engine.run(
            start=date.fromisoformat(req.start_date),
            end=date.fromisoformat(req.end_date),
            initial_capital=req.initial_capital,
            benchmark=req.benchmark,
            output_dir=f"backtest_result/{run_id}",
            rebalance_frequency=req.rebalance_frequency,
        )

        # Read summary
        import pandas as pd
        import json as _json
        port_path = Path(result_dir) / "daily_portfolio.parquet"
        summary_path = Path(result_dir) / "summary.json"

        if summary_path.exists():
            with open(summary_path) as f:
                summary = _json.load(f)
            run.final_value = summary.get("final_value")
            run.total_return = summary.get("total_return")

        # Compute metrics if we have daily data
        if port_path.exists():
            df = pd.read_parquet(port_path)
            returns = df.set_index("date")["daily_return"].dropna()
            from quant_engine.analytics.metrics import sharpe_ratio, max_drawdown
            run.sharpe_ratio = sharpe_ratio(returns)
            run.max_drawdown = max_drawdown(returns)[0]

        run.status = "completed"
        run.result_dir = result_dir
        run.completed_at = datetime.now().isoformat()
        db.commit()

    except Exception as e:
        run = db.query(Run).filter(Run.id == run_id).first()
        if run:
            run.status = "failed"
            run.error_message = f"{str(e)}\n{traceback.format_exc()}"
            db.commit()
    finally:
        db.close()


# ---- Endpoints ----

@router.post("/run", response_model=RunResponse)
def run_backtest(
    req: BacktestRunRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Start a backtest run (async). Returns immediately with run_id."""
    # Verify strategy exists
    strategy = db.query(Strategy).filter(Strategy.id == req.strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    now = datetime.now().isoformat()
    run = Run(
        run_type="backtest",
        status="pending",
        strategy_id=req.strategy_id,
        start_date=req.start_date,
        end_date=req.end_date,
        initial_capital=req.initial_capital,
        created_at=now,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    background_tasks.add_task(_execute_backtest, run.id, req)

    return RunResponse(
        id=run.id, run_type=run.run_type, status=run.status,
        strategy_id=run.strategy_id, start_date=run.start_date, end_date=run.end_date,
        initial_capital=run.initial_capital,
        created_at=run.created_at,
        completed_at=run.completed_at,
    )


@router.get("/runs", response_model=list[RunResponse])
def list_runs(
    run_type: Optional[str] = None,
    limit: int = 20,
    db: Session = Depends(get_db),
):
    """List past backtest/paper runs"""
    query = db.query(Run)
    if run_type:
        query = query.filter(Run.run_type == run_type)
    runs = query.order_by(Run.created_at.desc()).limit(limit).all()
    return [RunResponse(
        id=r.id, run_type=r.run_type, status=r.status,
        strategy_id=r.strategy_id, start_date=r.start_date, end_date=r.end_date,
        initial_capital=r.initial_capital, final_value=r.final_value,
        total_return=r.total_return, sharpe_ratio=r.sharpe_ratio,
        max_drawdown=r.max_drawdown, error_message=r.error_message,
        created_at=r.created_at, completed_at=r.completed_at,
    ) for r in runs]


@router.get("/runs/{run_id}", response_model=RunResponse)
def get_run(run_id: str, db: Session = Depends(get_db)):
    """Get a single run's status and summary"""
    r = db.query(Run).filter(Run.id == run_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")
    return RunResponse(
        id=r.id, run_type=r.run_type, status=r.status,
        strategy_id=r.strategy_id, start_date=r.start_date, end_date=r.end_date,
        initial_capital=r.initial_capital, final_value=r.final_value,
        total_return=r.total_return, sharpe_ratio=r.sharpe_ratio,
        max_drawdown=r.max_drawdown, error_message=r.error_message,
        created_at=r.created_at, completed_at=r.completed_at,
    )


@router.get("/runs/{run_id}/equity")
def get_equity(run_id: str, db: Session = Depends(get_db)):
    """Get daily portfolio equity curve"""
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run or not run.result_dir:
        raise HTTPException(status_code=404, detail="Run not found or not completed")

    port_path = Path(run.result_dir) / "daily_portfolio.parquet"
    if not port_path.exists():
        raise HTTPException(status_code=404, detail="Portfolio data not found")

    import pandas as pd
    df = pd.read_parquet(port_path)
    cols = ["date", "total_value", "daily_return", "cumulative_return", "cash", "market_value", "n_positions"]
    available = [c for c in cols if c in df.columns]
    return df[available].to_dict(orient="records")


@router.get("/runs/{run_id}/trades")
def get_trades(
    run_id: str,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
):
    """Get trade list for a run"""
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run or not run.result_dir:
        raise HTTPException(status_code=404, detail="Run not found or not completed")

    trades_path = Path(run.result_dir) / "trades.parquet"
    if not trades_path.exists():
        return {"trades": [], "total": 0}

    import pandas as pd
    df = pd.read_parquet(trades_path)
    total = len(df)
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "trades": df.iloc[start:end].to_dict(orient="records"),
        "total": total,
        "page": page,
        "page_size": page_size,
    }
