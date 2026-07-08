"""Paper trading account endpoints"""
from datetime import date
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from server.models.database import get_db
from server.models.schema import PaperSnapshot, PaperPosition as PaperPositionModel
from server.services.paper_engine import SimulationAccount

router = APIRouter(prefix="/paper", tags=["paper"])

# Global paper account instance (single-strategy for now)
_paper_account: Optional[SimulationAccount] = None
_paper_strategy_class: Optional[str] = None
_paper_strategy_params: dict = {}


def get_paper_account() -> Optional[SimulationAccount]:
    return _paper_account


@router.get("/status")
def get_paper_status(db: Session = Depends(get_db)):
    """Get current paper account state"""
    last = (
        db.query(PaperSnapshot)
        .order_by(PaperSnapshot.date.desc())
        .first()
    )
    if not last:
        return {
            "cash": 0,
            "market_value": 0,
            "total_value": 0,
            "daily_return": 0,
            "n_positions": 0,
            "positions": [],
            "message": "No paper trading data yet. Initialize with POST /paper/init"
        }

    positions = (
        db.query(PaperPositionModel)
        .filter(PaperPositionModel.snapshot_date == last.date)
        .all()
    )

    return {
        "date": last.date,
        "cash": last.cash,
        "market_value": last.market_value,
        "total_value": last.total_value,
        "daily_return": last.daily_return,
        "n_positions": last.n_positions,
        "positions": [
            {
                "code": p.code,
                "shares": p.shares,
                "avg_cost": p.avg_cost,
                "market_value": p.market_value,
                "weight": p.weight,
            }
            for p in positions
        ],
    }


@router.get("/history")
def get_paper_history(limit: int = 60, db: Session = Depends(get_db)):
    """Get historical account snapshots"""
    snapshots = (
        db.query(PaperSnapshot)
        .order_by(PaperSnapshot.date.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "date": s.date,
            "cash": s.cash,
            "market_value": s.market_value,
            "total_value": s.total_value,
            "daily_return": s.daily_return,
            "n_positions": s.n_positions,
        }
        for s in reversed(snapshots)
    ]


class InitPaperRequest(BaseModel):
    strategy_class: str
    strategy_params: dict = {}
    initial_capital: float = 1_000_000.0


@router.post("/init")
def init_paper(req: InitPaperRequest):
    """Initialize (or re-initialize) the paper trading account with a strategy"""
    global _paper_account, _paper_strategy_class, _paper_strategy_params

    _paper_strategy_class = req.strategy_class
    _paper_strategy_params = req.strategy_params

    # Import strategy
    from server.api.backtest import _import_strategy
    strategy_cls = _import_strategy(req.strategy_class)

    _paper_account = SimulationAccount(
        strategy_class=strategy_cls,
        strategy_params=req.strategy_params,
        initial_capital=req.initial_capital,
    )

    return {"status": "initialized", "strategy": req.strategy_class, "capital": req.initial_capital}


@router.post("/trigger")
def trigger_paper_run():
    """Manually trigger today's paper trading run"""
    if _paper_account is None:
        raise HTTPException(status_code=400, detail="Paper account not initialized. POST /paper/init first")

    result = _paper_account.run_daily(date.today())
    return result


@router.post("/reset")
def reset_paper():
    """Reset simulation account"""
    if _paper_account is None:
        raise HTTPException(status_code=400, detail="Paper account not initialized")

    _paper_account.reset()
    return {"status": "reset"}
