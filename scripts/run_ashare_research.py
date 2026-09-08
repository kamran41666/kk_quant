"""Frozen, auditable multi-strategy A-share experiment matrix.

Use --stage discovery before validation/oos/full. Changed code/data creates a
new run identity; existing result directories are never silently overwritten.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import math
import time
import platform
import shutil
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.acquire_ashare_research import digest, write_json

REPO = Path(__file__).resolve().parents[1]
STRATEGIES = {
    "low_volatility": "strategies.research_low_volatility.ResearchLowVolatilityStrategy",
    "short_reversal": "strategies.research_short_reversal.ResearchShortReversalStrategy",
    "momentum": "strategies.research_momentum.ResearchMomentumStrategy",
}
PERIODS = {
    "discovery": ("2015-01-05", "2018-12-28"),
    "validation": ("2019-01-02", "2022-12-30"),
    "oos": ("2023-01-03", "2026-08-31"),
    "full": ("2015-01-05", "2026-08-31"),
}
PERIOD_LABELS = {"discovery": "发现期", "validation": "验证期", "oos": "时间留出", "full": "长周期全段"}
_DATA = None


def implementation(path: str):
    module, name = path.rsplit(".", 1)
    return getattr(importlib.import_module(module), name)


def code_hashes() -> dict:
    files = [*Path("quant_engine/backtest").glob("*.py"),
             *Path("strategies").glob("*research*.py"),
             Path("scripts/run_ashare_research.py"), Path("scripts/build_ashare_research_dataset.py"),
             Path("docs/research-runs/ashare-multi-strategy-protocol.md")]
    return {str(path): digest(path) for path in sorted(files)}


def environment_versions() -> dict:
    return {"python": platform.python_version(), **{
        name: importlib.metadata.version(name) for name in ("numpy", "pandas", "pyarrow", "baostock")
    }}


def load_inputs(manifest_path: str):
    global _DATA
    manifest = json.loads(Path(manifest_path).read_text())
    inputs = {}
    for name in ("daily", "actions", "securities"):
        item = manifest["files"][name]
        if digest(Path(item["path"])) != item["sha256"]:
            raise ValueError(f"dataset file changed: {name}")
        inputs[name] = pd.read_parquet(item["path"])
    _DATA = (manifest, inputs)


def metrics(directory: Path) -> dict:
    summary = json.loads((directory / "summary.json").read_text())
    portfolio = pd.read_parquet(directory / "daily_portfolio.parquet").sort_values("date")
    equity = portfolio.total_value.to_numpy(dtype=float)
    if not np.isfinite(equity).all() or (equity <= 0).any():
        raise ValueError("nonpositive or nonfinite equity curve")
    initial = float(summary["initial_capital"])
    returns = portfolio.daily_return.to_numpy(dtype=float)
    reconstructed = initial * np.cumprod(1 + returns)
    if not np.allclose(equity, reconstructed, atol=.01, rtol=1e-8):
        raise ValueError("daily return compound does not reconcile with recorded equity")
    start, end = pd.Timestamp(portfolio.date.iloc[0]), pd.Timestamp(portfolio.date.iloc[-1])
    years = max(((end - start).days + 1) / 365.2425, 1 / 365.2425)
    deviation = float(np.std(returns, ddof=1)) if len(returns) > 1 else 0.
    peak = np.maximum.accumulate(np.r_[initial, equity])[1:]
    drawdown = float(np.min(equity / peak - 1))
    trades = pd.read_parquet(directory / "trades.parquet") if (directory / "trades.parquet").exists() else pd.DataFrame()
    fees = sum(float(pd.to_numeric(trades[field], errors="raise").sum()) for field in ("commission", "stamp_duty", "slippage") if field in trades)
    turnover = float(trades.amount.abs().sum()) / float(equity.mean()) / years if len(trades) else 0.
    annual = float((equity[-1] / initial) ** (1 / years) - 1)
    return {"total_return": float(equity[-1] / initial - 1), "annual_return": annual,
            "sharpe": float(np.mean(returns) / deviation * math.sqrt(252)) if deviation > 0 else None,
            "annual_volatility": deviation * math.sqrt(252), "max_drawdown": drawdown,
            "calmar": annual / abs(drawdown) if drawdown < 0 else None,
            "trade_count": len(trades), "explicit_fees": fees, "annual_turnover": turnover,
            "final_value": float(equity[-1]), "trading_days": len(equity),
            "start_date": str(start.date()), "end_date": str(end.date()),
            "time_in_market": float((portfolio.market_value > 0).mean()),
            "return_reconciliation_passed": True,
            "annualization": "CAGR uses elapsed calendar years; Sharpe/volatility use 252 sessions, rf=0"}


def run_case(case: dict, output_root: str, frozen_hashes: dict) -> dict:
    from quant_engine.backtest.research_engine import ResearchBacktestEngine
    from quant_engine.data.calendar import TradingCalendar
    manifest, frames = _DATA
    identity = {"case": case, "dataset": manifest["content_hash"], "code_hashes": frozen_hashes,
                "environment": environment_versions()}
    run_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]
    directory = Path(output_root) / run_id
    result = {**case, "run_id": run_id, "result_dir": str(directory.resolve())}
    if (directory / "research-run.json").exists():
        saved = json.loads((directory / "research-run.json").read_text())
        if saved.get("identity") != identity:
            raise ValueError("existing run identity mismatch")
        return saved["result"]
    if directory.exists():
        # Preserve failed/interrupted execution artifacts. A suffix creates a
        # fresh attempt while keeping the same deterministic economic inputs.
        attempt = 2
        while directory.with_name(f"{run_id}-attempt{attempt}").exists():
            attempt += 1
        directory = directory.with_name(f"{run_id}-attempt{attempt}")
        result["result_dir"] = str(directory.resolve())
    started = time.monotonic()
    try:
        engine = ResearchBacktestEngine(implementation(case["implementation"]),
                                       parameters=case["parameters"], cost_scenario=case["cost_scenario"])
        engine.run(**frames, start=date.fromisoformat(case["start_date"]), end=date.fromisoformat(case["end_date"]),
                   calendar=TradingCalendar(start_year=2013, end_year=2026),
                   output_dir=str(directory), initial_capital=1_000_000)
        summary = json.loads((directory / "summary.json").read_text())
        quality_issues = []
        for item in manifest["quality"]:
            gaps = [d for d in item["missing_dates"] if case["start_date"] <= d <= case["end_date"]]
            unexplained = [d for d in item["unexplained_reference_adjustments"] if case["start_date"] <= d["date"] <= case["end_date"]]
            if gaps or unexplained or item["invalid_trading_rows"]:
                quality_issues.append({"code": item["code"], "missing_count": len(gaps),
                                       "unexplained_actions": len(unexplained), "invalid_trading_rows": item["invalid_trading_rows"]})
        limits = list(summary.get("limitations", []))
        if quality_issues:
            limits.append(f"dataset has unresolved coverage/action issues for {len(quality_issues)} securities; see dataset_quality_issues")
        summary.update(research_experiment=True, data_content_hash=manifest["content_hash"],
                       dataset_id=manifest["dataset_id"], code_hashes=frozen_hashes,
                       universe_count=manifest["universe_count"], universe=manifest["universe"],
                       dataset_quality_issues=quality_issues, limitations=limits,
                       validated=bool(summary.get("validated")) and not quality_issues,
                       paper_authorized=False)
        summary["environment"] = identity["environment"]
        summary["signal_adjustment"] = manifest.get("signal_adjustment", "vendor_factor")
        write_json(directory / "summary.json", summary)
        run_metrics = metrics(directory)
        summary["research_metrics"] = run_metrics
        write_json(directory / "summary.json", summary)
        result.update(status="completed", metrics=run_metrics, validated=summary["validated"],
                      limitations=limits, seconds=round(time.monotonic() - started, 3))
    except Exception as exc:
        result.update(status="failed", error=f"{type(exc).__name__}: {exc}", validated=False,
                      seconds=round(time.monotonic() - started, 3))
    write_json(directory / "research-run.json", {"identity": identity, "result": result})
    return result


def compare_payload(directory: Path, benchmark_files: dict) -> dict:
    from scripts.render_ashare_research import maximum_rise, maximum_drawdown, normalize_benchmark
    portfolio = pd.read_parquet(directory / "daily_portfolio.parquet").sort_values("date")
    summary = json.loads((directory / "summary.json").read_text())
    dates = pd.DatetimeIndex(pd.to_datetime(portfolio.date))
    labels = [str(d.date()) for d in dates]
    nav = portfolio.total_value.to_numpy() / float(summary["initial_capital"])
    series = [{"name": "策略净值", "values": nav.tolist(), "color": "#ef5350"}]
    for key, name, color in (("sse", "上证指数", "#4294ff"), ("csi300", "沪深300", "#30bd85")):
        frame = pd.read_parquet(benchmark_files[key])
        frame["date"] = pd.to_datetime(frame.date)
        values = normalize_benchmark(frame.set_index("date"), dates)
        series.append({"name": name, "values": [float(x) if pd.notna(x) else None for x in values], "color": color})
    def interval(item):
        return {"start_date": labels[item.start], "end_date": labels[item.end], "change": item.change} if item else None
    return {"labels": labels, "series": series, "max_gain": interval(maximum_rise(nav)),
            "max_drawdown": interval(maximum_drawdown(nav)), "validated": summary["validated"],
            "limitations": summary.get("limitations", []),
            "note": "策略为含账户公司行动的研究净值；上证/沪深300为价格指数，不含分红。最大区间为全回测区间统计。"}


def register_runs(matrix: dict) -> None:
    """Expose reviewed research artifacts through existing result pages only."""
    from server.models.database import init_db, SessionLocal
    from server.models.schema import Run, Strategy
    init_db()
    with SessionLocal() as db:
        for result in matrix["runs"]:
            if result["status"] != "completed":
                continue
            cls = implementation(result["implementation"])
            strategy_id = cls.SPEC.id
            if db.get(Strategy, strategy_id) is None:
                db.add(Strategy(id=strategy_id, name=cls.SPEC.name, description=cls.SPEC.description,
                                strategy_class=result["implementation"], params=json.dumps(cls.SPEC.validate_params({})), market="a-share"))
                # SessionLocal intentionally disables autoflush. Persist the
                # pending identity before the next experiment queries it.
                db.flush()
            path = Path(result["result_dir"])
            summary = json.loads((path / "summary.json").read_text())
            write_json(path / "comparison.json", compare_payload(path, matrix["benchmark_files"]))
            m = result["metrics"]
            manifest = {**summary, "research_experiment": True, "research_label": result["label"],
                        "research_period": result["period"], "cost_scenario": result["cost_scenario"],
                        "strategy_parameters": result["parameters"], "market": "a-share",
                        "calendar_version": "trading-calendar-v1", "data_end": m["end_date"]}
            manifest["comparison_content_hash"] = digest(path / "comparison.json")
            record = db.get(Run, result["run_id"])
            if record is None:
                record = Run(id=result["run_id"], strategy_id=strategy_id, run_type="backtest", market="a-share")
                db.add(record)
            record.status = "completed"
            record.start_date, record.end_date = result["start_date"], result["end_date"]
            record.initial_capital, record.final_value = 1_000_000, m["final_value"]
            record.total_return, record.sharpe_ratio, record.max_drawdown = m["total_return"], m["sharpe"], m["max_drawdown"]
            record.result_dir, record.data_end = str(path.resolve()), m["end_date"]
            record.execution_model = summary["execution_model"]
            record.calendar_version = "trading-calendar-v1"
            record.data_manifest = json.dumps(manifest, ensure_ascii=False, allow_nan=False)
            record.eligible_for_observation = False
            record.completed_at = datetime.now().isoformat()
        db.commit()


def run_matrix(manifest_path: Path, output: Path, stages: list[str], workers: int, sensitivity: bool, register: bool):
    manifest = json.loads(manifest_path.read_text())
    output.mkdir(parents=True, exist_ok=True)
    benchmarks_root = manifest_path.parent.parent / "source/benchmarks"
    matrix = {"dataset": {key: manifest[key] for key in ("dataset_id", "content_hash", "universe_count", "start_date", "end_date", "limitations")},
              "environment": environment_versions(),
              "benchmark_files": {"sse": str((benchmarks_root / "sh.000001.parquet").resolve()),
                                  "csi300": str((benchmarks_root / "sh.000300.parquet").resolve())}, "runs": []}
    hashes = code_hashes()
    cases = []
    for stage in stages:
        start, end = PERIODS[stage]
        for name, path in STRATEGIES.items():
            cls = implementation(path)
            variants = [("base", {})]
            if sensitivity and stage in {"discovery", "validation"}:
                variants.append(("window", {"lookback": {"low_volatility": 60, "short_reversal": 10, "momentum": 126}[name]}))
            for variant, params in variants:
                for cost in ("baseline", "stress"):
                    cost_label = "基线" if cost == "baseline" else "压力"
                    variant_label = "默认窗口" if variant == "base" else "窗口敏感性"
                    cases.append({"label": f"{cls.SPEC.name} · {PERIOD_LABELS[stage]} · {cost_label} · {variant_label}", "strategy_id": cls.SPEC.id,
                                  "implementation": path, "period": stage, "cost_scenario": cost,
                                  "parameters": cls.SPEC.validate_params(params), "start_date": start, "end_date": end})
    frozen = {"cases": cases, "code_hashes": hashes, "dataset_hash": manifest["content_hash"], "environment": environment_versions()}
    frozen_path = output / "frozen-matrix.json"
    if frozen_path.exists() and json.loads(frozen_path.read_text()) != frozen:
        raise ValueError("experiment definition changed; use a new output directory to preserve prior evidence")
    write_json(frozen_path, frozen)
    for relative, expected in hashes.items():
        source = Path(relative)
        archived = output / "code-snapshot" / relative
        if digest(source) != expected:
            raise ValueError(f"code changed while freezing experiment: {relative}")
        archived.parent.mkdir(parents=True, exist_ok=True)
        if archived.exists() and digest(archived) != expected:
            raise ValueError(f"archived code content changed: {relative}")
        shutil.copyfile(source, archived)
    with ProcessPoolExecutor(max_workers=workers, initializer=load_inputs, initargs=(str(manifest_path),)) as pool:
        tasks = {pool.submit(run_case, case, str(output), hashes): case for case in cases}
        for future in as_completed(tasks):
            result = future.result()
            matrix["runs"].append(result)
            matrix["runs"].sort(key=lambda r: (r["period"], r["label"]))
            write_json(output / "matrix.json", matrix)
            print(json.dumps({"completed": len(matrix["runs"]), "total": len(cases), "label": result["label"],
                              "status": result["status"], "seconds": result["seconds"], "error": result.get("error")}, ensure_ascii=False), flush=True)
    if register:
        register_runs(matrix)
    return matrix


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("data/research/ashare-inception-2014-v1/normalized-v1/manifest.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage", choices=[*PERIODS, "all"], default="discovery")
    parser.add_argument("--workers", type=int, choices=range(1, 4), default=2)
    parser.add_argument("--sensitivity", action="store_true")
    parser.add_argument("--register", action="store_true")
    args = parser.parse_args()
    run_matrix(args.manifest, args.output, list(PERIODS) if args.stage == "all" else [args.stage], args.workers, args.sensitivity, args.register)
