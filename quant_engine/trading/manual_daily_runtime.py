"""Pure, serializable engineering runtime for the manual daily protocol.

The runtime is intentionally detached from SQLAlchemy, HTTP and broker code.
It is a small vertical demonstration of the close-signal -> next-open entry ->
next-close exit protocol.  Its market data is synthetic and its result is
never evidence for a real-forward pilot.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pandas as pd

from quant_engine.backtest.manual_daily_portfolio_v3 import (
    ManualPortfolioInputManifest,
    run_manual_daily_portfolio_v3,
)
from quant_engine.backtest.manual_portfolio_evidence import calculate_portfolio_metrics
from quant_engine.backtest.manual_research_ledger import (
    ManualResearchLedger,
    ResearchExecutionIntent,
)
from quant_engine.factor.expression import FactorExpressionSpec
from quant_engine.factor.manual_daily_bundle import ManualDailyFactorBundleV2

VERSION = "manual-daily-runtime-v2"
CODES = ("000001.SZ", "000002.SZ", "600000.SH", "600001.SH")
LOT = 100
ZERO = Decimal(0)


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    return value


def _specs() -> list[FactorExpressionSpec]:
    payloads = [
        {
            "name": "close_momentum", "hypothesis": "短窗收盘动量", "direction": 1, "role": "rank", "source": "engineering_demo",
            "expression": {"op": "delta", "args": [{"field": "close"}], "params": {"periods": 1}},
        },
        {
            "name": "range_pressure", "hypothesis": "收盘相对日内区间", "direction": 1, "role": "rank", "source": "engineering_demo",
            "expression": {"op": "div", "args": [{"op": "sub", "args": [{"field": "close"}, {"field": "low"}]}, {"op": "sub", "args": [{"field": "high"}, {"field": "low"}]}]},
        },
        {
            "name": "volume_change", "hypothesis": "成交量变化", "direction": 1, "role": "rank", "source": "engineering_demo",
            "expression": {"op": "delta", "args": [{"field": "volume"}], "params": {"periods": 1}},
        },
    ]
    return [FactorExpressionSpec.from_dict(item) for item in payloads]


def _market(seed: int) -> tuple[list[str], dict[str, dict[str, dict[str, Any]]], dict[str, dict[str, float]], list[dict[str, Any]]]:
    dates = [value.date().isoformat() for value in pd.bdate_range("2024-01-02", periods=75)]
    bars: dict[str, dict[str, dict[str, Any]]] = {}
    for code_index, code in enumerate(CODES):
        previous = Decimal(10) + Decimal(code_index)
        bars[code] = {}
        for day_index, day in enumerate(dates):
            drift = Decimal(str((seed + code_index * 3 + day_index * (code_index + 1)) % 9 - 4)) / Decimal(100)
            opening = previous + drift
            close = opening + Decimal(str((seed + day_index + code_index) % 5 - 2)) / Decimal(100)
            close = max(Decimal(1), close)
            bars[code][day] = {
                "date": day, "code": code, "open": str(_money(opening)), "high": str(_money(max(opening, close) + Decimal("0.05"))),
                "low": str(_money(max(Decimal("0.01"), min(opening, close) - Decimal("0.05")))), "close": str(_money(close)),
                "preclose": str(_money(previous)), "volume": 2_000_000 + code_index * 100_000 + day_index * 1_000,
                "amount": str(_money(close * Decimal(2_000_000))), "turnover_rate": "0.02",
                "is_suspended": False, "is_st": False, "limit_up": False, "limit_down": False,
            }
            previous = close
    benchmark = {day: {"open": str(_money(3000 + i * 2)), "close": str(_money(3000 + i * 2 + ((seed + i) % 7 - 3) * 2))} for i, day in enumerate(dates)}
    return dates, bars, benchmark, [{"date": day, "value": value} for day, value in benchmark.items()]


def _signals(dates: list[str], bars: dict[str, dict[str, dict[str, Any]]], specs: list[FactorExpressionSpec]) -> tuple[dict[str, dict[str, float]], list[dict[str, Any]]]:
    records = [bars[code][day] | {"code": code, "date": day} for code in CODES for day in dates]
    frame = pd.DataFrame(records).set_index(["code", "date"]).sort_index()
    values = [spec.compute(frame).rename(spec.name) * spec.direction for spec in specs]
    panel = pd.concat(values, axis=1)
    combined = panel.mean(axis=1, skipna=True)
    output: dict[str, dict[str, float]] = {}
    for day in dates:
        rows = combined.xs(day, level="date", drop_level=True).dropna()
        output[day] = {str(code): float(value) for code, value in rows.sort_values(ascending=False).items()}
    return output, [{"name": spec.name, "expression_hash": spec.expression_hash, "lookback": spec.lookback, "direction": spec.direction} for spec in specs]


class _DemoCalendar:
    def __init__(self, days: list[str]):
        self.days = [date.fromisoformat(value) for value in days]
        self.content_hash = _sha(days)

    def ensure_coverage(self, start: date, end: date) -> dict[str, Any]:
        return {"complete": True, "verified": True, "content_hash": self.content_hash, "source": "engineering:business-day-calendar"}

    def get_trading_days(self, start: date, end: date) -> list[date]:
        return [day for day in self.days if start <= day <= end]


def _run_research(dates: list[str], bars: dict[str, dict[str, dict[str, Any]]], benchmark: dict[str, dict[str, str]], specs: list[FactorExpressionSpec]) -> tuple[dict[str, Any], dict[str, dict[str, float]]]:
    records = [bars[code][day] | {"code": code, "date": day} for code in CODES for day in dates]
    panel = pd.DataFrame(records)
    values = {spec.name: spec.compute(panel.set_index(["code", "date"])).rename(spec.name) * spec.direction for spec in specs}
    signals_by_spec = {
        name: {day: {str(code): float(score) for code, score in values[name].xs(day, level="date", drop_level=True).dropna().items()}
               for day in dates}
        for name in values
    }
    daily = panel[["date", "code", "open", "high", "low", "close", "preclose", "volume", "amount", "is_suspended", "is_st"]].copy()
    eligibility = daily[["date", "code"]].assign(is_eligible=True)
    benchmark_frame = pd.DataFrame([{"date": day, "close": row["close"]} for day, row in benchmark.items()])
    calendar = _DemoCalendar(dates)
    results: dict[str, Any] = {}
    manifest_base = {
        "dataset_id": "engineering-demo", "dataset_content_hash": _sha({"dates": dates, "codes": CODES}),
        "calendar_content_hash": calendar.content_hash, "signal_content_hash": _sha(signals_by_spec),
        "eligibility_content_hash": _sha(eligibility.to_dict(orient="records")), "corporate_action_content_hash": _sha([]),
        "benchmark_id": "synthetic-hs300", "benchmark_content_hash": _sha(benchmark_frame.to_dict(orient="records")),
        "training_artifact_id": "demo-training", "validation_artifact_id": "demo-validation",
        "training_artifact_hash": _sha("demo-training"), "validation_artifact_hash": _sha("demo-validation"),
    }
    for spec in specs:
        scenario_metrics = {}
        baseline_daily = None
        for scenario in ("baseline", "stress"):
            bundle = ManualDailyFactorBundleV2(
                strategy_key="engineering-demo", factor_name=spec.name, factor_expression_hash=spec.expression_hash,
                label_spec_hash=_sha("manual-daily-label-v1"), training_evidence_hash=_sha("demo-training"), validation_evidence_hash=_sha("demo-validation"),
                dataset_content_hash=manifest_base["dataset_content_hash"], cost_scenario=scenario, top_n=2, code_hash=_sha(VERSION),
            )
            manifest_payload = {**manifest_base, "signal_content_hash": _sha(signals_by_spec[spec.name]), "source_files": tuple({"role": role, "path": f"{role}.synthetic"} for role in ("daily", "actions", "securities", "calendar", "benchmark", "signals"))}
            manifest = ManualPortfolioInputManifest(**manifest_payload)
            result = run_manual_daily_portfolio_v3(
                daily=daily, eligibility=eligibility, benchmark=benchmark_frame, signals=signals_by_spec[spec.name], corporate_actions=[],
                calendar=calendar, bundle=bundle, input_manifest=manifest, start=date.fromisoformat(dates[0]), end=date.fromisoformat(dates[-3]),
                initial_capital=Decimal(1000000),
            )
            metrics = calculate_portfolio_metrics(result)
            scenario_metrics[scenario] = metrics
            baseline_daily = result.daily if scenario == "baseline" else baseline_daily
        results[spec.name] = {"metrics": scenario_metrics, "bundle": bundle.as_dict(), "daily": baseline_daily or []}
    first = specs[0].name
    baseline = results[first]["metrics"]["baseline"]
    shanghai_levels: dict[str, Decimal] = {}
    shanghai = Decimal(1)
    for index, day in enumerate(dates):
        if index:
            shanghai *= 1 + Decimal(((index * 3) % 5) - 2) / Decimal(1000)
        shanghai_levels[day] = shanghai
    curves = [{"date": row["date"], "strategy": str(Decimal(row["equity"]) / Decimal(1000000)), "hs300": str(next(item["nav"] for item in _benchmark_rows_for_demo(benchmark, dates) if item["date"] == row["date"])), "shanghai": str(shanghai_levels[row["date"]])} for row in results[first]["daily"]]
    return {"factor_name": first, "candidates": [{"name": spec.name, "expression_hash": spec.expression_hash} for spec in specs], "curves": curves, "metrics": {"total_return": baseline["total_return"], "excess_return": baseline["excess_return"], "max_drawdown": baseline["max_drawdown_magnitude"], "sharpe": None if baseline["sharpe_252_rf0"] == "null" else baseline["sharpe_252_rf0"], "trade_count": baseline["trade_count"]}, "scenarios": results, "promotion_eligible": False}, signals_by_spec[first]


def _benchmark_rows_for_demo(benchmark: dict[str, dict[str, str]], dates: list[str]) -> list[dict[str, str]]:
    origin = Decimal(benchmark[dates[0]]["close"])
    return [{"date": day, "nav": str(Decimal(benchmark[day]["close"]) / origin)} for day in dates]


def create_demo_state(seed: int = 7) -> dict[str, Any]:
    """Create a fresh deterministic engineering demonstration state."""
    dates, bars, benchmark, benchmark_rows = _market(int(seed))
    specs = _specs()
    candidates = [{"name": spec.name, "expression_hash": spec.expression_hash, "lookback": spec.lookback, "direction": spec.direction} for spec in specs]
    bundle = ManualDailyFactorBundleV2(
        strategy_key="engineering-demo", factor_name=candidates[0]["name"], factor_expression_hash=candidates[0]["expression_hash"],
        label_spec_hash=_sha("manual-daily-label-v1"), training_evidence_hash=_sha("demo-training"), validation_evidence_hash=_sha("demo-validation"),
        dataset_content_hash=_sha({"seed": seed, "dates": dates}), top_n=2, code_hash=_sha(VERSION),
    )
    state = {
        "protocol_version": VERSION, "mode": "engineering_demo", "stage": "created", "next_action": "research", "current_date": dates[0], "day_index": 0,
        "seed": int(seed), "capital": "1000000.00", "cash": "1000000.00", "equity": "1000000.00", "positions": [], "cohorts": [],
        "decision": {}, "plans": [], "fills": [], "reviews": [], "nav": [], "events": [],
        "research": {"status": "pending", "metrics": {"total_return": None, "max_drawdown": None, "sharpe": None, "trade_count": 0}, "curves": [], "candidates": candidates, "promotion_eligible": False, "bundle": bundle.as_dict()},
        "observation": {"status": "pending", "engineering_completed": False, "eligible_for_real": False, "days": []},
        "scenario": "rebalance", "scenario_schedule": {dates[4]: "hold", dates[6]: "blocked", dates[8]: "flat"},
        "market": {"dates": dates, "bars": bars, "benchmark": benchmark, "benchmark_rows": benchmark_rows, "signals": {}},
    }
    return _json(state)


def _ledger(state: dict[str, Any]) -> ManualResearchLedger:
    ledger = ManualResearchLedger(Decimal(state["capital"]), cost_scenario="baseline")
    dates = state["market"]["dates"]
    bars = state["market"]["bars"]
    for fill in state["fills"]:
        if fill.get("status") not in {"filled", "partially_filled"} or int(fill.get("filled_quantity", 0)) <= 0:
            continue
        day_index = dates.index(fill["trade_date"]); code = fill["code"]
        intent = ResearchExecutionIntent(fill["intent_id"], date.fromisoformat(fill["trade_date"]), fill["phase"], fill["cohort_id"], code, fill["side"], int(fill["filled_quantity"]), Decimal(fill["reference_price"]), fill["reason"])
        ledger.execute_intent(intent, bar=bars[code][fill["trade_date"]], previous_bar=bars[code][dates[max(0, day_index - 1)]], historically_eligible=True)
    return ledger


def _plan(state: dict[str, Any]) -> dict[str, Any]:
    dates, bars, signals = state["market"]["dates"], state["market"]["bars"], state["market"]["signals"]
    index = int(state["day_index"]); day = dates[index]; signal_day = dates[index - 1] if index else dates[0]
    scenario = state.get("scenario_schedule", {}).get(day, state.get("scenario", "rebalance"))
    ledger = _ledger(state)
    close_items = []
    for cohort in state["cohorts"]:
        if cohort.get("status") in {"open", "exiting"} and date.fromisoformat(cohort["exit_date"]) <= date.fromisoformat(day):
            for (cohort_id, code), lot in sorted(ledger.lots.items()):
                if cohort_id == cohort["id"]:
                    close_items.append({"phase": "close", "trade_date": day, "signal_date": cohort["signal_date"], "cohort_id": cohort_id, "code": code, "name": code, "side": "sell", "planned_quantity": lot.quantity, "reference_price": bars[code][signal_day]["close"], "reason": "cohort_exit", "status": "planned"})
    open_items = []
    if scenario not in {"hold", "reduce", "flat", "blocked"} and sum(item.get("status") in {"planned", "open"} for item in state["cohorts"]) < 2:
        cohort_id = f"cohort-{signal_day}"
        selected = [code for code, _score in sorted(signals.get(signal_day, {}).items(), key=lambda item: (-item[1], item[0]))][:2]
        for code in selected:
            signal_close = bars[code][signal_day]["close"]
            quantity = int(Decimal(state["equity"]) * Decimal("0.45") / max(1, len(selected)) / Decimal(signal_close)) // LOT * LOT
            open_items.append({"phase": "open", "trade_date": day, "signal_date": signal_day, "cohort_id": cohort_id, "code": code, "name": code, "side": "buy", "planned_quantity": quantity, "reference_price": signal_close, "reason": "cohort_entry", "status": "blocked" if scenario == "blocked" else "planned"})
        state["cohorts"].append({"id": cohort_id, "signal_date": signal_day, "entry_date": day, "exit_date": dates[min(index + 1, len(dates) - 1)], "status": "planned", "scenario": scenario})
    action = scenario if scenario in {"hold", "rebalance", "reduce", "flat", "blocked"} else "rebalance"
    state["decision"] = {"signal_date": signal_day, "action": action, "reasons": ["synthetic_engineering"] + (["data_health_blocked"] if scenario == "blocked" else [])}
    state["plans"] = [
        {"id": f"close-{day}", "phase": "close", "status": "planned", "items": close_items},
        {"id": f"open-{day}", "phase": "open", "status": "planned", "items": open_items},
    ]
    return state


def _fill(state: dict[str, Any], fill_mode: str) -> dict[str, Any]:
    if fill_mode not in {"full", "partial", "unfilled"}:
        raise ValueError("unsupported_fill_mode")
    ledger = _ledger(state); dates, bars = state["market"]["dates"], state["market"]["bars"]; day = dates[int(state["day_index"])]
    fills = []
    for plan in sorted(state["plans"], key=lambda item: 0 if item["phase"] == "open" else 1):
      for item in plan["items"]:
        requested = int(item["planned_quantity"])
        quantity = 0 if item["status"] == "blocked" else (requested if fill_mode == "full" else (requested // 2 // LOT * LOT if fill_mode == "partial" else 0))
        fill = {"intent_id": f"{item['phase']}:{day}:{item['cohort_id']}:{item['code']}", "trade_date": day, "phase": item["phase"], "cohort_id": item["cohort_id"], "code": item["code"], "side": item["side"], "requested_quantity": requested, "filled_quantity": quantity, "reference_price": str(bars[item["code"]][day]["open" if item["phase"] == "open" else "close"]), "reason": item["reason"], "status": "unfilled" if quantity == 0 else ("filled" if quantity == requested else "partially_filled"), "unfilled_reason": "data_health_blocked" if item["status"] == "blocked" else ("operator_unfilled" if quantity == 0 else None)}
        if quantity:
            intent = ResearchExecutionIntent(fill["intent_id"], date.fromisoformat(day), item["phase"], item["cohort_id"], item["code"], item["side"], quantity, Decimal(fill["reference_price"]), item["reason"])
            attempt = ledger.execute_intent(intent, bar=bars[item["code"]][day], previous_bar=bars[item["code"]][dates[max(0, dates.index(day) - 1)]], historically_eligible=True)
            fill["filled_quantity"] = int(attempt.get("filled_quantity", 0)); fill["status"] = "filled" if fill["filled_quantity"] == requested else "partially_filled"
            trade = ledger.trades[-1] if ledger.trades else {}
            fill["actual_price"] = trade.get("price"); fill["fees"] = trade.get("total_fee"); fill["gross"] = trade.get("gross")
        for cohort in state["cohorts"]:
            if cohort["id"] != item["cohort_id"] or not fill["filled_quantity"]:
                continue
            if item["phase"] == "open":
                cohort["status"] = "open"
            elif fill["status"] != "filled":
                cohort["status"] = "exiting"
        fills.append(fill)
    for cohort in state["cohorts"]:
        if cohort.get("status") in {"open", "exiting", "closed"}:
            has_lot = any(key[0] == cohort["id"] for key in ledger.lots)
            if cohort.get("exit_date") and date.fromisoformat(cohort["exit_date"]) <= date.fromisoformat(day):
                cohort["status"] = "exiting" if has_lot else "closed"
        elif cohort.get("status") == "planned" and not any(item.get("cohort_id") == cohort["id"] and item.get("filled_quantity", 0) > 0 for item in fills):
            cohort["status"] = "entry_failed"
    state["fills"].extend(fills)
    state["stage"] = "review"; state["events"].append({"type": "fills_recorded", "date": day, "fill_mode": fill_mode})
    return _refresh_account(state)


def _refresh_account(state: dict[str, Any]) -> dict[str, Any]:
    ledger = _ledger(state); day = state["market"]["dates"][int(state["day_index"])]
    mark = ledger.mark_close(date.fromisoformat(day), {code: state["market"]["bars"][code][day] for code in CODES})
    state["cash"] = mark["cash"]; state["equity"] = mark["equity"]
    state["positions"] = [{"cohort_id": key[0], "code": key[1], "quantity": lot.quantity, "avg_cost": lot.avg_cost} for key, lot in sorted(ledger.lots.items())]
    return _json(state)


def advance_demo_state(state: dict[str, Any], action: str, fill_mode: str = "full") -> dict[str, Any]:
    """Advance one strict runtime action and return a new JSON-compatible state."""
    current = deepcopy(state)
    if current.get("mode") != "engineering_demo":
        raise ValueError("engineering_demo_state_required")
    stage = current.get("stage")
    allowed = {"created": "research", "researched": "observe", "observed": "plan", "planned": "confirm", "confirmed": "fill", "filled": "review", "reviewed": "next_day"}
    if allowed.get(stage) != action:
        raise ValueError(f"invalid_demo_action:{stage}:{action}")
    if action == "research":
        specs = _specs()
        research, signals = _run_research(current["market"]["dates"], current["market"]["bars"], current["market"]["benchmark"], specs)
        current["market"]["signals"] = signals
        current["research"].update(research)
        current["research"]["status"] = "completed"
        current["stage"] = "researched"; current["next_action"] = "observe"; current["events"].append({"type": "research_completed"})
    elif action == "observe":
        daily = current["research"].get("scenarios", {}).get(current["research"].get("factor_name"), {}).get("daily", [])
        days = []
        for row in daily[:30]:
            cash = Decimal(row.get("cash", "0")); market = Decimal(row.get("market_value", "0")); equity = Decimal(row.get("equity", "0"))
            days.append(dict(row, mode="synthetic_engineering", reconciled=_money(cash + market) == equity))
        reconciled_days = sum(bool(row.get("reconciled")) for row in days)
        current["observation"] = {"status": "engineering_completed", "engineering_completed": True, "eligible_for_real": False, "days": days, "reviews": days, "nav": days, "reconciled_days": reconciled_days, "completed_days": len(days)}
        first_signal = next((index for index, day in enumerate(current["market"]["dates"]) if current["market"]["signals"].get(day)), 0)
        execution_index = min(first_signal + 1, len(current["market"]["dates"]) - 1)
        current["day_index"] = execution_index; current["current_date"] = current["market"]["dates"][execution_index]; current["stage"] = "observed"; current["next_action"] = "plan"
    elif action == "plan":
        current = _plan(current); current["stage"] = "planned"; current["next_action"] = "confirm"
    elif action == "confirm":
        current["plans"] = [{**plan, "status": "confirmed", "items": [{**item, "status": "confirmed" if item["status"] == "planned" else item["status"]} for item in plan["items"]]} for plan in current["plans"]]; current["stage"] = "confirmed"; current["next_action"] = "fill"
    elif action == "fill":
        current = _fill(current, fill_mode); current["stage"] = "filled"; current["next_action"] = "review"
    elif action == "review":
        ledger = _ledger(current); day = current["market"]["dates"][int(current["day_index"])]
        mark = ledger.mark_close(date.fromisoformat(day), {code: current["market"]["bars"][code][day] for code in CODES})
        position_value = sum((Decimal(item["market_value"]) for item in ledger.positions if item["date"] == day), ZERO)
        reconciled = _money(ledger.cash + position_value) == Decimal(mark["equity"])
        previous_equity = Decimal(current["reviews"][-1]["equity"]) if current["reviews"] else Decimal(current["capital"])
        daily_return = Decimal(mark["equity"]) / previous_equity - 1 if previous_equity else ZERO
        review = {**mark, "daily_return": str(daily_return), "status": "matched" if reconciled else "blocked", "data_health": "synthetic_engineering", "reconciliation": {"cash": str(ledger.cash), "receivable_cash": mark.get("receivable_cash", "0"), "position_value": str(position_value), "cash_plus_receivable_plus_position": str(_money(ledger.cash + Decimal(mark.get("receivable_cash", "0")) + position_value)), "equity": mark["equity"], "matched": reconciled}}
        curve = next((item for item in current["research"].get("curves", []) if item["date"] == day), {})
        nav = {"date": day, "strategy": str(Decimal(mark["equity"]) / Decimal(current["capital"])), "hs300": curve.get("hs300"), "shanghai": curve.get("shanghai"), "cash": mark["cash"], "equity": mark["equity"], "daily_return": str(daily_return)}
        current["reviews"].append(review); current["nav"].append(nav); current["stage"] = "reviewed"; current["next_action"] = "next_day"
    elif action == "next_day":
        next_index = int(current["day_index"]) + 1
        if next_index >= len(current["market"]["dates"]):
            current["stage"] = "completed"; current["next_action"] = None
        else:
            current["day_index"] = next_index; current["current_date"] = current["market"]["dates"][next_index]; current = _plan(current); current["stage"] = "planned"; current["next_action"] = "confirm"
    return _json(current)


def replay_daily_cycle(history: dict[str, Any] | list[dict[str, Any]], policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Replay a serializable demo history without importing a web or DB layer.

    ``history`` may be an existing state or an action list.  The replay always
    starts from the same deterministic seed and applies the authoritative
    action order, making this the seam a real-forward receipt adapter can reuse.
    """
    if isinstance(history, dict) and history.get("mode") == "engineering_demo":
        raise ValueError("opaque_state_replay_requires_action_history")
    actions = history if isinstance(history, list) else list((history or {}).get("actions", []))
    state = create_demo_state(int((policy or {}).get("seed", 7)))
    for item in actions:
        action = item if isinstance(item, str) else item.get("action")
        fill_mode = "full" if isinstance(item, str) else item.get("fill_mode", "full")
        state = advance_demo_state(state, action, fill_mode)
    return state


__all__ = ["VERSION", "advance_demo_state", "create_demo_state", "replay_daily_cycle"]
