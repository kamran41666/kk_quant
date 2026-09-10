"""Independent metrics, replay and atomic evidence writer for portfolio v3."""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import statistics
import tempfile
from collections import defaultdict
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

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


def _allocate_source_cash(
    entitlements: Mapping[str, int],
    cash_per_share: Decimal,
    tax_rate: Decimal,
) -> dict[str, Decimal]:
    """Recompute cohort cash independently from the production ledger."""
    exact = {
        cohort_id: Decimal(quantity) * cash_per_share * (Decimal(1) - tax_rate)
        for cohort_id, quantity in entitlements.items()
    }
    total = sum(exact.values(), ZERO).quantize(CENT, rounding=ROUND_HALF_UP)
    allocated = {
        cohort_id: value.quantize(CENT, rounding=ROUND_DOWN)
        for cohort_id, value in exact.items()
    }
    cents = int(((total - sum(allocated.values(), ZERO)) / CENT).to_integral_value())
    priority = sorted(
        exact,
        key=lambda cohort_id: (-(exact[cohort_id] - allocated[cohort_id]), cohort_id),
    )
    for cohort_id in priority[:cents]:
        allocated[cohort_id] += CENT
    return allocated


def _allocate_source_bonus(
    entitlements: Mapping[str, int],
    bonus_ratio: Decimal,
) -> dict[str, int]:
    """Recompute account-level bonus shares with largest remainders."""
    exact = {
        cohort_id: Decimal(quantity) * bonus_ratio
        for cohort_id, quantity in entitlements.items()
    }
    total = int(sum(exact.values(), ZERO).to_integral_value(rounding=ROUND_DOWN))
    allocated = {
        cohort_id: int(value.to_integral_value(rounding=ROUND_DOWN))
        for cohort_id, value in exact.items()
    }
    remaining = total - sum(allocated.values())
    priority = sorted(
        exact,
        key=lambda cohort_id: (-(exact[cohort_id] - allocated[cohort_id]), cohort_id),
    )
    for cohort_id in priority[:remaining]:
        allocated[cohort_id] += 1
    return allocated


def _consume_unlocked(
    acquisitions: list[dict[str, Any]],
    quantity: int,
    day: date,
) -> bool:
    """Consume only source-reconstructed shares that are sellable on ``day``."""
    remaining = quantity
    for item in acquisitions:
        if item["unlock_date"] > day or item["quantity"] <= 0:
            continue
        consumed = min(remaining, item["quantity"])
        item["quantity"] -= consumed
        remaining -= consumed
        if remaining == 0:
            return True
    return remaining == 0


def _event_signature(row: Mapping[str, Any]) -> tuple[Any, ...]:
    stage = row.get("stage")
    base = (
        row.get("action_id"), stage, row.get("date"), row.get("code"),
        row.get("source_hash"), row.get("action_hash"), row.get("economic_key"),
    )
    if stage == "record":
        return (*base, row.get("cohort_id"), int(row.get("eligible_quantity", 0)))
    if stage == "ex_cash":
        return (
            *base, row.get("cohort_id"), int(row.get("eligible_quantity", 0)),
            _money(row.get("gross_cash", 0)), _money(row.get("receivable_cash", 0)),
        )
    if stage == "ex_bonus":
        return (
            *base, row.get("cohort_id"), int(row.get("eligible_quantity", 0)),
            int(row.get("bonus_quantity", 0)), row.get("stock_listing_date"),
        )
    if stage == "pay":
        return (*base, row.get("cohort_id"), _money(row.get("cash", 0)))
    if stage == "stock_listing":
        return base
    raise ValueError(f"unsupported_corporate_action_stage:{stage}")


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


def replay_portfolio_result(result: Any, sources: Any | None = None) -> dict[str, Any]:
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

    source_checks = {
        "source_files": False, "calendar_source": False, "benchmark_source": False,
        "signal_source": False, "execution_price_source": False,
        "valuation_price_source": False,
        "capacity_source": False, "eligibility_source": False,
        "corporate_action_source": False, "corporate_action_economics_source": False,
        "share_lock_source": False, "cohort_terminal_source": False,
        "t_plus_one_source": False,
    }
    target_completion_verifiable = False
    if sources is not None:
        try:
            fresh = sources.reload()
            source_checks["source_files"] = sources.input_manifest.manifest_hash == result.input_manifest.manifest_hash
            daily_dates = [date.fromisoformat(row["date"]) for row in result.daily]
            expected_dates = [day for day in fresh["calendar"] if result.start_date <= day <= result.end_date]
            source_checks["calendar_source"] = daily_dates == expected_dates
            daily_source = {
                (pd.Timestamp(row["date"]).date(), str(row["code"]).upper()): row
                for row in fresh["daily"].to_dict("records")
            }
            eligibility_source = {
                (pd.Timestamp(row["date"]).date(), str(row["code"]).upper()): bool(row["is_eligible"])
                for row in fresh["eligibility"].to_dict("records")
            }
            benchmark_values = {
                pd.Timestamp(row["date"]).date(): _decimal(row["close"], "benchmark_close")
                for row in fresh["benchmark"].to_dict("records")
            }
            benchmark_ok = bool(expected_dates)
            if benchmark_ok:
                origin = benchmark_values.get(expected_dates[0])
                prior = origin
                for recorded, day in zip(result.benchmark, expected_dates):
                    close = benchmark_values.get(day)
                    if close is None or origin is None or prior is None:
                        benchmark_ok = False
                        break
                    if (
                        recorded["date"] != day.isoformat()
                        or _decimal(recorded["close"], "benchmark_close") != close
                        or _decimal(recorded["nav"], "benchmark_nav") != close / origin
                        or _decimal(recorded["daily_return"], "benchmark_return") != close / prior - Decimal("1")
                    ):
                        benchmark_ok = False
                        break
                    prior = close
            source_checks["benchmark_source"] = benchmark_ok and len(result.benchmark) == len(expected_dates)

            expected_signals = {
                (day.isoformat(), code): _decimal(score, "source_signal")
                for day, values in fresh["signals"].items()
                if result.start_date <= day <= result.end_date
                for code, score in values.items()
            }
            recorded_signals = {
                (row["signal_date"], row["code"]): _decimal(row["score"], "recorded_signal")
                for row in result.signals
            }
            source_checks["signal_source"] = expected_signals == recorded_signals

            day_index = {day: index for index, day in enumerate(fresh["calendar"])}
            costs = RESEARCH_EXECUTION_COSTS[result.bundle.cost_scenario]
            price_ok = capacity_ok = eligibility_ok = t1_ok = True
            buy_dates: dict[tuple[str, str], list[date]] = defaultdict(list)
            attempt_by_intent = {row["intent_id"]: row for row in result.attempts}
            for trade in sorted(result.trades, key=lambda row: int(row["event_sequence"])):
                day = date.fromisoformat(trade["date"])
                bar = daily_source.get((day, trade["code"]))
                if bar is None or bool(bar.get("is_suspended", True)):
                    price_ok = False
                    continue
                field = "open" if trade["phase"] == "open" else "close"
                direction = Decimal("1") if trade["side"] == "buy" else Decimal("-1")
                expected_price = (
                    _decimal(bar[field], field) * (Decimal("1") + direction * costs["slippage_rate"])
                ).quantize(CENT, rounding=ROUND_HALF_UP)
                if expected_price != _decimal(trade["price"], "trade_price"):
                    price_ok = False
                if trade["side"] == "buy":
                    if not eligibility_source.get((day, trade["code"]), False):
                        eligibility_ok = False
                    buy_dates[(trade["cohort_id"], trade["code"])].append(day)
                elif not any(buy_day < day for buy_day in buy_dates[(trade["cohort_id"], trade["code"])]):
                    # Bonus shares are handled by the corporate-action check;
                    # a normal exit still needs an earlier acquisition.
                    t1_ok = False
                attempt = attempt_by_intent.get(trade["intent_id"])
                previous_index = day_index.get(day, 0) - 1
                previous_day = fresh["calendar"][previous_index] if previous_index >= 0 else None
                previous_bar = daily_source.get((previous_day, trade["code"])) if previous_day else None
                expected_capacity = int(
                    (_decimal((previous_bar or {}).get("volume", 0), "previous_volume") * costs["participation_rate"])
                    .to_integral_value(rounding=ROUND_DOWN)
                )
                if attempt is None or int(attempt["capacity_total"]) != expected_capacity:
                    capacity_ok = False
            source_checks["execution_price_source"] = price_ok
            source_checks["capacity_source"] = capacity_ok
            source_checks["eligibility_source"] = eligibility_ok
            source_checks["t_plus_one_source"] = t1_ok

            source_actions = {item.action_id: item for item in fresh["actions"]}
            definition_rows = [
                row for row in result.corporate_actions if row["stage"] == "definition"
            ]
            definitions = {row["action_id"]: row for row in definition_rows}
            action_ok = (
                len(definition_rows) == len(definitions)
                and set(source_actions) == set(definitions)
            )
            source_calendar = set(fresh["calendar"])
            source_calendar_start = fresh["calendar"][0] if fresh["calendar"] else None
            source_calendar_end = fresh["calendar"][-1] if fresh["calendar"] else None
            for action_id, action in source_actions.items():
                row = definitions.get(action_id, {})
                if (
                    row.get("action_hash") != action.action_hash
                    or row.get("economic_key") != action.economic_key
                    or row.get("source_hash") != action.source_hash
                    or row.get("stage") != "definition"
                    or row.get("code") != action.code
                    or row.get("cohort_id") is not None
                    or row.get("date")
                    != (action.ex_date or action.record_date or result.start_date).isoformat()
                    or row.get("record_date")
                    != (action.record_date.isoformat() if action.record_date else None)
                    or row.get("ex_date")
                    != (action.ex_date.isoformat() if action.ex_date else None)
                    or row.get("pay_date")
                    != (action.pay_date.isoformat() if action.pay_date else None)
                    or row.get("stock_listing_date")
                    != (
                        action.stock_listing_date.isoformat()
                        if action.stock_listing_date else None
                    )
                    or _decimal(row.get("cash_per_share"), "definition_cash_per_share")
                    != action.cash_per_share
                    or _decimal(row.get("bonus_ratio"), "definition_bonus_ratio")
                    != action.bonus_ratio
                    or not isinstance(row.get("allocation_verified"), bool)
                    or row.get("allocation_verified") != action.allocation_verified
                ):
                    action_ok = False
                for event_day in (action.record_date, action.ex_date):
                    if (
                        event_day is not None
                        and source_calendar_start is not None
                        and source_calendar_start <= event_day <= source_calendar_end
                        and event_day not in source_calendar
                    ):
                        action_ok = False
            source_checks["corporate_action_source"] = action_ok

            # Rebuild company-action economics and sellability from the
            # verified action source and immutable trades.  The production
            # ledger's allocation and lot methods are deliberately not used.
            expected_action_events: list[dict[str, Any]] = []
            entitlements_by_action: dict[str, dict[str, int]] = {}
            pending_cash: list[dict[str, Any]] = []
            applied_actions: set[str] = set()
            listed_actions: set[str] = set()
            acquisitions: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
            source_trades_by_day: dict[date, list[Mapping[str, Any]]] = defaultdict(list)
            for trade in result.trades:
                source_trades_by_day[date.fromisoformat(trade["date"])].append(trade)
            for rows in source_trades_by_day.values():
                rows.sort(key=lambda row: int(row["event_sequence"]))
            record_actions_by_day: dict[date, list[Any]] = defaultdict(list)
            ex_actions_by_day: dict[date, list[Any]] = defaultdict(list)
            for action in source_actions.values():
                if action.record_date is not None:
                    record_actions_by_day[action.record_date].append(action)
                if action.ex_date is not None:
                    ex_actions_by_day[action.ex_date].append(action)
            for rows in record_actions_by_day.values():
                rows.sort(key=lambda action: action.action_id)
            for rows in ex_actions_by_day.values():
                rows.sort(key=lambda action: action.action_id)

            lock_ok = True
            valuation_price_ok = True
            for day in expected_dates:
                for action in ex_actions_by_day.get(day, []):
                    entitlements = entitlements_by_action.get(action.action_id, {})
                    applied_actions.add(action.action_id)
                    identity = {
                        "source_hash": action.source_hash,
                        "action_hash": action.action_hash,
                        "economic_key": action.economic_key,
                    }
                    if action.cash_per_share > ZERO:
                        allocation = _allocate_source_cash(
                            entitlements,
                            action.cash_per_share,
                            costs["dividend_tax_rate"],
                        )
                        effective_pay_date = (
                            action.pay_date
                            if action.pay_date is not None and action.pay_date >= day
                            else None
                        )
                        for cohort_id, quantity in sorted(entitlements.items()):
                            gross = _money(Decimal(quantity) * action.cash_per_share)
                            net = allocation[cohort_id]
                            expected_action_events.append({
                                **identity,
                                "action_id": action.action_id,
                                "date": day.isoformat(),
                                "stage": "ex_cash",
                                "code": action.code,
                                "cohort_id": cohort_id,
                                "eligible_quantity": quantity,
                                "gross_cash": gross,
                                "receivable_cash": net,
                            })
                            pending_cash.append({
                                "action": action,
                                "cohort_id": cohort_id,
                                "amount": net,
                                "pay_date": effective_pay_date,
                                "paid": False,
                            })
                    if action.bonus_ratio > ZERO and action.allocation_verified:
                        allocation = _allocate_source_bonus(entitlements, action.bonus_ratio)
                        unlock_date = (
                            action.stock_listing_date
                            if action.stock_listing_date is not None
                            and action.stock_listing_date >= day
                            else date.max
                        )
                        for cohort_id, bonus_quantity in sorted(allocation.items()):
                            if bonus_quantity <= 0:
                                continue
                            acquisitions[(cohort_id, action.code)].append({
                                "quantity": bonus_quantity,
                                "unlock_date": unlock_date,
                            })
                            expected_action_events.append({
                                **identity,
                                "action_id": action.action_id,
                                "date": day.isoformat(),
                                "stage": "ex_bonus",
                                "code": action.code,
                                "cohort_id": cohort_id,
                                "eligible_quantity": entitlements[cohort_id],
                                "bonus_quantity": bonus_quantity,
                                "stock_listing_date": unlock_date.isoformat(),
                            })

                for receivable_row in pending_cash:
                    pay_date = receivable_row["pay_date"]
                    if receivable_row["paid"] or pay_date is None or pay_date > day:
                        continue
                    receivable_row["paid"] = True
                    action = receivable_row["action"]
                    expected_action_events.append({
                        "source_hash": action.source_hash,
                        "action_hash": action.action_hash,
                        "economic_key": action.economic_key,
                        "action_id": action.action_id,
                        "date": day.isoformat(),
                        "stage": "pay",
                        "code": action.code,
                        "cohort_id": receivable_row["cohort_id"],
                        "cash": receivable_row["amount"],
                    })

                for action in sorted(source_actions.values(), key=lambda item: item.action_id):
                    listing_valid = (
                        action.stock_listing_date is not None
                        and action.ex_date is not None
                        and action.stock_listing_date >= action.ex_date
                    )
                    if (
                        listing_valid
                        and action.stock_listing_date <= day
                        and action.action_id in applied_actions
                        and action.action_id not in listed_actions
                    ):
                        listed_actions.add(action.action_id)
                        expected_action_events.append({
                            "source_hash": action.source_hash,
                            "action_hash": action.action_hash,
                            "economic_key": action.economic_key,
                            "action_id": action.action_id,
                            "date": day.isoformat(),
                            "stage": "stock_listing",
                            "code": action.code,
                        })

                for trade in source_trades_by_day.get(day, []):
                    key = (trade["cohort_id"], trade["code"])
                    lots = acquisitions[key]
                    quantity = int(trade["quantity"])
                    if trade["side"] == "buy":
                        calendar_index = day_index[day]
                        unlock_date = (
                            fresh["calendar"][calendar_index + 1]
                            if calendar_index + 1 < len(fresh["calendar"])
                            else date.max
                        )
                        lots.append({"quantity": quantity, "unlock_date": unlock_date})
                    else:
                        sellable_before = sum(
                            int(item["quantity"])
                            for item in lots
                            if item["unlock_date"] <= day
                        )
                        if quantity > sellable_before or not _consume_unlocked(lots, quantity, day):
                            lock_ok = False
                            fail(
                                "share_lock_source",
                                date=day.isoformat(),
                                trade_id=trade["trade_id"],
                                expected_sellable=sellable_before,
                                sold_quantity=quantity,
                            )

                expected_positions: dict[tuple[str, str], int] = {}
                expected_sellable: dict[tuple[str, str], int] = {}
                for key, lots in acquisitions.items():
                    quantity = sum(int(item["quantity"]) for item in lots)
                    if quantity <= 0:
                        continue
                    expected_positions[key] = quantity
                    expected_sellable[key] = sum(
                        int(item["quantity"])
                        for item in lots
                        if item["unlock_date"] <= day
                    )
                recorded_position_rows = positions_by_day[day.isoformat()]
                recorded_positions = {
                    (row["cohort_id"], row["code"]): row
                    for row in recorded_position_rows
                }
                if len(recorded_positions) != len(recorded_position_rows):
                    lock_ok = False
                    fail("share_lock_source", date=day.isoformat(), reason="duplicate_position")
                if {
                    key: int(row["quantity"])
                    for key, row in recorded_positions.items()
                } != expected_positions:
                    lock_ok = False
                    fail("share_lock_source", date=day.isoformat(), reason="quantity_mismatch")
                for key, row in recorded_positions.items():
                    if int(row["sellable_quantity"]) != expected_sellable.get(key, 0):
                        lock_ok = False
                        fail(
                            "share_lock_source",
                            date=day.isoformat(),
                            cohort_id=key[0],
                            code=key[1],
                            expected_sellable=expected_sellable.get(key, 0),
                            actual_sellable=int(row["sellable_quantity"]),
                        )
                    bar = daily_source.get((day, key[1]))
                    if (
                        bar is None
                        or _decimal(row["close"], "position_close")
                        != _money(_decimal(bar["close"], "source_close"))
                    ):
                        valuation_price_ok = False
                        fail(
                            "valuation_price_source",
                            date=day.isoformat(),
                            cohort_id=key[0],
                            code=key[1],
                        )

                for action in record_actions_by_day.get(day, []):
                    entitlements = {
                        cohort_id: quantity
                        for (cohort_id, code), quantity in expected_positions.items()
                        if code == action.code and quantity > 0
                    }
                    entitlements_by_action[action.action_id] = entitlements
                    for cohort_id, quantity in sorted(entitlements.items()):
                        expected_action_events.append({
                            "source_hash": action.source_hash,
                            "action_hash": action.action_hash,
                            "economic_key": action.economic_key,
                            "action_id": action.action_id,
                            "date": day.isoformat(),
                            "stage": "record",
                            "code": action.code,
                            "cohort_id": cohort_id,
                            "eligible_quantity": quantity,
                        })

            actual_economic_rows = [
                row for row in result.corporate_actions
                if row["stage"] != "definition"
            ]
            expected_signatures = sorted(repr(_event_signature(row)) for row in expected_action_events)
            actual_signatures = sorted(repr(_event_signature(row)) for row in actual_economic_rows)
            action_economics_ok = expected_signatures == actual_signatures
            if not action_economics_ok:
                fail(
                    "corporate_action_economics_source",
                    expected_count=len(expected_signatures),
                    actual_count=len(actual_signatures),
                    expected_hash=_hash(expected_signatures),
                    actual_hash=_hash(actual_signatures),
                )
            source_checks["valuation_price_source"] = valuation_price_ok
            source_checks["corporate_action_economics_source"] = action_economics_ok
            source_checks["share_lock_source"] = lock_ok

            cohort_status = {cohort["id"]: cohort["status"] for cohort in result.cohorts}
            terminal_ok = True
            for (cohort_id, code), lots in acquisitions.items():
                remaining = sum(int(item["quantity"]) for item in lots)
                if remaining <= 0:
                    continue
                if cohort_status.get(cohort_id) in {None, "closed", "entry_failed"}:
                    terminal_ok = False
                    fail(
                        "cohort_terminal_source",
                        cohort_id=cohort_id,
                        code=code,
                        remaining_quantity=remaining,
                        status=cohort_status.get(cohort_id),
                    )
            source_checks["cohort_terminal_source"] = terminal_ok

            logical_rows: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
            for row in result.intents:
                logical_rows[str(row.get("logical_intent_id") or row["intent_id"])].append(row)
            target_ok = True
            for cohort in result.cohorts:
                signal_day = date.fromisoformat(cohort["signal_date"])
                ranked = sorted(fresh["signals"].get(signal_day, {}).items(), key=lambda item: (-_decimal(item[1], "signal"), item[0]))[:result.bundle.top_n]
                if not ranked:
                    continue
                budget = _decimal(cohort["budget"], "cohort_budget") / len(ranked)
                entry_day = date.fromisoformat(cohort["entry_date"])
                for code, _score in ranked:
                    bar = daily_source.get((entry_day, code), {})
                    raw = bar.get("open")
                    if raw is None or _decimal(raw, "open") <= 0:
                        target_ok = False
                        continue
                    requested = int((budget / _decimal(raw, "open")).to_integral_value(rounding=ROUND_DOWN)) // 100 * 100
                    if requested > 0 and f"{cohort['id']}:entry:{code}" not in logical_rows:
                        target_ok = False
            target_completion_verifiable = target_ok
        except Exception as exc:
            fail("source_replay_exception", error=f"{type(exc).__name__}:{exc}")

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
        **source_checks,
    }
    accounting_keys = {
        "intent_trade_binding", "capacity_replay", "fee_replay", "cash_replay",
        "receivable_replay", "share_replay", "valuation_replay",
        "corporate_action_identity", "equity_replay",
    }
    source_only_errors = {
        "source_replay_exception",
        "corporate_action_economics_source",
        "share_lock_source",
        "valuation_price_source",
        "cohort_terminal_source",
    }
    accounting_passed = all(checks[key] for key in accounting_keys) and not any(
        item["check"] not in source_only_errors for item in errors
    )
    source_and_execution_passed = sources is not None and all(source_checks.values()) and not any(
        item["check"] == "source_replay_exception" for item in errors
    )
    overall_passed = (
        accounting_passed and source_and_execution_passed
        and target_completion_verifiable and not result.quality_errors
    )
    payload = {
        "protocol_version": "manual-portfolio-replay-v1",
        "result_hash": result.result_hash,
        "checks": checks,
        "accounting_passed": accounting_passed,
        "source_and_execution_passed": source_and_execution_passed,
        "target_completion_verifiable": target_completion_verifiable,
        "passed": overall_passed,
        "limitations": [] if sources is not None else [
            "VerifiedPortfolioSources is required for source and execution replay.",
        ],
        "first_difference": errors[0] if errors else None,
        "differences": errors,
        "replay_code_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    return {**payload, "replay_hash": _hash(payload)}


MONEY = pa.decimal128(24, 2)
PRICE = pa.decimal128(24, 8)
RATIO = pa.decimal128(38, 18)
TEXT = pa.string()
DAY = pa.date32()
INT = pa.int64()
BOOL = pa.bool_()


def _schema(fields: Sequence[tuple[str, pa.DataType, bool]]) -> pa.Schema:
    return pa.schema([pa.field(name, kind, nullable=nullable) for name, kind, nullable in fields])


_ARROW_SCHEMAS = {
    "signals.parquet": _schema([
        ("signal_date", DAY, False), ("code", TEXT, False), ("score", RATIO, False),
        ("rank_status", TEXT, False), ("cohort_id", TEXT, True), ("entry_date", DAY, True),
        ("planned_exit_date", DAY, True), ("cohort_budget", MONEY, True),
        ("cohort_terminal_status", TEXT, True), ("closed_date", DAY, True),
    ]),
    "order_intents.parquet": _schema([
        ("intent_id", TEXT, False), ("logical_intent_id", TEXT, False), ("date", DAY, False),
        ("phase", TEXT, False), ("cohort_id", TEXT, False), ("code", TEXT, False),
        ("side", TEXT, False), ("requested_quantity", INT, False), ("reference_price", PRICE, False),
        ("requested_notional", MONEY, False), ("reason", TEXT, False), ("status", TEXT, False),
        ("filled_quantity", INT, False),
    ]),
    "order_attempts.parquet": _schema([
        ("attempt_id", TEXT, False), ("intent_id", TEXT, False), ("logical_intent_id", TEXT, False),
        ("attempt_sequence", INT, False), ("date", DAY, False), ("phase", TEXT, False),
        ("cohort_id", TEXT, False), ("code", TEXT, False), ("side", TEXT, False),
        ("requested_quantity", INT, False), ("capacity_total", INT, False),
        ("capacity_used_before", INT, False), ("capacity_available", INT, False),
        ("capacity_used_after", INT, True), ("filled_quantity", INT, False),
        ("status", TEXT, False), ("reason", TEXT, True),
    ]),
    "trades.parquet": _schema([
        ("trade_id", TEXT, False), ("intent_id", TEXT, False), ("attempt_id", TEXT, False),
        ("event_sequence", INT, False), ("date", DAY, False), ("phase", TEXT, False),
        ("cohort_id", TEXT, False), ("code", TEXT, False), ("side", TEXT, False),
        ("quantity", INT, False), ("price", PRICE, False), ("gross", MONEY, False),
        ("commission", MONEY, False), ("transfer_fee", MONEY, False),
        ("stamp_duty", MONEY, False), ("total_fee", MONEY, False),
    ]),
    "corporate_actions.parquet": _schema([
        ("action_id", TEXT, False), ("action_hash", TEXT, False), ("economic_key", TEXT, False),
        ("date", DAY, False), ("stage", TEXT, False), ("code", TEXT, False),
        ("cohort_id", TEXT, True), ("record_date", DAY, True), ("ex_date", DAY, True),
        ("pay_date", DAY, True), ("stock_listing_date", DAY, True),
        ("cash_per_share", PRICE, True), ("bonus_ratio", RATIO, True),
        ("allocation_verified", BOOL, True), ("eligible_quantity", INT, True),
        ("gross_cash", MONEY, True), ("receivable_cash", MONEY, True),
        ("cash", MONEY, True), ("bonus_quantity", INT, True), ("source_hash", TEXT, False),
    ]),
    "positions.parquet": _schema([
        ("date", DAY, False), ("cohort_id", TEXT, False), ("code", TEXT, False),
        ("quantity", INT, False), ("sellable_quantity", INT, False),
        ("close", PRICE, False), ("market_value", MONEY, False),
    ]),
    "daily_portfolio.parquet": _schema([
        ("date", DAY, False), ("cash", MONEY, False), ("receivable_cash", MONEY, False),
        ("market_value", MONEY, False), ("equity", MONEY, False),
        ("daily_return", RATIO, False), ("position_count", INT, False),
    ]),
    "benchmark.parquet": _schema([
        ("date", DAY, False), ("close", PRICE, False), ("nav", RATIO, False),
        ("daily_return", RATIO, False),
    ]),
}

_PRIMARY_KEYS = {
    "signals.parquet": ("signal_date", "code"),
    "order_intents.parquet": ("intent_id",),
    "order_attempts.parquet": ("attempt_id",),
    "trades.parquet": ("trade_id",),
    "corporate_actions.parquet": ("date", "action_id", "stage", "cohort_id"),
    "positions.parquet": ("date", "cohort_id", "code"),
    "daily_portfolio.parquet": ("date",),
    "benchmark.parquet": ("date",),
}


def _schema_identity(schema: pa.Schema) -> list[dict[str, Any]]:
    return [{"name": item.name, "type": str(item.type), "nullable": item.nullable} for item in schema]


def _coerce(value: Any, kind: pa.DataType) -> Any:
    if value is None:
        return None
    if pa.types.is_date32(kind):
        return value if isinstance(value, date) else date.fromisoformat(str(value))
    if pa.types.is_decimal(kind):
        number = _decimal(value, "arrow_decimal")
        quantum = Decimal(1).scaleb(-kind.scale)
        return number.quantize(quantum)
    if pa.types.is_integer(kind):
        return int(value)
    if pa.types.is_boolean(kind):
        if not isinstance(value, bool):
            raise ValueError("arrow_boolean_value_required")
        return value
    return str(value)


def _table(rows: Sequence[Mapping[str, Any]], schema: pa.Schema) -> pa.Table:
    values = [
        {item.name: _coerce(row.get(item.name), item.type) for item in schema}
        for row in rows
    ]
    return pa.Table.from_pylist(values, schema=schema)


def _logical_rows(table: pa.Table, primary_key: Sequence[str]) -> list[dict[str, Any]]:
    rows = table.to_pylist()
    return sorted(rows, key=lambda row: tuple("" if row.get(key) is None else str(row.get(key)) for key in primary_key))


def _parquet_entry(path: Path, role: str, table: pa.Table) -> dict[str, Any]:
    schema_hash = _hash(_schema_identity(table.schema))
    logical_hash = _hash(_logical_rows(table, _PRIMARY_KEYS[path.name]))
    return {
        "role": role, "path": path.name, "format": "parquet",
        "schema_version": "manual-portfolio-arrow-v1", "row_count": table.num_rows,
        "size": path.stat().st_size, "sha256": _file_hash(path),
        "schema_hash": schema_hash, "logical_content_hash": logical_hash,
    }


def _json_entry(path: Path, role: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        "role": role, "path": path.name, "format": "json",
        "schema_version": str(payload.get("schema_version") or payload.get("protocol_version")),
        "row_count": len(payload.get("items", [])) if isinstance(payload, Mapping) else 1,
        "size": path.stat().st_size, "sha256": _file_hash(path),
        "schema_hash": _hash({"role": role, "schema_version": payload.get("schema_version") or payload.get("protocol_version")}),
        "logical_content_hash": _hash(payload),
    }


def write_portfolio_evidence(result: Any, output_dir: str | Path, *, sources: Any | None = None) -> Path:
    target = Path(output_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.parent / f".{target.name}.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise FileExistsError("portfolio_evidence_output_locked") from exc
    temporary: Path | None = None
    try:
        if target.exists():
            raise FileExistsError("portfolio_evidence_output_exists")
        temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
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
        tables = {}
        for name, schema in _ARROW_SCHEMAS.items():
            table = _table(rows_by_file[name], schema)
            pq.write_table(table, temporary / name, compression="zstd")
            tables[name] = table
        replay = replay_portfolio_result(result, sources=sources)
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
        audit = {
            "schema_version": "manual-portfolio-audit-v1",
            "items": result.audit, "quality_errors": result.quality_errors,
            "error_count": sum(item.get("severity") == "error" for item in result.audit),
            "warning_count": sum(item.get("severity") == "warning" for item in result.audit),
        }
        (temporary / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        (temporary / "replay.json").write_text(json.dumps(replay, ensure_ascii=False, indent=2), encoding="utf-8")
        (temporary / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        files = [
            _parquet_entry(temporary / name, name.removesuffix(".parquet"), tables[name])
            for name in sorted(tables)
        ]
        files.extend(
            _json_entry(temporary / name, name.removesuffix(".json"))
            for name in ("audit.json", "replay.json", "summary.json")
        )
        files.sort(key=lambda item: item["path"])
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
        verification = verify_portfolio_evidence_directory(temporary)
        if not verification["verified"]:
            raise ValueError("portfolio_evidence_staging_verification_failed")
        for path in temporary.iterdir():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        directory_fd = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.replace(temporary, target)
        temporary = None
        parent_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return target.resolve()
    except Exception:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
        raise
    finally:
        os.close(lock_fd)
        lock_path.unlink(missing_ok=True)


_EXPECTED_FILES = frozenset({*_ARROW_SCHEMAS, "audit.json", "replay.json", "summary.json", "manifest.json"})


def verify_portfolio_evidence_directory(source: str | Path) -> dict[str, Any]:
    root = Path(source)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("portfolio_evidence_directory_invalid")
    names = {path.name for path in root.iterdir()}
    if names != _EXPECTED_FILES:
        raise ValueError("portfolio_evidence_file_set_mismatch")
    if any(path.is_symlink() or not path.is_file() for path in root.iterdir()):
        raise ValueError("portfolio_evidence_file_invalid")
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("portfolio_evidence_manifest_invalid") from exc
    expected_manifest_hash = manifest.get("manifest_hash")
    payload = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    if expected_manifest_hash != _hash(payload):
        raise ValueError("portfolio_evidence_manifest_hash_mismatch")
    entries = manifest.get("files")
    if not isinstance(entries, list) or len(entries) != 11:
        raise ValueError("portfolio_evidence_manifest_files_invalid")
    by_path = {item.get("path"): item for item in entries if isinstance(item, Mapping)}
    if set(by_path) != _EXPECTED_FILES - {"manifest.json"} or len(by_path) != len(entries):
        raise ValueError("portfolio_evidence_manifest_paths_invalid")
    for name, entry in by_path.items():
        if Path(name).name != name or Path(name).is_absolute():
            raise ValueError("portfolio_evidence_manifest_path_escape")
        path = root / name
        if path.stat().st_size != int(entry.get("size", -1)) or _file_hash(path) != entry.get("sha256"):
            raise ValueError(f"portfolio_evidence_file_hash_mismatch:{name}")
        if name in _ARROW_SCHEMAS:
            table = pq.read_table(path)
            schema = _ARROW_SCHEMAS[name]
            if not table.schema.equals(schema, check_metadata=True):
                raise ValueError(f"portfolio_evidence_schema_mismatch:{name}")
            if _hash(_schema_identity(schema)) != entry.get("schema_hash"):
                raise ValueError(f"portfolio_evidence_schema_hash_mismatch:{name}")
            if table.num_rows != int(entry.get("row_count", -1)):
                raise ValueError(f"portfolio_evidence_row_count_mismatch:{name}")
            if _hash(_logical_rows(table, _PRIMARY_KEYS[name])) != entry.get("logical_content_hash"):
                raise ValueError(f"portfolio_evidence_logical_hash_mismatch:{name}")
        else:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if _hash(parsed) != entry.get("logical_content_hash"):
                raise ValueError(f"portfolio_evidence_logical_hash_mismatch:{name}")
    return {
        "verified": True, "manifest_hash": expected_manifest_hash,
        "manifest_file_sha256": _file_hash(root / "manifest.json"),
        "file_count": len(names),
    }


__all__ = [
    "calculate_portfolio_metrics", "replay_portfolio_result",
    "write_portfolio_evidence", "verify_portfolio_evidence_directory",
]
