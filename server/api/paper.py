"""Paper trading account endpoints"""
from datetime import date, datetime
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, field_validator
from typing import Optional

from server.models.database import get_db
from server.models.schema import PaperSnapshot, PaperPosition as PaperPositionModel
from server.services.paper_engine import SimulationAccount
from server.services.paper_trading import account_report, account_snapshot, create_account, list_accounts, list_ledger, list_orders, list_valuations, mark_to_market, submit_order
from server.ws.manager import manager

router = APIRouter(prefix="/paper", tags=["paper"])


class CreatePaperAccountRequest(BaseModel):
    name: str = Field(default="默认模拟账户", min_length=1, max_length=120)
    initial_capital: float = Field(default=1_000_000.0, gt=0, le=1_000_000_000_000)
    max_order_notional: float = Field(default=100_000.0, gt=0)
    max_position_weight: float = Field(default=0.25, gt=0, le=1)
    max_daily_loss: float = Field(default=0.03, ge=0, lt=1)


class PaperOrderRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=160)
    code: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    side: str = Field(pattern=r"^(buy|sell)$")
    quantity: int = Field(gt=0, le=10_000_000)
    price: float = Field(gt=0, le=10_000_000)
    price_source: str = Field(default="manual_input", min_length=1, max_length=80)
    price_as_of: Optional[datetime] = None
    price_freshness: str = Field(default="manual", pattern=r"^(manual|realtime|fresh|delayed|stale|unknown)$")

    @field_validator("quantity")
    @classmethod
    def round_lot(cls, value: int) -> int:
        if value % 100 != 0:
            raise ValueError("A-share paper orders must use 100-share lots")
        return value


@router.post("/accounts")
def create_paper_account(req: CreatePaperAccountRequest, db: Session = Depends(get_db)):
    try:
        return create_account(db, **req.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/accounts")
def get_paper_accounts(db: Session = Depends(get_db)):
    return list_accounts(db)


@router.get("/accounts/{account_id}")
def get_paper_account(account_id: str, db: Session = Depends(get_db)):
    try:
        return account_snapshot(db, account_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/accounts/{account_id}/orders")
async def submit_paper_order(account_id: str, req: PaperOrderRequest, db: Session = Depends(get_db)):
    try:
        payload = req.model_dump()
        payload["price_as_of"] = req.price_as_of.isoformat() if req.price_as_of else None
        result = submit_order(db, account_id=account_id, **payload)
        if result["status"] == "filled":
            message = {"type": "order_fill", "account_id": account_id, "data": result}
            try:
                await manager.broadcast(f"paper:{account_id}", message)
                await manager.broadcast("dashboard", message)
            except Exception:
                # The order is already committed. A broken client socket must
                # never turn a successful fill into a retryable HTTP failure.
                pass
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/accounts/{account_id}/orders")
def get_paper_orders(account_id: str, limit: int = 50, db: Session = Depends(get_db)):
    try:
        return list_orders(db, account_id, max(1, min(limit, 200)))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/accounts/{account_id}/ledger")
def get_paper_ledger(account_id: str, limit: int = 100, db: Session = Depends(get_db)):
    try:
        return list_ledger(db, account_id, max(1, min(limit, 500)))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class PaperValuationRequest(BaseModel):
    valuation_date: date
    prices: dict[str, float] = Field(min_length=0)
    price_source: str = Field(default="manual_input", min_length=1, max_length=80)
    price_as_of: Optional[datetime] = None
    price_freshness: str = Field(default="manual", pattern=r"^(manual|realtime|fresh|delayed|stale|unknown)$")

    @field_validator("prices")
    @classmethod
    def positive_prices(cls, value: dict[str, float]) -> dict[str, float]:
        for code, price in value.items():
            if not __import__("re").fullmatch(r"\d{6}\.(SH|SZ|BJ)", code) or price <= 0:
                raise ValueError("prices must contain canonical A-share codes and positive values")
        return value


@router.post("/accounts/{account_id}/valuations")
async def create_paper_valuation(account_id: str, req: PaperValuationRequest, db: Session = Depends(get_db)):
    try:
        result = mark_to_market(db, account_id=account_id, prices=req.prices,
                                valuation_date=req.valuation_date,
                                price_source=req.price_source,
                                price_as_of=req.price_as_of.isoformat() if req.price_as_of else None,
                                price_freshness=req.price_freshness)
        message = {"type": "portfolio_update", "account_id": account_id, "data": result}
        try:
            await manager.broadcast(f"paper:{account_id}", message)
            await manager.broadcast("dashboard", message)
        except Exception:
            pass
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/accounts/{account_id}/valuations")
def get_paper_valuations(account_id: str, limit: int = 60, db: Session = Depends(get_db)):
    try:
        return list_valuations(db, account_id, max(1, min(limit, 365)))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/accounts/{account_id}/report")
def get_paper_report(account_id: str, db: Session = Depends(get_db)):
    try:
        return account_report(db, account_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

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
    strategy_class: str = Field(min_length=1, max_length=255)
    strategy_params: dict = Field(default_factory=dict)
    initial_capital: float = Field(default=1_000_000.0, gt=0, le=1_000_000_000_000)


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
