"""Analytics endpoints — reuse quant_engine.analytics"""
import json
import hashlib
from datetime import date
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from server.models.database import get_db
from server.models.schema import Run

router = APIRouter(prefix="/analytics", tags=["analytics"])


def _run_evidence(run: Run) -> dict:
    try:
        manifest = json.loads(run.data_manifest or "{}")
    except (TypeError, ValueError):
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}
    coverage = manifest.get("daily_data_coverage")
    if not isinstance(coverage, dict):
        coverage = None
    dataset_manifests = manifest.get("dataset_manifests")
    fund_dataset_hashes = []
    fund_coverage_complete = None
    if isinstance(dataset_manifests, list):
        for item in dataset_manifests:
            if isinstance(item, dict) and isinstance(item.get("content_hash"), str) and item.get("content_hash"):
                fund_dataset_hashes.append(item["content_hash"])
        if dataset_manifests:
            def has_rows(item: object) -> bool:
                if not isinstance(item, dict):
                    return False
                try:
                    return float(item.get("row_count", 0) or 0) > 0
                except (TypeError, ValueError):
                    return False

            fund_coverage_complete = all(
                isinstance(item, dict)
                and isinstance(item.get("content_hash"), str)
                and bool(item.get("content_hash"))
                and has_rows(item)
                for item in dataset_manifests
            )
    data_content_hash = manifest.get("daily_data_content_hash") or (coverage or {}).get("dataset_hash")
    if not data_content_hash and fund_dataset_hashes:
        # A stable aggregate lets the compare table identify a multi-fund
        # archive without pretending it is one provider-issued hash.
        data_content_hash = (
            fund_dataset_hashes[0]
            if len(fund_dataset_hashes) == 1
            else hashlib.sha256(
                json.dumps(sorted(fund_dataset_hashes), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
        )
    market = getattr(run, "market", None)
    return {
        "market": market,
        "strategy_fingerprint": getattr(run, "strategy_fingerprint", None),
        "data_end": getattr(run, "data_end", None),
        "calendar_version": getattr(run, "calendar_version", None),
        "execution_model": getattr(run, "execution_model", None),
        "data_content_hash": data_content_hash,
        "dataset_hashes": fund_dataset_hashes,
        "coverage_complete": (coverage or {}).get("complete") if coverage else fund_coverage_complete,
        # A-share runs use the registered CostModel scenarios. Domestic fund
        # runs use their separate NAV fee sensitivity; never label one as the
        # other in a cross-run comparison.
        "cost_scenario": manifest.get("cost_scenario") if market == "a-share" else None,
        "cost_model": manifest.get("cost_model") if market == "a-share" else None,
        "fund_fee_rate": manifest.get("fund_fee_rate") if market == "cn-fund" else None,
    }


@router.get("/metrics/{run_id}")
def get_metrics(run_id: str, db: Session = Depends(get_db)):
    """Get all performance metrics for a completed run"""
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != "completed" or not run.result_dir:
        raise HTTPException(status_code=400, detail="Run not yet completed")

    port_path = Path(run.result_dir) / "daily_portfolio.parquet"
    if not port_path.exists():
        raise HTTPException(status_code=404, detail="Portfolio data not found")

    import pandas as pd
    df = pd.read_parquet(port_path)
    returns = df.set_index("date")["daily_return"].dropna()

    from quant_engine.analytics.metrics import (
        annual_return, annual_volatility, downside_volatility,
        max_drawdown, sharpe_ratio, sortino_ratio, calmar_ratio,
        win_rate, profit_loss_ratio,
    )

    mdd_result = max_drawdown(returns)

    # Win rate & P/L ratio from trades if available
    trades_path = Path(run.result_dir) / "trades.parquet"
    win_r = None
    pl_ratio = None
    if trades_path.exists():
        trades_df = pd.read_parquet(trades_path)
        win_r = win_rate(trades_df)
        pl_ratio = profit_loss_ratio(trades_df)

    return {
        "annual_return": annual_return(returns),
        "annual_volatility": annual_volatility(returns),
        "downside_volatility": downside_volatility(returns),
        "sharpe_ratio": sharpe_ratio(returns),
        "sortino_ratio": sortino_ratio(returns),
        "calmar_ratio": calmar_ratio(returns),
        "max_drawdown": mdd_result[0],
        "max_drawdown_duration_days": mdd_result[3],
        "win_rate": win_r,
        "profit_loss_ratio": pl_ratio,
        "n_trading_days": len(returns),
    }


@router.get("/attribution/{run_id}")
def get_attribution(run_id: str, benchmark: str = "000300.SH", db: Session = Depends(get_db)):
    """Get alpha/beta attribution"""
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != "completed" or not run.result_dir:
        raise HTTPException(status_code=400, detail="Run not yet completed")

    port_path = Path(run.result_dir) / "daily_portfolio.parquet"
    if not port_path.exists():
        raise HTTPException(status_code=404, detail="Portfolio data not found")

    import pandas as pd
    df = pd.read_parquet(port_path)
    strategy_returns = df.set_index("date")["daily_return"].dropna()

    # Get benchmark returns from DataAPI (simplified: skip if no data)
    try:
        from quant_engine.data.api import DataAPI
        api = DataAPI()
        bench_df = api.daily(
            codes=[benchmark],
            start=date.fromisoformat(str(strategy_returns.index[0])[:10]),
            end=date.fromisoformat(str(strategy_returns.index[-1])[:10]),
            fields=["close"],
            adjust="event_driven",
        )
        if hasattr(bench_df.index, 'get_level_values'):
            bench_close = bench_df.xs(benchmark, level='code')['close']
            bench_returns = bench_close.pct_change().dropna()

            # Align indices
            common = strategy_returns.index.intersection(bench_returns.index)
            sr = strategy_returns[common]
            br = bench_returns[common]

            from quant_engine.analytics.alpha_beta import capm_alpha_beta, information_ratio
            capm = capm_alpha_beta(sr, br)
            ir = information_ratio(sr, br)

            return {
                "alpha": capm["alpha"],
                "annual_alpha": capm["annual_alpha"],
                "beta": capm["beta"],
                "r_squared": capm["r_squared"],
                "t_stat_alpha": capm.get("t_stat_alpha"),
                "information_ratio": ir,
            }
    except Exception:
        pass

    return {"error": "Could not compute attribution — benchmark data unavailable"}


@router.get("/compare")
def compare_runs(
    run_ids: str = Query(..., description="Comma-separated completed backtest ids (2-8)"),
    db: Session = Depends(get_db),
):
    """Compare completed research runs without treating one metric as proof."""
    requested = list(dict.fromkeys(item.strip() for item in str(run_ids).split(",") if item.strip()))
    if len(requested) < 2 or len(requested) > 8:
        raise HTTPException(status_code=422, detail="compare requires 2-8 unique run ids")
    rows = db.query(Run).filter(Run.id.in_(requested)).all()
    by_id = {row.id: row for row in rows}
    missing = [run_id for run_id in requested if run_id not in by_id]
    if missing:
        raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND", "ids": missing})
    result = []
    for run_id in requested:
        run = by_id[run_id]
        if run.status != "completed" or not run.result_dir:
            raise HTTPException(status_code=400, detail={"code": "RUN_NOT_COMPLETED", "id": run_id})
        metrics = get_metrics(run_id, db)
        result.append({
            "run_id": run.id,
            "strategy_id": run.strategy_id,
            "created_at": run.created_at,
            "total_return": run.total_return,
            "metrics": metrics,
            "evidence": _run_evidence(run),
        })
    return {
        "data": result,
        "meta": {
            "research_only": True,
            "comparison_count": len(result),
            "same_market": len({item["evidence"].get("market") for item in result}) == 1,
        },
    }
