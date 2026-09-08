"""Run the frozen COPA_PRICE_ONLY v1 train/validation/sealed experiment matrix."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date
from multiprocessing import get_context
from pathlib import Path
from typing import Any

import pandas as pd

from quant_engine.analytics.metrics import max_drawdown, sharpe_ratio
from quant_engine.backtest.engine import BacktestEngine
from quant_engine.backtest.strategy import Strategy
from quant_engine.data.api import DataAPI
from server.services.strategy_evidence import strategy_source_hash
from strategies.cycle_of_price_action import CycleOfPriceActionStrategy

DATASET_MANIFEST = Path("docs/research-runs/copa-price-only-v1-dataset.json")
PROTOCOL = Path("docs/research-runs/copa-price-only-v1-protocol.md")
RESULT_SUMMARY = Path("docs/research-runs/copa-price-only-v1-results.json")
FROZEN_EVIDENCE = Path("docs/research-runs/copa-price-only-v1-frozen-evidence.json")
EXECUTION_FILES = (
    Path("quant_engine/backtest/engine.py"),
    Path("quant_engine/backtest/matcher.py"),
    Path("quant_engine/backtest/cost_model.py"),
    Path("quant_engine/backtest/portfolio.py"),
    Path("quant_engine/backtest/data_handler.py"),
    Path("quant_engine/data/api.py"),
    Path("quant_engine/data/adjust.py"),
    Path("strategies/cycle_of_price_action.py"),
)
PERIODS = {
    "train": (date(2020, 1, 2), date(2022, 12, 30)),
    "validation": (date(2023, 1, 3), date(2024, 6, 28)),
    "sealed_oos": (date(2024, 7, 1), date(2026, 8, 31)),
}


class _EqualWeightBaseline(Strategy):
    def initialize(self) -> None:
        pass

    def generate_signals(self, dt: date) -> dict[str, float]:
        codes = sorted(self.ctx.universe)
        return {code: 0.9 / len(codes) for code in codes} if codes else {}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _metrics(result_dir: Path, initial_capital: float) -> dict[str, Any]:
    summary = json.loads((result_dir / "summary.json").read_text(encoding="utf-8"))
    coverage = summary.get("daily_data_coverage") or {}
    portfolio = pd.read_parquet(result_dir / "daily_portfolio.parquet").sort_values("date")
    returns = portfolio["daily_return"].astype(float).fillna(0.0)
    final_value = float(portfolio.iloc[-1]["total_value"])
    years = max(len(portfolio) / 252.0, 1.0 / 252.0)
    annual_return = (final_value / initial_capital) ** (1.0 / years) - 1.0
    drawdown = float(max_drawdown(returns)[0])
    trades_path = result_dir / "trades.parquet"
    trades = pd.read_parquet(trades_path) if trades_path.exists() else pd.DataFrame()
    traded_amount = float(trades["amount"].abs().sum()) if "amount" in trades else 0.0
    average_equity = float(portfolio["total_value"].mean())
    sharpe = float(sharpe_ratio(returns))
    calmar = annual_return / abs(drawdown) if drawdown < 0 else None
    pnl_by_security: dict[str, float] = {}
    if not trades.empty:
        for code, code_trades in trades.groupby("code"):
            pnl = 0.0
            for trade in code_trades.to_dict("records"):
                costs = sum(float(trade.get(field) or 0.0) for field in (
                    "commission", "stamp_duty", "slippage",
                ))
                amount = float(trade["amount"])
                pnl += amount - costs if trade["side"] == "sell" else -amount - costs
            pnl_by_security[str(code)] = pnl
    positions_path = result_dir / "daily_positions.parquet"
    if positions_path.exists():
        positions = pd.read_parquet(positions_path)
        if not positions.empty:
            final_date = positions["date"].max()
            for row in positions[positions["date"] == final_date].to_dict("records"):
                pnl_by_security[str(row["code"])] = (
                    pnl_by_security.get(str(row["code"]), 0.0)
                    + float(row["market_value"])
                )
    positive = [value for value in pnl_by_security.values() if value > 0]
    top_positive_ratio = max(positive) / sum(positive) if positive else None
    return {
        "start_date": str(portfolio.iloc[0]["date"]),
        "end_date": str(portfolio.iloc[-1]["date"]),
        "trading_days": len(portfolio),
        "final_value": final_value,
        "total_return": final_value / initial_capital - 1.0,
        "annual_return": annual_return,
        "sharpe": sharpe if math.isfinite(sharpe) else None,
        "max_drawdown": drawdown,
        "calmar": calmar if calmar is None or math.isfinite(calmar) else None,
        "trade_count": len(trades),
        "annual_turnover_proxy": traded_amount / average_equity / years if average_equity > 0 else None,
        "time_in_market": float((portfolio["market_value"] > 0).mean()),
        "pnl_by_security": pnl_by_security,
        "top_positive_contribution_ratio": top_positive_ratio,
        "run_dataset_hash": coverage.get("dataset_hash"),
        "run_coverage_hash": coverage.get("coverage_hash"),
        "run_universe": sorted(
            str(item["code"])
            for item in coverage.get("items", [])
            if isinstance(item, dict) and item.get("code")
        ),
    }


def _run_one(
    *,
    label: str,
    period: str,
    start: date,
    end: date,
    universe: list[str],
    output_root: Path,
    initial_capital: float,
    cost_scenario: str,
) -> dict[str, Any]:
    result_dir = output_root / period / f"{label}-{cost_scenario}"
    if result_dir.exists():
        shutil.rmtree(result_dir)
    if label == "S0":
        engine = BacktestEngine(
            _EqualWeightBaseline,
            stock_list=universe,
            cost_scenario=cost_scenario,
        )
    else:
        engine = BacktestEngine(
            CycleOfPriceActionStrategy,
            stock_list=universe,
            cost_scenario=cost_scenario,
            ablation_stage=label,
        )
    path = Path(engine.run(
        start=start,
        end=end,
        initial_capital=initial_capital,
        output_dir=str(result_dir),
        rebalance_frequency="daily",
    ))
    try:
        display_path = str(path.relative_to(Path.cwd()))
    except ValueError:
        display_path = str(path)
    return {
        "period": period,
        "stage": label,
        "cost_scenario": cost_scenario,
        "result_dir": display_path,
        **_metrics(path, initial_capital),
    }


def run_matrix(
    output_root: Path,
    initial_capital: float = 1_000_000.0,
    workers: int = 4,
) -> dict[str, Any]:
    dataset = json.loads(DATASET_MANIFEST.read_text(encoding="utf-8"))
    frozen = json.loads(FROZEN_EVIDENCE.read_text(encoding="utf-8"))
    universe = list(dataset["universe"])
    DataAPI._instance = None
    coverage = DataAPI().daily_coverage(
        universe,
        date.fromisoformat(dataset["start_date"]),
        date.fromisoformat(dataset["end_date"]),
        fields=["open", "high", "low", "close", "volume", "amount"],
        adjust="event_driven",
    )
    if not coverage.get("complete"):
        raise RuntimeError("frozen dataset coverage is no longer complete")
    if coverage.get("dataset_hash") != dataset["dataset_hash"]:
        raise RuntimeError("frozen dataset hash changed")
    actual_evidence = {
        "dataset_id": dataset["dataset_id"],
        "dataset_hash": dataset["dataset_hash"],
        "coverage_hash": coverage["coverage_hash"],
        "calendar_source": coverage["calendar_source"],
        "calendar_content_hash": coverage["calendar_content_hash"],
        "protocol_sha256": _sha256(PROTOCOL),
        "strategy_source_hash": strategy_source_hash(
            "strategies.cycle_of_price_action.CycleOfPriceActionStrategy"
        ),
        "execution_code_hashes": {
            str(path): _sha256(path) for path in EXECUTION_FILES
        },
    }
    if actual_evidence != frozen["expected"]:
        raise RuntimeError("frozen research evidence changed")

    jobs: list[dict[str, Any]] = []
    for period, (start, end) in PERIODS.items():
        jobs.append({
            "label": "S0", "period": period, "start": start, "end": end,
            "universe": universe, "output_root": output_root,
            "initial_capital": initial_capital, "cost_scenario": "paper_baseline_v1",
        })
        for stage in ("S1", "S2", "S3", "S4", "S5"):
            jobs.append({
                "label": stage, "period": period, "start": start, "end": end,
                "universe": universe, "output_root": output_root,
                "initial_capital": initial_capital, "cost_scenario": "paper_baseline_v1",
            })
        jobs.append({
            "label": "S5", "period": period, "start": start, "end": end,
            "universe": universe, "output_root": output_root,
            "initial_capital": initial_capital, "cost_scenario": "paper_high_impact_v1",
        })
    rows: list[dict[str, Any]] = []
    with ProcessPoolExecutor(
        max_workers=max(1, workers),
        mp_context=get_context("spawn"),
    ) as pool:
        futures = [pool.submit(_run_one, **job) for job in jobs]
        for future in as_completed(futures):
            rows.append(future.result())
    period_order = {name: index for index, name in enumerate(PERIODS)}
    rows.sort(key=lambda row: (
        period_order[row["period"]],
        row["stage"],
        row["cost_scenario"],
    ))
    for period in PERIODS:
        baseline = next(
            row for row in rows
            if row["period"] == period and row["stage"] == "S0"
        )
        for row in rows:
            if row["period"] == period:
                row["excess_return_vs_s0"] = row["total_return"] - baseline["total_return"]

    sealed = next(
        row for row in rows
        if row["period"] == "sealed_oos"
        and row["stage"] == "S5"
        and row["cost_scenario"] == "paper_baseline_v1"
    )
    sealed_high = next(
        row for row in rows
        if row["period"] == "sealed_oos"
        and row["stage"] == "S5"
        and row["cost_scenario"] == "paper_high_impact_v1"
    )
    total_trades = sum(
        row["trade_count"] for row in rows
        if row["stage"] == "S5" and row["cost_scenario"] == "paper_baseline_v1"
    )
    baseline_period_rows = [
        row for row in rows
        if row["stage"] == "S5" and row["cost_scenario"] == "paper_baseline_v1"
    ]
    gate_checks = {
        "sealed_excess_positive": sealed["excess_return_vs_s0"] > 0,
        "sealed_sharpe_at_least_0_8": sealed["sharpe"] is not None and sealed["sharpe"] >= 0.8,
        "sealed_calmar_at_least_0_5": sealed["calmar"] is not None and sealed["calmar"] >= 0.5,
        "sealed_max_drawdown_at_most_25pct": sealed["max_drawdown"] >= -0.25,
        "high_impact_excess_non_negative": sealed_high["excess_return_vs_s0"] >= 0,
        "high_impact_sharpe_at_least_0_5": sealed_high["sharpe"] is not None and sealed_high["sharpe"] >= 0.5,
        "s5_total_trades_at_least_100": total_trades >= 100,
        "s5_has_trades_in_every_period": all(row["trade_count"] > 0 for row in baseline_period_rows),
        "top_security_positive_contribution_at_most_40pct": (
            sealed["top_positive_contribution_ratio"] is not None
            and sealed["top_positive_contribution_ratio"] <= 0.40
        ),
        "share_adjusted_volume_verified": False,
        "historical_price_limit_and_suspension_verified": False,
    }
    payload = {
        "experiment_id": "copa-price-only-v1-frozen-matrix",
        "dataset_id": dataset["dataset_id"],
        "dataset_hash": dataset["dataset_hash"],
        "coverage_hash": coverage["coverage_hash"],
        "protocol_sha256": actual_evidence["protocol_sha256"],
        "strategy_id": CycleOfPriceActionStrategy.SPEC.id,
        "strategy_version": CycleOfPriceActionStrategy.SPEC.version,
        "strategy_source_hash": actual_evidence["strategy_source_hash"],
        "strategy_parameters": CycleOfPriceActionStrategy.SPEC.validate_params({
            "ablation_stage": "S5",
        }),
        "calendar_source": actual_evidence["calendar_source"],
        "calendar_content_hash": actual_evidence["calendar_content_hash"],
        "execution_code_hashes": actual_evidence["execution_code_hashes"],
        "authorized_observation_evidence": {
            "period": "sealed_oos",
            "stage": "S5",
            "cost_scenario": "paper_baseline_v1",
            "dataset_hash": sealed["run_dataset_hash"],
            "coverage_hash": sealed["run_coverage_hash"],
            "universe": sealed["run_universe"],
        },
        "initial_capital": initial_capital,
        "results": rows,
        "research_gate": {
            "passed": all(gate_checks.values()),
            "checks": gate_checks,
            "status": "eligible_for_paper_observation" if all(gate_checks.values()) else "blocked_by_research_gate",
        },
    }
    RESULT_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    RESULT_SUMMARY.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("backtest_result/copa-price-only-v1"))
    parser.add_argument("--initial-capital", type=float, default=1_000_000.0)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    payload = run_matrix(args.output_dir.resolve(), args.initial_capital, args.workers)
    print(json.dumps(payload["research_gate"], ensure_ascii=False))


if __name__ == "__main__":
    main()
