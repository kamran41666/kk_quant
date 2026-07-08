"""Analytics endpoints — reuse quant_engine.analytics"""
from datetime import date
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from server.models.database import get_db
from server.models.schema import Run

router = APIRouter(prefix="/analytics", tags=["analytics"])


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
