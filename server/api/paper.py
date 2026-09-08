"""Paper trading account endpoints"""
from datetime import date, datetime
import math
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, field_validator
from typing import Optional

from server.models.database import get_db
from server.models.schema import PaperSnapshot, PaperPosition as PaperPositionModel
from quant_engine.data.live import SHANGHAI_TZ
from server.services.paper_engine import SimulationAccount
from server.services.paper_trading import (account_report, account_snapshot, build_daily_report, create_account,
    list_accounts, list_daily_reports, list_deviations, list_ledger, list_orders, list_valuations,
    mark_to_market, record_deviation, reconcile_account, submit_order)
from server.ws.manager import manager
from server.services.paper_scheduler import scheduler_runs

router = APIRouter(prefix="/paper", tags=["paper"])


@router.get("/scheduler/runs")
def get_scheduler_runs(limit: int = 30):
    return scheduler_runs(max(1, min(limit, 100)))


@router.post("/scheduler/run")
def run_scheduler_now(request: Request, run_date: Optional[date] = None):
    scheduler = getattr(request.app.state, "paper_scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="paper scheduler is disabled")
    try:
        return scheduler.run_once(run_date)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class CreatePaperAccountRequest(BaseModel):
    name: str = Field(default="默认模拟账户", min_length=1, max_length=120)
    market: str = Field(default="a-share", pattern=r"^(a-share|cn-fund|us-equity)$")
    initial_capital: float = Field(default=1_000_000.0, gt=0, le=1_000_000_000_000)
    max_order_notional: float = Field(default=100_000.0, gt=0)
    max_position_weight: float = Field(default=0.25, gt=0, le=1)
    max_daily_loss: float = Field(default=0.03, ge=0, lt=1)
    validation_only: bool = False


class PaperOrderRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=160)
    # Code format is market-specific and is therefore validated by the
    # service after loading the account's authoritative market. Keeping this
    # field open here allows both six-digit fund codes and US tickers.
    code: str = Field(min_length=1, max_length=20)
    market: Optional[str] = Field(default=None, pattern=r"^(a-share|cn-fund|us-equity)$")
    side: str = Field(pattern=r"^(buy|sell)$")
    quantity: float = Field(gt=0, le=10_000_000)
    price: float = Field(gt=0, le=10_000_000)
    price_source: str = Field(default="manual_input", min_length=1, max_length=80)
    price_as_of: Optional[datetime] = None
    price_freshness: str = Field(default="manual", pattern=r"^(manual|realtime|fresh|delayed|stale|unknown)$")
    trade_date: Optional[date] = None

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
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/accounts/{account_id}/orders")
async def submit_paper_order(account_id: str, req: PaperOrderRequest, db: Session = Depends(get_db)):
    try:
        payload = req.model_dump()
        payload["price_as_of"] = req.price_as_of.isoformat() if req.price_as_of else None
        # HTTP orders always carry an explicit session date. This makes a
        # weekend/holiday request fail closed while direct service callers can
        # retain the legacy default used by offline tests and replays.
        payload["trade_date"] = req.trade_date or datetime.now(SHANGHAI_TZ).date()
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
    price_freshness: str = Field(default="manual", pattern=r"^(manual|realtime|fresh|delayed|stale|unknown|mixed)$")
    price_metadata: Optional[dict[str, dict[str, Optional[str]]]] = None

    @field_validator("prices")
    @classmethod
    def positive_prices(cls, value: dict[str, float]) -> dict[str, float]:
        for code, price in value.items():
            if not isinstance(code, str) or not code.strip() or not isinstance(price, (int, float)) or not math.isfinite(float(price)) or price <= 0:
                raise ValueError("prices must contain non-empty symbols and positive finite values")
        return value


@router.post("/accounts/{account_id}/valuations")
async def create_paper_valuation(account_id: str, req: PaperValuationRequest, db: Session = Depends(get_db)):
    try:
        result = mark_to_market(db, account_id=account_id, prices=req.prices,
                                valuation_date=req.valuation_date,
                                price_source=req.price_source,
                                price_as_of=req.price_as_of.isoformat() if req.price_as_of else None,
                                price_freshness=req.price_freshness,
                                price_metadata=req.price_metadata)
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
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/accounts/{account_id}/reconcile")
def get_paper_reconciliation(account_id: str, db: Session = Depends(get_db)):
    try:
        return reconcile_account(db, account_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class PaperDeviationRequest(BaseModel):
    valuation_date: date
    expected_return: float = Field(ge=-0.999, lt=10)
    source: str = Field(default="manual_plan", min_length=1, max_length=80)


@router.post("/accounts/{account_id}/deviations")
def create_paper_deviation(account_id: str, req: PaperDeviationRequest, db: Session = Depends(get_db)):
    try:
        return record_deviation(db, account_id=account_id, valuation_date=req.valuation_date, expected_return=req.expected_return, source=req.source)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/accounts/{account_id}/deviations")
def get_paper_deviations(account_id: str, limit: int = 60, db: Session = Depends(get_db)):
    try:
        return list_deviations(db, account_id, max(1, min(limit, 365)))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class PaperDailyReportRequest(BaseModel):
    report_date: date


@router.post("/accounts/{account_id}/reports/daily")
def create_paper_daily_report(account_id: str, req: PaperDailyReportRequest, db: Session = Depends(get_db)):
    try:
        return build_daily_report(db, account_id=account_id, report_date=req.report_date)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/accounts/{account_id}/reports/daily")
def get_paper_daily_reports(account_id: str, limit: int = 60, db: Session = Depends(get_db)):
    try:
        return list_daily_reports(db, account_id, max(1, min(limit, 365)))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

# Global paper account instance (single-strategy for now)
_paper_account: Optional[SimulationAccount] = None
_paper_strategy_class: Optional[str] = None
_paper_strategy_params: dict = {}


def get_legacy_paper_account() -> Optional[SimulationAccount]:
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

    result = _paper_account.run_daily(datetime.now(SHANGHAI_TZ).date())
    return result


@router.post("/reset")
def reset_paper():
    """Reset simulation account"""
    if _paper_account is None:
        raise HTTPException(status_code=400, detail="Paper account not initialized")

    _paper_account.reset()
    return {"status": "reset"}
