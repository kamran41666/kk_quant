"""Backtest run endpoints"""
import json
import traceback
import threading
from datetime import date, datetime
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import Optional

from server.models.database import get_db
from server.models.schema import Run, Strategy
from server.services.strategy_evidence import (
    CALENDAR_VERSION,
    EXECUTION_MODEL,
    FUND_CALENDAR_VERSION,
    FUND_EXECUTION_MODEL,
    build_manifest,
    manifest_is_complete,
    serialize_manifest,
    strategy_fingerprint,
)
from quant_engine.backtest.cost_model import cost_scenario_catalog

router = APIRouter(prefix="/backtest", tags=["backtest"])
_cancelled_runs: set[str] = set()
_cancel_lock = threading.Lock()


# ---- Request/Response Models ----

class BacktestRunRequest(BaseModel):
    strategy_id: str
    market: str = Field(default="a-share", pattern=r"^(a-share|cn-fund|us-equity)$")
    symbols: list[str] = Field(default_factory=list, max_length=20)
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    initial_capital: float = Field(default=1_000_000.0, gt=0, le=1_000_000_000_000)
    # User-supplied paper sensitivity assumption.  It is deliberately not a
    # broker fee quote; the default remains zero when no assumption is given.
    fund_fee_rate: float = Field(default=0.0, ge=0, le=0.1)
    # Versioned paper assumptions for A-share execution. This is never a
    # broker fee quote and is persisted so reports remain reproducible.
    cost_scenario: str = Field(default="paper_baseline_v1", pattern=r"^paper_(baseline|low_impact|high_impact)_v1$")
    benchmark: str = Field(default="000300.SH", min_length=1, max_length=20)
    rebalance_frequency: str = Field(default="weekly", pattern=r"^(daily|weekly|monthly)$")

    @field_validator("start_date", "end_date")
    @classmethod
    def valid_iso_date(cls, value: str) -> str:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date must be a valid YYYY-MM-DD value") from exc
        return value

    @field_validator("initial_capital")
    @classmethod
    def finite_capital(cls, value: float) -> float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("initial_capital must be finite")
        return value

    @field_validator("fund_fee_rate")
    @classmethod
    def finite_fund_fee_rate(cls, value: float) -> float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("fund_fee_rate must be finite")
        return value

    @model_validator(mode="after")
    def ordered_dates(self):
        if date.fromisoformat(self.end_date) < date.fromisoformat(self.start_date):
            raise ValueError("end_date must be on or after start_date")
        return self

    @model_validator(mode="after")
    def validate_market_scope(self):
        if self.market == "cn-fund" and not self.symbols:
            raise ValueError("symbols are required for a domestic-fund backtest")
        if self.market == "us-equity":
            raise ValueError("US equity backtest is not available in this phase")
        if self.market != "cn-fund" and self.fund_fee_rate != 0:
            raise ValueError("fund_fee_rate applies only to domestic-fund backtests")
        if self.market != "a-share" and self.cost_scenario != "paper_baseline_v1":
            raise ValueError("cost_scenario applies only to A-share backtests")
        return self


class RunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
    market: Optional[str] = None
    strategy_fingerprint: Optional[str] = None
    data_manifest: Optional[dict] = None
    data_end: Optional[str] = None
    calendar_version: Optional[str] = None
    execution_model: Optional[str] = None
    eligible_for_observation: bool = False
    created_at: str
    completed_at: Optional[str] = None


# ---- Helpers ----

def _import_strategy(class_path: str):
    """Dynamically import a strategy class from a dotted path.

    Example: "quant_engine.strategies.momentum.MomentumStrategy"
    """
    parts = class_path.rsplit(".", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid strategy class path: {class_path}. Expected format: module.ClassName")
    module_name, class_name = parts
    if not module_name.startswith("strategies."):
        raise ValueError(
            "Strategy classes must be declared in the local strategies package"
        )
    import importlib
    module = importlib.import_module(module_name)
    strategy_class = getattr(module, class_name)
    from quant_engine.backtest.strategy import Strategy as StrategyBase
    if not isinstance(strategy_class, type) or not issubclass(strategy_class, StrategyBase):
        raise TypeError(f"{class_path} is not a Strategy subclass")
    return strategy_class


def _manifest_payload(run: Run) -> Optional[dict]:
    raw = getattr(run, "data_manifest", None)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _run_response(run: Run, *, error_message: Optional[str] = None) -> RunResponse:
    return RunResponse(
        id=run.id,
        strategy_id=run.strategy_id,
        run_type=run.run_type,
        status=run.status,
        start_date=run.start_date,
        end_date=run.end_date,
        initial_capital=run.initial_capital,
        final_value=run.final_value,
        total_return=run.total_return,
        sharpe_ratio=run.sharpe_ratio,
        max_drawdown=run.max_drawdown,
        error_message=error_message if error_message is not None else run.error_message,
        market=getattr(run, "market", None),
        strategy_fingerprint=getattr(run, "strategy_fingerprint", None),
        data_manifest=_manifest_payload(run),
        data_end=getattr(run, "data_end", None),
        calendar_version=getattr(run, "calendar_version", None),
        execution_model=getattr(run, "execution_model", None),
        eligible_for_observation=bool(getattr(run, "eligible_for_observation", False)),
        created_at=run.created_at,
        completed_at=run.completed_at,
    )


def _execute_backtest(run_id: str, req: BacktestRunRequest):
    """Background task: run the backtest engine"""
    from server.models.database import SessionLocal
    from quant_engine.backtest.engine import BacktestCancelled, BacktestEngine
    db = SessionLocal()

    try:
        run = db.query(Run).filter(Run.id == run_id).first()
        if not run:
            return
        if _is_cancelled(run_id):
            run.status = "cancelled"
            run.error_message = "Cancellation requested"
            run.completed_at = datetime.now().isoformat()
            db.commit()
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

        strategy_market = str(getattr(strategy_def, "market", "a-share") or "a-share").strip().lower()
        if strategy_market != req.market:
            run.status = "failed"
            run.error_message = "strategy_market_mismatch"
            db.commit()
            return
        strategy_cls = _import_strategy(strategy_def.strategy_class)
        params = json.loads(strategy_def.params) if strategy_def.params else {}

        if req.market == "cn-fund":
            if isinstance(params, dict) and "paper_fee_rate" in params:
                raise ValueError("strategy parameter 'paper_fee_rate' is reserved for the paper cost model")
            # A fund run is only eligible when its exact NAV response has
            # first been archived locally.  The archive is immutable and its
            # content hash becomes part of the run evidence.
            from quant_engine.data.global_markets import EastmoneyFundDataProvider
            from quant_engine.data.calendar import TradingCalendar
            from quant_engine.backtest.fund_engine import FundNavBacktestEngine
            from server.services.fund_nav_archive import archive_fund_nav_dataset, get_fund_nav_dataset
            provider = EastmoneyFundDataProvider()
            rows_by_code = {}
            dataset_manifests = []
            start_day = date.fromisoformat(req.start_date)
            end_day = date.fromisoformat(req.end_date)
            calendar = TradingCalendar(start_year=start_day.year, end_year=end_day.year)
            calendar_report = calendar.ensure_coverage(start_day, end_day)
            if not calendar_report.get("complete"):
                raise ValueError(
                    "fund_calendar_coverage_insufficient: "
                    + json.dumps(calendar_report, ensure_ascii=False, sort_keys=True)
                )
            for symbol in dict.fromkeys(req.symbols):
                if _is_cancelled(run_id):
                    raise RuntimeError("backtest cancelled")
                rows = provider.fetch_daily(symbol, start_day, end_day)
                dataset = archive_fund_nav_dataset(
                    db, code=symbol, rows=rows, source=provider.source_name,
                    start_date=start_day, end_date=end_day,
                )
                full = get_fund_nav_dataset(db, dataset["dataset_id"], include_rows=True)
                rows_by_code[full["code"]] = full["rows"]
                dataset_manifests.append({key: dataset[key] for key in ("dataset_id", "code", "source", "start_date", "end_date", "row_count", "content_hash", "dataset_version", "nav_rule")})
            manifest = build_manifest(
                market=req.market, strategy_class=strategy_def.strategy_class,
                params=params, start_date=req.start_date, end_date=req.end_date,
                benchmark=None, rebalance_frequency=req.rebalance_frequency,
                universe=sorted(rows_by_code), dataset_manifests=dataset_manifests,
                nav_rule="published_nav_next_valid_day",
                cost_scenario="paper_baseline_v1",
            )
            manifest["fund_fee_rate"] = req.fund_fee_rate
            manifest["calendar_evidence"] = {
                "source": calendar_report["source"],
                "content_hash": calendar_report["content_hash"],
                "coverage_start": calendar_report["coverage_start"],
                "coverage_end": calendar_report["coverage_end"],
                "verified": calendar_report["verified"],
            }
            run.data_manifest = serialize_manifest(manifest)
            run.market = req.market
            run.data_end = req.end_date
            run.calendar_version = manifest["calendar_version"]
            run.execution_model = manifest["execution_model"]
            engine = FundNavBacktestEngine(strategy_cls, paper_fee_rate=req.fund_fee_rate, **params)
            result_dir = engine.run(
                rows=rows_by_code, start=start_day, end=end_day,
                initial_capital=req.initial_capital,
                output_dir=f"backtest_result/{run_id}",
                rebalance_frequency=req.rebalance_frequency,
                cancel_check=lambda: _is_cancelled(run_id),
                # Fund NAV rows are accepted only on the same dated trading
                # calendar used by the research manifest.  Tests and direct
                # library callers may omit this explicit gate when supplying
                # a synthetic dataset.
                calendar=calendar,
            )
        else:
            # Run the existing A-share event-driven engine.
            from quant_engine.data.calendar import TradingCalendar
            start_day = date.fromisoformat(req.start_date)
            end_day = date.fromisoformat(req.end_date)
            calendar = TradingCalendar(start_year=start_day.year, end_year=end_day.year)
            calendar_report = calendar.ensure_coverage(start_day, end_day)
            if not calendar_report.get("complete"):
                raise ValueError(
                    "trading_calendar_coverage_insufficient: "
                    + json.dumps(calendar_report, ensure_ascii=False, sort_keys=True)
                )
            saved_manifest = _manifest_payload(run) or {}
            expected_calendar_hash = (saved_manifest.get("calendar_evidence") or {}).get("content_hash")
            if expected_calendar_hash and expected_calendar_hash != calendar_report.get("content_hash"):
                raise ValueError("trading_calendar_changed_after_submission")
            engine = BacktestEngine(strategy_cls, cost_scenario=req.cost_scenario, **params)
            result_dir = engine.run(
                start=start_day,
                end=end_day,
                initial_capital=req.initial_capital,
                benchmark=req.benchmark,
                output_dir=f"backtest_result/{run_id}",
                rebalance_frequency=req.rebalance_frequency,
                cancel_check=lambda: _is_cancelled(run_id),
                max_seconds=60 * 30,
                calendar=calendar,
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
            # ``data_end`` is an evidence field: use the last date actually
            # present in the engine's input, never the requested bound alone.
            actual_data_end = summary.get("data_available_end") or summary.get("end_date")
            if actual_data_end:
                try:
                    date.fromisoformat(str(actual_data_end))
                except ValueError:
                    actual_data_end = None
            if actual_data_end:
                run.data_end = str(actual_data_end)
                manifest_payload = _manifest_payload(run)
                if manifest_payload is not None:
                    manifest_payload["data_end"] = str(actual_data_end)
                    # The event-driven engine emits a per-security coverage
                    # report only after the same local files it trades have
                    # been read. Persist that evidence alongside the request
                    # manifest so observation eligibility can be audited
                    # without trusting a UI count or requested end date.
                    daily_coverage = summary.get("daily_data_coverage")
                    if isinstance(daily_coverage, dict):
                        manifest_payload["daily_data_coverage"] = daily_coverage
                        if daily_coverage.get("dataset_hash"):
                            manifest_payload["daily_data_content_hash"] = daily_coverage["dataset_hash"]
                    cost_model = summary.get("cost_model")
                    if isinstance(cost_model, dict):
                        manifest_payload["cost_model"] = cost_model
                    run.data_manifest = serialize_manifest(manifest_payload)

        # Compute metrics if we have daily data
        if port_path.exists():
            df = pd.read_parquet(port_path)
            returns = df.set_index("date")["daily_return"].dropna()
            from quant_engine.analytics.metrics import sharpe_ratio, max_drawdown
            run.sharpe_ratio = sharpe_ratio(returns)
            run.max_drawdown = max_drawdown(returns)[0]

        run.status = "completed"
        run.result_dir = result_dir
        run.eligible_for_observation = bool(
            run.market in {"a-share", "cn-fund"}
            and run.strategy_fingerprint
            and manifest_is_complete(run.data_manifest)
        )
        run.completed_at = datetime.now().isoformat()
        db.commit()

    except BacktestCancelled as e:
        run = db.query(Run).filter(Run.id == run_id).first()
        if run:
            run.status = "cancelled"
            run.error_message = str(e)
            run.completed_at = datetime.now().isoformat()
            db.commit()
    except Exception as e:
        run = db.query(Run).filter(Run.id == run_id).first()
        if run:
            run.status = "failed"
            run.error_message = f"{str(e)}\n{traceback.format_exc()}"
            db.commit()
    finally:
        with _cancel_lock:
            _cancelled_runs.discard(run_id)
        db.close()


# ---- Endpoints ----

@router.get("/cost-scenarios")
def list_cost_scenarios():
    """List registered A-share paper cost assumptions for beginner-friendly UI."""
    return {
        "data": cost_scenario_catalog(),
        "meta": {
            "research_only": True,
            "message": "这些是纸面敏感性假设，不是券商费率报价。",
        },
    }

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
    market = req.market
    strategy_market = str(getattr(strategy, "market", "a-share") or "a-share").strip().lower()
    if strategy_market != market:
        raise HTTPException(status_code=409, detail="strategy_market_mismatch")
    if market == "cn-fund" and not req.symbols:
        raise HTTPException(status_code=422, detail="symbols are required for a domestic-fund backtest")
    params = json.loads(strategy.params) if strategy.params else {}
    if market == "cn-fund" and isinstance(params, dict) and "paper_fee_rate" in params:
        raise HTTPException(status_code=422, detail="strategy parameter 'paper_fee_rate' is reserved for the paper cost model")
    fingerprint = strategy_fingerprint(strategy.strategy_class, params)
    manifest = build_manifest(
        market=market,
        strategy_class=strategy.strategy_class,
        params=params,
        start_date=req.start_date,
        end_date=req.end_date,
        benchmark=req.benchmark if market == "a-share" else None,
        universe=sorted({str(item).strip().upper() for item in req.symbols}) if market == "cn-fund" else None,
        nav_rule="published_nav_next_valid_day" if market == "cn-fund" else None,
        rebalance_frequency=req.rebalance_frequency,
        cost_scenario=req.cost_scenario if market == "a-share" else "paper_baseline_v1",
    )
    if market == "a-share":
        # Run the same strict calendar preflight at the HTTP boundary so a
        # pending run cannot be created with unverifiable exchange dates.
        from quant_engine.data.calendar import TradingCalendar
        requested_start = date.fromisoformat(req.start_date)
        requested_end = date.fromisoformat(req.end_date)
        calendar = TradingCalendar(start_year=requested_start.year, end_year=requested_end.year)
        calendar_report = calendar.ensure_coverage(requested_start, requested_end)
        if not calendar_report.get("complete"):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "TRADING_CALENDAR_COVERAGE_INSUFFICIENT",
                    "message": "A-share backtests require a verified exchange calendar covering the requested range.",
                    "calendar": calendar_report,
                },
            )
        manifest["calendar_evidence"] = {
            "source": calendar_report["source"],
            "content_hash": calendar_report["content_hash"],
            "coverage_start": calendar_report["coverage_start"],
            "coverage_end": calendar_report["coverage_end"],
            "verified": calendar_report["verified"],
        }
    elif market == "cn-fund":
        # Match the A-share submission gate: a fund run must not enter
        # ``pending`` when the public mainland trading calendar cannot be
        # verified for the requested interval.  The worker repeats this check
        # immediately before fetching NAVs to protect against cache changes.
        from quant_engine.data.calendar import TradingCalendar
        requested_start = date.fromisoformat(req.start_date)
        requested_end = date.fromisoformat(req.end_date)
        calendar = TradingCalendar(start_year=requested_start.year, end_year=requested_end.year)
        calendar_report = calendar.ensure_coverage(requested_start, requested_end)
        if not calendar_report.get("complete"):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "FUND_CALENDAR_COVERAGE_INSUFFICIENT",
                    "message": "Domestic-fund backtests require a verified mainland trading calendar covering the requested range.",
                    "calendar": calendar_report,
                },
            )
        manifest["calendar_evidence"] = {
            "source": calendar_report["source"],
            "content_hash": calendar_report["content_hash"],
            "coverage_start": calendar_report["coverage_start"],
            "coverage_end": calendar_report["coverage_end"],
            "verified": calendar_report["verified"],
        }
    if market == "cn-fund":
        manifest["fund_fee_rate"] = req.fund_fee_rate
    run = Run(
        run_type="backtest",
        status="pending",
        strategy_id=req.strategy_id,
        start_date=req.start_date,
        end_date=req.end_date,
        initial_capital=req.initial_capital,
        market=market,
        strategy_fingerprint=fingerprint,
        data_manifest=serialize_manifest(manifest),
        data_end=req.end_date,
        # Return the same execution contract that the background worker will
        # persist. Without this, a newly-created fund run briefly advertised
        # the A-share calendar/model while it was still pending.
        calendar_version=FUND_CALENDAR_VERSION if market == "cn-fund" else CALENDAR_VERSION,
        execution_model=FUND_EXECUTION_MODEL if market == "cn-fund" else EXECUTION_MODEL,
        eligible_for_observation=False,
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
        market=run.market,
        strategy_fingerprint=run.strategy_fingerprint,
        data_manifest=manifest,
        data_end=run.data_end,
        calendar_version=run.calendar_version,
        execution_model=run.execution_model,
        eligible_for_observation=run.eligible_for_observation,
        created_at=run.created_at,
        completed_at=run.completed_at,
    )


def _is_cancelled(run_id: str) -> bool:
    with _cancel_lock:
        return run_id in _cancelled_runs


@router.post("/runs/{run_id}/cancel", response_model=RunResponse)
def cancel_run(run_id: str, db: Session = Depends(get_db)):
    """Request cancellation for a pending/running run."""
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status not in {"pending", "running"}:
        raise HTTPException(status_code=409, detail=f"Run is already {run.status}")
    with _cancel_lock:
        _cancelled_runs.add(run_id)
    run.status = "cancelled" if run.status == "pending" else run.status
    db.commit()
    return _run_response(run, error_message="Cancellation requested")


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
    return [_run_response(r) for r in runs]


@router.get("/runs/{run_id}", response_model=RunResponse)
def get_run(run_id: str, db: Session = Depends(get_db)):
    """Get a single run's status and summary"""
    r = db.query(Run).filter(Run.id == run_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")
    return _run_response(r)


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
