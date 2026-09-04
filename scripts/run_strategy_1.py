#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Strategy #1 backtest: Small-Cap Value Alpha

Usage:
    cd D:\workspace\quant
    python scripts/run_strategy_1.py
"""
import sys
import argparse
import os
from datetime import date
from pathlib import Path

# Keep progress visible on Windows while the backtest is running.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np

from strategies.small_cap_value import SmallCapValueStrategy
from quant_engine.backtest.engine import BacktestEngine
from quant_engine.backtest.strategy import Strategy
from quant_engine.data.calendar import TradingCalendar
from quant_engine.data.store import PriceStore


def main():
    parser = argparse.ArgumentParser(description="Run the Strategy #1 backtest demo")
    parser.add_argument(
        "--generate-demo",
        action="store_true",
        help="create deterministic synthetic files only when they are missing",
    )
    args = parser.parse_args()
    if args.generate_demo:
        # Synthetic data lives outside the production data root and is selected
        # explicitly through the same runtime configuration used by DataAPI.
        os.environ["QUANT_DATA_DIR"] = str(Path("data/demo").resolve())
    print("=" * 60)
    print("Strategy #1: Small-Cap Value Alpha — SYNTHETIC DEMO")
    print("=" * 60)

    # 股票池: 全A股 or 中证500成分股
    stock_list = [
        '000001.SZ', '000002.SZ', '000858.SZ', '002415.SZ',
        '600000.SH', '600036.SH', '600519.SH', '601318.SH',
        '600276.SH', '000333.SZ', '300750.SZ', '000651.SZ',
        '002714.SZ', '601166.SH', '600900.SH', '000568.SZ',
        '002304.SZ', '600809.SH', '000725.SZ', '002475.SZ',
        '601398.SH', '601288.SH', '601857.SH', '600030.SH',
        '601668.SH', '000063.SZ', '002594.SZ', '600887.SH',
        '601012.SH', '603259.SH',
    ]

    print(f"\nStock pool: {len(stock_list)} stocks")
    print("Data: deterministic synthetic fixture (not investable market evidence)")
    print("Period: 2023-01-01 ~ 2024-12-31")
    print("Capital: 1,000,000")
    print("Rebalance: weekly (Friday signal, Monday exec)")
    print("\nFactors:")
    print("  - volatility_1m  (low-vol anomaly)")
    print("  - momentum_1m    (short-term reversal)")
    print("  - log_market_cap (size premium)")
    print("\nRisk controls:")
    print("  - Empty position in Jan/Apr/Dec (A-share seasonal effect)")

    # ---- Phase 0: Generate synthetic market data if not present ----
    # Step 1: Write calendar cache first (so TradingCalendar picks it up)
    cal_dates = pd.date_range("2022-06-01", "2024-12-31", freq="B")
    cal_df = pd.DataFrame({
        "date": cal_dates.date,
        "is_trading_day": [True] * len(cal_dates),
        "exchange": ["SSE"] * len(cal_dates),
    })
    data_root = Path(os.getenv("QUANT_DATA_DIR", "data"))
    calendar_path = data_root / "raw" / "calendar" / "trading_dates.parquet"
    if not calendar_path.exists():
        if not args.generate_demo:
            raise SystemExit(
                f"Missing calendar {calendar_path}; pass --generate-demo to create synthetic demo data."
            )
        calendar_path.parent.mkdir(parents=True, exist_ok=True)
        cal_df.to_parquet(calendar_path, index=False)

    # Step 2: Get trading days
    cal = TradingCalendar()
    trading_days = cal.get_trading_days(date(2022, 6, 1), date(2024, 12, 31))
    print(f"\nTrading days in range: {len(trading_days)}")

    # Step 3: Generate synthetic daily data
    store = PriceStore()
    np.random.seed(42)

    fixture_probe = Path("data/raw/daily/year=2024/quarter=4/000001.SZ.parquet")
    missing_codes = [
        code for code in stock_list
        if not (data_root / "raw" / "daily" / "year=2024" / "quarter=4" / f"{code}.parquet").exists()
    ]
    if missing_codes and not args.generate_demo:
        raise SystemExit(
            f"Missing {len(missing_codes)} local price files; pass --generate-demo to create synthetic demo data."
        )
    codes_to_generate = missing_codes
    if codes_to_generate:
        print("Generating deterministic synthetic fixture for 30 stocks ...")
    else:
        print("Using existing local demo fixture; source files are left unchanged.")
    for code in codes_to_generate:
        n = len(trading_days)
        # Random walk for close
        close = 10.0 + np.cumsum(np.random.randn(n) * 0.15)
        close = np.maximum(close, 1.0)

        high = close + np.abs(np.random.randn(n) * 0.1)
        low  = close - np.abs(np.random.randn(n) * 0.1)
        open_price = close * (1.0 + np.random.randn(n) * 0.001)

        df = pd.DataFrame({
            "date": trading_days,
            "open": open_price,
            "high": np.maximum.reduce([open_price, close, high]),
            "low":  np.minimum.reduce([open_price, close, low]),
            "close": close,
            "volume": np.random.randint(100000, 5000000, n).astype(float),
            "amount": np.random.randint(1000000, 50000000, n).astype(float),
            "turnover_rate": np.random.rand(n) * 5.0,
            "pre_close": np.concatenate([[close[0]], close[:-1]]),
            "up_limit": close * 1.1,
            "down_limit": close * 0.9,
            "is_suspended": [False] * n,
        })
        store.write(code, df)

    if codes_to_generate:
        print("Synthetic fixture generated.\n")

    engine = BacktestEngine(SmallCapValueStrategy, stock_list=stock_list)

    result_dir = engine.run(
        start=date(2023, 1, 1),
        end=date(2024, 12, 31),
        initial_capital=1_000_000.0,
        benchmark='000300.SH',
        output_dir='backtest_result/strategy_1',
        rebalance_frequency='weekly',
        rebalance_weekday=5,
    )

    # 读取结果
    import json

    print(f"\nResult dir: {result_dir}")

    with open(Path(result_dir) / 'summary.json') as f:
        summary = json.load(f)

    print(f"Start: {summary['start_date']}")
    print(f"End: {summary['end_date']}")
    print(f"Final equity: {summary.get('final_value', 0):,.0f}")
    print(f"Total return: {summary.get('total_return', 0) * 100:.2f}%")

    # Compute metrics
    df = pd.read_parquet(Path(result_dir) / 'daily_portfolio.parquet')
    returns = df.set_index('date')['daily_return'].dropna()

    from quant_engine.analytics.metrics import (
        annual_return, annual_volatility, sharpe_ratio,
        max_drawdown, calmar_ratio, sortino_ratio,
    )

    ar = annual_return(returns) * 100
    vol = annual_volatility(returns) * 100
    sr = sharpe_ratio(returns)
    sor = sortino_ratio(returns)
    mdd, peak, trough, recovery = max_drawdown(returns)
    cr = calmar_ratio(returns)

    print("")
    print("  Metric          Value")
    print("  ──────────────  ────────────")
    print(f"  Annual Return   {ar:7.2f}%")
    print(f"  Annual Vol      {vol:7.2f}%")
    print(f"  Sharpe Ratio    {sr:10.2f}")
    print(f"  Sortino Ratio   {sor:10.2f}")
    print(f"  Max Drawdown    {mdd*100:7.2f}%")
    print(f"  Recovery Days   {recovery:7.0f}")
    print(f"  Calmar Ratio    {cr:10.2f}")

    print(f"\n{'='*60}")
    print("Backtest complete")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
