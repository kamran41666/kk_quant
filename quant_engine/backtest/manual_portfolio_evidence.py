"""Independent metrics, replay and atomic evidence writer for portfolio v3."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import tempfile
from typing import Any, Mapping, Sequence

import pandas as pd

from quant_engine.backtest.manual_research_ledger import RESEARCH_EXECUTION_COSTS
from quant_engine.trading.effective_rules import EffectiveDatedTradingRuleRegistry


ZERO = Decimal("0")
CENT = Decimal("0.01")


def _decimal(value: Any, field_name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError(f"{field_name}_must_be_numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name}_must_be_finite")
    return result


def _money(value: Any) -> Decimal:
    return _decimal(value, "money").quantize(CENT, rounding=ROUND_HALF_UP)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def calculate_portfolio_metrics(result: Any) -> dict[str, str | int]:
    if not result.daily or not result.benchmark:
        raise ValueError("portfolio_metrics_require_daily_and_benchmark")
    equities = [_decimal(row["equity"], "equity") for row in result.daily]
    returns = [_decimal(row["daily_return"], "daily_return") for row in result.daily]
    total_return = equities[-1] / result.initial_capital - Decimal("1")
    benchmark_return = _decimal(result.benchmark[-1]["nav"], "benchmark_nav") - Decimal("1")
    numeric_returns = [float(value) for value in returns]
    sharpe: Decimal | None = None
    if len(numeric_returns) >= 2:
        deviation = statistics.stdev(numeric_returns)
        if deviation > 0:
            sharpe = Decimal(str(statistics.mean(numeric_returns) / deviation * math.sqrt(252)))
    peak = result.initial_capital
    max_drawdown = ZERO
    for equity in equities:
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    gross_turnover = sum((_decimal(row["gross"], "trade_gross") for row in result.trades), ZERO)
    average_equity = sum(equities, ZERO) / len(equities)
    elapsed_years = max(
        Decimal((result.end_date - result.start_date).days) / Decimal("365.2425"),
        Decimal("1") / Decimal("252"),
    )
    annual_turnover = gross_turnover / average_equity / elapsed_years if average_equity else ZERO
    logical: dict[str, dict[str, Any]] = {}
    for row in result.intents:
        logical_id = row.get("logical_intent_id") or row["intent_id"]
        item = logical.setdefault(logical_id, {
            "requested_notional": _decimal(row["requested_notional"], "requested_notional"),
            "requested_quantity": int(row["requested_quantity"]),
            "reference_price": _decimal(row["reference_price"], "reference_price"),
            "filled_quantity": 0,
        })
        item["filled_quantity"] += int(row["filled_quantity"])
    requested = sum((item["requested_notional"] for item in logical.values()), ZERO)
    filled = sum((
        item["reference_price"] * min(item["filled_quantity"], item["requested_quantity"])
        for item in logical.values()
    ), ZERO)
    fill_rate = filled / requested if requested else None
    total_fees = sum((_decimal(row["total_fee"], "total_fee") for row in result.trades), ZERO)
    return {
        "total_return": str(total_return),
        "benchmark_total_return": str(benchmark_return),
        "excess_return": str(total_return - benchmark_return),
        "sharpe_252_rf0": str(sharpe) if sharpe is not None else "null",
        "max_drawdown_magnitude": str(max_drawdown),
        "annual_turnover_double_sided": str(annual_turnover),
        "capacity_fill_rate_amount_weighted": str(fill_rate) if fill_rate is not None else "null",
        "gross_turnover": str(gross_turnover),
        "total_fees": str(total_fees),
        "trade_count": len(result.trades),
        "intent_count": len(result.intents),
    }


def replay_portfolio_result(result: Any) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []

    def fail(check: str, **details: Any) -> None:
        if not any(item["check"] == check and item.get("details") == details for item in errors):
            errors.append({"check": check, "details": details})

    intent_ids = [row["intent_id"] for row in result.intents]
    if len(intent_ids) != len(set(intent_ids)):
        fail("intent_ids_unique")
    known_intents = set(intent_ids)
    trade_ids = set()
    trade_by_intent: dict[str, int] = defaultdict(int)
    for trade in result.trades:
        if trade["trade_id"] in trade_ids:
            fail("trade_ids_unique", trade_id=trade["trade_id"])
        trade_ids.add(trade["trade_id"])
        if trade["intent_id"] not in known_intents:
            fail("trade_binds_intent", trade_id=trade["trade_id"])
        trade_by_intent[trade["intent_id"]] += int(trade["quantity"])
    for intent in result.intents:
        if trade_by_intent[intent["intent_id"]] != int(intent["filled_quantity"]):
            fail("intent_filled_quantity", intent_id=intent["intent_id"])
        if int(intent["filled_quantity"]) > int(intent["requested_quantity"]):
            fail("intent_overfill", intent_id=intent["intent_id"])

    capacity_used: dict[tuple[str, str], int] = defaultdict(int)
    attempt_ids = set()
    for attempt in result.attempts:
        if attempt["attempt_id"] in attempt_ids:
            fail("attempt_ids_unique", attempt_id=attempt["attempt_id"])
        attempt_ids.add(attempt["attempt_id"])
        if attempt["intent_id"] not in known_intents:
            fail("attempt_binds_intent", attempt_id=attempt["attempt_id"])
        key = (attempt["date"], attempt["code"])
        if int(attempt["capacity_used_before"]) != capacity_used[key]:
            fail("capacity_sequence", attempt_id=attempt["attempt_id"])
        capacity_used[key] += int(attempt["filled_quantity"])
        if capacity_used[key] > int(attempt["capacity_total"]):
            fail("capacity_limit", attempt_id=attempt["attempt_id"])

    registry = EffectiveDatedTradingRuleRegistry.default()
    costs = RESEARCH_EXECUTION_COSTS[result.bundle.cost_scenario]
    for trade in result.trades:
        gross = _money(trade["gross"])
        if gross != _money(_decimal(trade["price"], "trade_price") * int(trade["quantity"])):
            fail("trade_gross_replay", trade_id=trade["trade_id"])
        commission = max(_money(gross * costs["commission_rate"]), costs["minimum_commission"])
        transfer = _money(gross * costs["transfer_rate"])
        stamp_rate = registry.resolve("a-share", date.fromisoformat(trade["date"])).stamp_duty_rate if trade["side"] == "sell" else ZERO
        stamp = _money(gross * stamp_rate)
        if commission != _decimal(trade["commission"], "commission"):
            fail("commission_replay", trade_id=trade["trade_id"])
        if transfer != _decimal(trade["transfer_fee"], "transfer_fee"):
            fail("transfer_fee_replay", trade_id=trade["trade_id"])
        if stamp != _decimal(trade["stamp_duty"], "stamp_duty"):
            fail("stamp_duty_replay", trade_id=trade["trade_id"])
        if commission + transfer + stamp != _decimal(trade["total_fee"], "total_fee"):
            fail("total_fee_replay", trade_id=trade["trade_id"])

    trades_by_day: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    actions_by_day: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    positions_by_day: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in result.trades:
        trades_by_day[row["date"]].append(row)
    for row in result.corporate_actions:
        actions_by_day[row["date"]].append(row)
        if row.get("source_hash") != result.input_manifest.corporate_action_content_hash:
            fail("corporate_action_source_hash", action_id=row.get("action_id"))
    for row in result.positions:
        positions_by_day[row["date"]].append(row)

    cash = _money(result.initial_capital)
    receivable = ZERO
    shares: dict[tuple[str, str], int] = defaultdict(int)
    prior_equity = _decimal(result.initial_capital, "initial_capital")
    for daily in result.daily:
        day = daily["date"]
        for action in actions_by_day[day]:
            if action["stage"] == "ex_cash":
                receivable += _decimal(action["receivable_cash"], "receivable_cash")
            elif action["stage"] == "pay":
                amount = _decimal(action["cash"], "cash")
                receivable -= amount
                cash += amount
            elif action["stage"] == "ex_bonus":
                shares[(action["cohort_id"], action["code"])] += int(action["bonus_quantity"])
        for trade in trades_by_day[day]:
            gross = _decimal(trade["gross"], "gross")
            fee = _decimal(trade["total_fee"], "total_fee")
            quantity = int(trade["quantity"])
            key = (trade["cohort_id"], trade["code"])
            if trade["side"] == "buy":
                cash -= gross + fee
                shares[key] += quantity
            else:
                cash += gross - fee
                shares[key] -= quantity
                if shares[key] < 0:
                    fail("cohort_share_replay_negative", date=day, cohort_id=key[0], code=key[1])
        recorded_positions = {
            (row["cohort_id"], row["code"]): int(row["quantity"])
            for row in positions_by_day[day]
        }
        expected_positions = {key: value for key, value in shares.items() if value}
        if recorded_positions != expected_positions:
            fail("position_replay", date=day)
        market_value = ZERO
        for row in positions_by_day[day]:
            recorded_value = _decimal(row["market_value"], "market_value")
            expected_value = _money(_decimal(row["close"], "close") * int(row["quantity"]))
            if _money(recorded_value) != expected_value:
                fail("position_market_value_replay", date=day, cohort_id=row["cohort_id"], code=row["code"])
            market_value += recorded_value
        if _money(market_value) != _money(daily["market_value"]):
            fail("daily_market_value_replay", date=day)
        if len(positions_by_day[day]) != int(daily["position_count"]):
            fail("position_count_replay", date=day)
        if _money(cash) != _money(daily["cash"]):
            fail("cash_replay", date=day)
        if _money(receivable) != _money(daily["receivable_cash"]):
            fail("receivable_replay", date=day)
        equity = _money(cash + receivable + market_value)
        if equity != _money(daily["equity"]):
            fail("equity_replay", date=day)
        expected_return = equity / prior_equity - Decimal("1") if prior_equity else ZERO
        if abs(expected_return - _decimal(daily["daily_return"], "daily_return")) > Decimal("1e-10"):
            fail("daily_return_replay", date=day)
        prior_equity = equity

    checks = {
        "intent_trade_binding": not any(item["check"].startswith("intent_") or item["check"].startswith("trade_") for item in errors),
        "capacity_replay": not any(item["check"].startswith("capacity_") for item in errors),
        "fee_replay": not any("fee" in item["check"] or item["check"] == "commission_replay" for item in errors),
        "cash_replay": not any(item["check"] == "cash_replay" for item in errors),
        "receivable_replay": not any(item["check"] == "receivable_replay" for item in errors),
        "share_replay": not any(item["check"] in {"position_replay", "cohort_share_replay_negative", "position_count_replay"} for item in errors),
        "valuation_replay": not any(item["check"] in {"position_market_value_replay", "daily_market_value_replay"} for item in errors),
        "corporate_action_identity": not any(item["check"] == "corporate_action_source_hash" for item in errors),
        "equity_replay": not any(item["check"] in {"equity_replay", "daily_return_replay"} for item in errors),
    }
    accounting_passed = all(checks.values()) and not errors
    payload = {
        "protocol_version": "manual-portfolio-replay-v1",
        "result_hash": result.result_hash,
        "checks": checks,
        "accounting_passed": accounting_passed,
        "source_and_execution_passed": False,
        "target_completion_verifiable": False,
        "passed": False,
        "limitations": [
            "Frozen input files are not yet available to recheck prices, previous volume, eligibility and action definitions.",
            "Logical intent retry grouping is not yet complete.",
        ],
        "first_difference": errors[0] if errors else None,
        "differences": errors,
        "replay_code_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    return {**payload, "replay_hash": _hash(payload)}


_SCHEMAS = {
    "signals.parquet": ["signal_date", "code", "score", "rank_status", "cohort_id", "entry_date", "planned_exit_date", "cohort_budget", "cohort_terminal_status", "closed_date"],
    "order_intents.parquet": ["intent_id", "logical_intent_id", "date", "phase", "cohort_id", "code", "side", "requested_quantity", "reference_price", "requested_notional", "reason", "status", "filled_quantity"],
    "order_attempts.parquet": ["attempt_id", "intent_id", "logical_intent_id", "attempt_sequence", "date", "phase", "cohort_id", "code", "side", "requested_quantity", "capacity_total", "capacity_used_before", "capacity_available", "capacity_used_after", "filled_quantity", "status", "reason"],
    "trades.parquet": ["trade_id", "intent_id", "attempt_id", "event_sequence", "date", "phase", "cohort_id", "code", "side", "quantity", "price", "gross", "commission", "transfer_fee", "stamp_duty", "total_fee"],
    "corporate_actions.parquet": ["action_id", "action_hash", "economic_key", "date", "stage", "code", "cohort_id", "record_date", "ex_date", "pay_date", "stock_listing_date", "cash_per_share", "bonus_ratio", "allocation_verified", "eligible_quantity", "gross_cash", "receivable_cash", "cash", "bonus_quantity", "source_hash"],
    "positions.parquet": ["date", "cohort_id", "code", "quantity", "sellable_quantity", "close", "market_value"],
    "daily_portfolio.parquet": ["date", "cash", "receivable_cash", "market_value", "equity", "daily_return", "position_count"],
    "benchmark.parquet": ["date", "close", "nav", "daily_return"],
}


def _frame(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> pd.DataFrame:
    return pd.DataFrame([{column: row.get(column) for column in columns} for row in rows], columns=columns)


def write_portfolio_evidence(result: Any, output_dir: str | Path) -> Path:
    target = Path(output_dir)
    if target.exists():
        raise FileExistsError("portfolio_evidence_output_exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        rows_by_file = {
            "signals.parquet": result.signals,
            "order_intents.parquet": result.intents,
            "order_attempts.parquet": result.attempts,
            "trades.parquet": result.trades,
            "corporate_actions.parquet": result.corporate_actions,
            "positions.parquet": result.positions,
            "daily_portfolio.parquet": result.daily,
            "benchmark.parquet": result.benchmark,
        }
        for name, columns in _SCHEMAS.items():
            _frame(rows_by_file[name], columns).to_parquet(temporary / name, index=False)
        replay = replay_portfolio_result(result)
        metrics = calculate_portfolio_metrics(result)
        summary = {
            "protocol_version": "manual-daily-portfolio-summary-v3",
            "result_hash": result.result_hash, "metrics": metrics,
            "quality_errors": result.quality_errors,
            "accounting_replay_passed": replay["accounting_passed"],
            "source_and_execution_passed": replay["source_and_execution_passed"],
            "promotion_eligible": False,
            "evidence_status": "unregistered",
        }
        (temporary / "audit.json").write_text(json.dumps(result.audit, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        (temporary / "replay.json").write_text(json.dumps(replay, ensure_ascii=False, indent=2), encoding="utf-8")
        (temporary / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        files = []
        for path in sorted(temporary.iterdir(), key=lambda item: item.name):
            files.append({"path": path.name, "size": path.stat().st_size, "sha256": _file_hash(path)})
        manifest = {
            "protocol_version": "manual-daily-portfolio-evidence-v1",
            "strategy_core_hash": result.bundle.strategy_core_hash,
            "scenario_bundle_hash": result.bundle.bundle_hash,
            "scenario": result.bundle.cost_scenario,
            "input_manifest": result.input_manifest.__dict__,
            "input_manifest_hash": result.input_manifest.manifest_hash,
            "result_hash": result.result_hash,
            "metrics": metrics,
            "replay_hash": replay["replay_hash"],
            "quality_errors": result.quality_errors,
            "eligible_for_artifact_registration": replay["passed"] and not result.quality_errors,
            "files": files,
        }
        manifest["manifest_hash"] = _hash(manifest)
        (temporary / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
        return target.resolve()
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


__all__ = [
    "calculate_portfolio_metrics", "replay_portfolio_result",
    "write_portfolio_evidence",
]
