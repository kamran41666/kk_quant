"""Evidence-oriented manual-daily portfolio loop built on ManualResearchLedger."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd
import numpy as np

from quant_engine.backtest.manual_research_ledger import (
    ManualResearchLedger,
    RESEARCH_EXECUTION_COSTS,
    ResearchCorporateAction,
    ResearchExecutionIntent,
)
from quant_engine.factor.manual_daily_bundle import ManualDailyFactorBundleV2


ZERO = Decimal("0")
LOT = Decimal("100")


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _decimal(value: Any, field_name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError(f"{field_name}_must_be_numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name}_must_be_finite")
    return result


def _date(value: Any) -> date:
    return value if type(value) is date else pd.Timestamp(value).date()


@dataclass(frozen=True)
class ManualPortfolioInputManifest:
    dataset_id: str
    dataset_content_hash: str
    calendar_content_hash: str
    signal_content_hash: str
    eligibility_content_hash: str
    corporate_action_content_hash: str
    benchmark_id: str
    benchmark_content_hash: str
    training_artifact_id: str
    validation_artifact_id: str
    training_artifact_hash: str = ""
    validation_artifact_hash: str = ""
    source_files: tuple[Mapping[str, Any], ...] = ()
    unresolved_adjustment_count: int = 0
    protocol_version: str = "manual-daily-portfolio-input-v1"

    def __post_init__(self) -> None:
        if self.protocol_version != "manual-daily-portfolio-input-v1":
            raise ValueError("unsupported_manual_portfolio_input_manifest")
        if not self.dataset_id or not self.benchmark_id or not self.training_artifact_id or not self.validation_artifact_id:
            raise ValueError("manual_portfolio_input_identity_required")
        for name in (
            "dataset_content_hash", "calendar_content_hash", "signal_content_hash",
            "eligibility_content_hash", "corporate_action_content_hash", "benchmark_content_hash",
        ):
            value = getattr(self, name)
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"{name}_must_be_sha256")
        for name in ("training_artifact_hash", "validation_artifact_hash"):
            value = getattr(self, name)
            if value and (len(value) != 64 or any(char not in "0123456789abcdef" for char in value)):
                raise ValueError(f"{name}_must_be_sha256")
        if isinstance(self.unresolved_adjustment_count, bool) or self.unresolved_adjustment_count < 0:
            raise ValueError("unresolved_adjustment_count_must_be_nonnegative")

    @property
    def manifest_hash(self) -> str:
        return _hash(self.__dict__)

    @property
    def source_files_complete(self) -> bool:
        roles = {str(item.get("role")) for item in self.source_files}
        return roles == {"daily", "actions", "securities", "calendar", "benchmark", "signals"}


@dataclass
class ManualDailyPortfolioV3Result:
    bundle: ManualDailyFactorBundleV2
    input_manifest: ManualPortfolioInputManifest
    start_date: date
    end_date: date
    initial_capital: Decimal
    signals: list[dict[str, Any]] = field(default_factory=list)
    cohorts: list[dict[str, Any]] = field(default_factory=list)
    intents: list[dict[str, Any]] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    corporate_actions: list[dict[str, Any]] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    daily: list[dict[str, Any]] = field(default_factory=list)
    benchmark: list[dict[str, Any]] = field(default_factory=list)
    audit: list[dict[str, Any]] = field(default_factory=list)
    quality_errors: list[str] = field(default_factory=list)

    @property
    def result_hash(self) -> str:
        return _hash({
            "protocol_version": "manual-daily-portfolio-v3",
            "strategy_core_hash": self.bundle.strategy_core_hash,
            "bundle_hash": self.bundle.bundle_hash,
            "cost_policy": RESEARCH_EXECUTION_COSTS[self.bundle.cost_scenario],
            "input_manifest_hash": self.input_manifest.manifest_hash,
            "start_date": self.start_date, "end_date": self.end_date,
            "initial_capital": self.initial_capital,
            "signals": self.signals, "cohorts": self.cohorts,
            "intents": self.intents, "attempts": self.attempts,
            "trades": self.trades, "corporate_actions": self.corporate_actions,
            "positions": self.positions, "daily": self.daily,
            "benchmark": self.benchmark, "audit": self.audit,
            "quality_errors": self.quality_errors,
        })

    @property
    def promotion_eligible(self) -> bool:
        # Independent replay and pair registration are intentionally required
        # before this can ever become true.
        return False

    def metrics(self) -> dict[str, str | int]:
        from quant_engine.backtest.manual_portfolio_evidence import calculate_portfolio_metrics

        return calculate_portfolio_metrics(self)

    def replay(self, sources: Any | None = None) -> dict[str, Any]:
        from quant_engine.backtest.manual_portfolio_evidence import replay_portfolio_result

        return replay_portfolio_result(self, sources=sources)

    def write_evidence(self, output_dir: str | Path, *, sources: Any | None = None) -> Path:
        from quant_engine.backtest.manual_portfolio_evidence import write_portfolio_evidence

        return write_portfolio_evidence(self, output_dir, sources=sources)


def _bar_map(frame: pd.DataFrame) -> dict[tuple[date, str], dict[str, Any]]:
    required = {"date", "code", "open", "high", "low", "close", "preclose", "volume", "amount", "is_suspended", "is_st"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"manual_v3_daily_fields_missing:{','.join(sorted(missing))}")
    rows = frame.copy()
    rows["date"] = rows["date"].map(_date)
    rows["code"] = rows["code"].astype(str).str.upper()
    if rows.duplicated(["date", "code"]).any():
        raise ValueError("manual_v3_daily_rows_not_unique")
    return {(row["date"], row["code"]): row for row in rows.to_dict("records")}


def _eligibility_map(frame: pd.DataFrame) -> dict[tuple[date, str], bool]:
    required = {"date", "code", "is_eligible"}
    if required - set(frame.columns):
        raise ValueError("manual_v3_eligibility_fields_missing")
    rows = frame.copy()
    rows["date"] = rows["date"].map(_date)
    rows["code"] = rows["code"].astype(str).str.upper()
    if rows.duplicated(["date", "code"]).any():
        raise ValueError("manual_v3_eligibility_rows_not_unique")
    result = {}
    for row in rows.to_dict("records"):
        if not isinstance(row["is_eligible"], (bool, np.bool_)):
            raise ValueError("manual_v3_eligibility_value_not_boolean")
        result[(row["date"], row["code"])] = bool(row["is_eligible"])
    return result


def _benchmark_rows(frame: pd.DataFrame, days: Sequence[date]) -> list[dict[str, Any]]:
    if {"date", "close"} - set(frame.columns):
        raise ValueError("manual_v3_benchmark_fields_missing")
    normalized_dates = frame["date"].map(_date)
    if normalized_dates.duplicated().any():
        raise ValueError("manual_v3_benchmark_dates_not_unique")
    values = {_date(row["date"]): _decimal(row["close"], "benchmark_close") for row in frame.to_dict("records")}
    if any(day not in values or values[day] <= 0 for day in days):
        raise ValueError("manual_v3_benchmark_coverage_insufficient")
    origin = values[days[0]]
    prior = origin
    result = []
    for day in days:
        close = values[day]
        result.append({
            "date": day.isoformat(), "close": str(close),
            "nav": str(close / origin), "daily_return": str(close / prior - Decimal("1")),
        })
        prior = close
    return result


def run_manual_daily_portfolio_v3(
    *,
    daily: pd.DataFrame,
    eligibility: pd.DataFrame,
    benchmark: pd.DataFrame,
    signals: Mapping[date | str, Mapping[str, Any]],
    corporate_actions: Sequence[ResearchCorporateAction],
    calendar: Any,
    bundle: ManualDailyFactorBundleV2,
    input_manifest: ManualPortfolioInputManifest,
    start: date,
    end: date,
    initial_capital: Decimal | int | str = Decimal("1000000"),
) -> ManualDailyPortfolioV3Result:
    if start > end:
        raise ValueError("manual_v3_start_after_end")
    if input_manifest.dataset_content_hash != bundle.dataset_content_hash:
        raise ValueError("manual_v3_bundle_dataset_hash_mismatch")
    if input_manifest.training_artifact_hash and input_manifest.training_artifact_hash != bundle.training_evidence_hash:
        raise ValueError("manual_v3_training_artifact_hash_mismatch")
    if input_manifest.validation_artifact_hash and input_manifest.validation_artifact_hash != bundle.validation_evidence_hash:
        raise ValueError("manual_v3_validation_artifact_hash_mismatch")
    coverage = calendar.ensure_coverage(start, end)
    if not coverage.get("complete") or coverage.get("content_hash") != input_manifest.calendar_content_hash:
        raise ValueError("manual_v3_calendar_evidence_mismatch")
    days = [day for day in calendar.get_trading_days(start, end) if start <= day <= end]
    if not days:
        raise ValueError("manual_v3_no_trading_days")
    bars = _bar_map(daily)
    eligibility_by_day = _eligibility_map(eligibility)
    scores = {
        _date(day): {str(code).upper(): _decimal(value, "signal") for code, value in values.items()}
        for day, values in signals.items()
    }
    benchmark_rows = _benchmark_rows(benchmark, days)
    if any(action.source_hash != input_manifest.corporate_action_content_hash for action in corporate_actions):
        raise ValueError("manual_v3_corporate_action_source_hash_mismatch")
    ledger = ManualResearchLedger(initial_capital, cost_scenario=bundle.cost_scenario)
    ledger.register_corporate_actions(list(corporate_actions))
    action_definitions = [{
        "action_id": action.action_id, "action_hash": action.action_hash,
        "economic_key": action.economic_key, "date": (action.ex_date or action.record_date or start).isoformat(),
        "stage": "definition", "code": action.code, "cohort_id": None,
        "record_date": action.record_date.isoformat() if action.record_date else None,
        "ex_date": action.ex_date.isoformat() if action.ex_date else None,
        "pay_date": action.pay_date.isoformat() if action.pay_date else None,
        "stock_listing_date": action.stock_listing_date.isoformat() if action.stock_listing_date else None,
        "cash_per_share": str(action.cash_per_share), "bonus_ratio": str(action.bonus_ratio),
        "allocation_verified": action.allocation_verified, "source_hash": action.source_hash,
    } for action in sorted(corporate_actions, key=lambda item: (item.economic_key, item.action_id))]
    cohorts: list[dict[str, Any]] = []
    signal_rows: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []
    engine_audit: list[dict[str, Any]] = []
    last_equity = _decimal(initial_capital, "initial_capital")

    for index, day in enumerate(days):
        ledger.process_corporate_actions_at_open(day)
        previous_day = days[index - 1] if index > 0 else None

        for cohort in cohorts:
            if cohort["status"] != "planned" or cohort["entry_date"] != day.isoformat():
                continue
            ranked = sorted(scores.get(_date(cohort["signal_date"]), {}).items(), key=lambda item: (-item[1], item[0]))[:bundle.top_n]
            per_name_budget = last_equity * bundle.cohort_gross_exposure / max(1, len(ranked))
            for order, (code, score) in enumerate(ranked, start=1):
                bar = bars.get((day, code), {})
                previous_bar = bars.get((previous_day, code), {}) if previous_day else {}
                raw_reference = bar.get("open")
                if raw_reference is None or pd.isna(raw_reference) or _decimal(raw_reference, "open") <= 0:
                    raw_reference = previous_bar.get("close")
                if raw_reference is None or pd.isna(raw_reference) or _decimal(raw_reference, "reference") <= 0:
                    engine_audit.append({"date": day.isoformat(), "code": code, "reason": "entry_reference_price_unavailable", "severity": "error"})
                    continue
                requested = int((per_name_budget / _decimal(raw_reference, "reference")).to_integral_value(rounding=ROUND_DOWN))
                requested = requested // int(LOT) * int(LOT)
                if requested <= 0:
                    engine_audit.append({"date": day.isoformat(), "code": code, "reason": "entry_target_below_round_lot", "severity": "warning"})
                    continue
                ledger.execute_intent(
                    ResearchExecutionIntent(
                        intent_id=f"{cohort['id']}:open:{order}:{code}", trade_date=day,
                        phase="open", cohort_id=cohort["id"], code=code, side="buy",
                        requested_quantity=requested, reference_price=raw_reference,
                        reason="cohort_entry",
                        logical_intent_id=f"{cohort['id']}:entry:{code}",
                    ),
                    bar=bar, previous_bar=previous_bar,
                    historically_eligible=eligibility_by_day.get((day, code), False),
                )
            cohort["status"] = "open" if any(key[0] == cohort["id"] for key in ledger.lots) else "entry_failed"

        for cohort in cohorts:
            if cohort["status"] not in {"open", "exiting"} or _date(cohort["exit_date"]) > day:
                continue
            keys = [key for key in sorted(ledger.lots) if key[0] == cohort["id"]]
            for order, key in enumerate(keys, start=1):
                lot = ledger.lots[key]
                code = lot.code
                bar = bars.get((day, code), {})
                previous_bar = bars.get((previous_day, code), {}) if previous_day else {}
                reference = bar.get("close")
                if reference is None or pd.isna(reference) or _decimal(reference, "close") <= 0:
                    reference = previous_bar.get("close")
                if reference is None or pd.isna(reference) or _decimal(reference, "reference") <= 0:
                    engine_audit.append({"date": day.isoformat(), "code": code, "reason": "exit_reference_price_unavailable", "severity": "error"})
                    continue
                ledger.execute_intent(
                    # Retries get immutable attempt records under one logical
                    # exit target, so they do not multiply the fill-rate denominator.
                    ResearchExecutionIntent(
                        intent_id=f"{cohort['id']}:close:{day.isoformat()}:{order}:{code}",
                        trade_date=day, phase="close", cohort_id=cohort["id"],
                        code=code, side="sell", requested_quantity=lot.quantity,
                        reference_price=reference, reason="cohort_exit",
                        logical_intent_id=f"{cohort['id']}:exit:{code}",
                        attempt_sequence=1 + sum(
                            row.get("logical_intent_id") == f"{cohort['id']}:exit:{code}"
                            for row in ledger.intents
                        ),
                    ),
                    bar=bar, previous_bar=previous_bar, historically_eligible=True,
                )
            if any(key[0] == cohort["id"] for key in ledger.lots):
                cohort["status"] = "exiting"
                if index + 1 < len(days):
                    cohort["exit_date"] = days[index + 1].isoformat()
            else:
                cohort["status"] = "closed"
                cohort["closed_date"] = day.isoformat()

        daily_row = ledger.mark_close(day, {
            code: bar for (bar_day, code), bar in bars.items() if bar_day == day
        })
        equity = _decimal(daily_row["equity"], "equity")
        daily_row["daily_return"] = str(equity / last_equity - Decimal("1")) if last_equity else "0"
        daily_rows.append(daily_row)
        last_equity = equity
        ledger.record_action_entitlements_at_close(day)

        day_scores = scores.get(day)
        if day_scores is not None:
            active_count = sum(item["status"] in {"planned", "open", "exiting"} for item in cohorts)
            status = "captured"
            if active_count >= bundle.cohort_count:
                status = "cohort_capacity_blocked"
            elif index + bundle.exit_offset < len(days):
                cohort_id = f"cohort-{day.isoformat()}"
                cohorts.append({
                    "id": cohort_id, "signal_date": day.isoformat(),
                    "entry_date": days[index + bundle.entry_offset].isoformat(),
                    "exit_date": days[index + bundle.exit_offset].isoformat(),
                    "status": "planned", "budget": str(equity * bundle.cohort_gross_exposure),
                })
            else:
                status = "insufficient_forward_calendar"
            for code, score in sorted(day_scores.items()):
                signal_rows.append({
                    "signal_date": day.isoformat(), "code": code, "score": str(score),
                    "rank_status": status,
                })

    quality_errors = sorted({
        *(item["reason"] for item in engine_audit if item.get("severity") == "error"),
        *(item["reason"] for item in ledger.corporate_action_audit if item.get("severity") == "error"),
        *(["unexplained_reference_adjustments"] if input_manifest.unresolved_adjustment_count else []),
        *(["insufficient_forward_calendar"] if any(row["rank_status"] == "insufficient_forward_calendar" for row in signal_rows) else []),
        *(["unfinished_cohorts_at_end"] if any(item["status"] in {"planned", "open", "exiting"} for item in cohorts) else []),
    })
    cohort_by_signal = {item["signal_date"]: item for item in cohorts}
    for signal_row in signal_rows:
        cohort = cohort_by_signal.get(signal_row["signal_date"])
        signal_row.update({
            "cohort_id": cohort["id"] if cohort else None,
            "entry_date": cohort["entry_date"] if cohort else None,
            "planned_exit_date": cohort["exit_date"] if cohort else None,
            "cohort_budget": cohort["budget"] if cohort else None,
            "cohort_terminal_status": cohort["status"] if cohort else None,
            "closed_date": cohort.get("closed_date") if cohort else None,
        })
    return ManualDailyPortfolioV3Result(
        bundle=bundle, input_manifest=input_manifest, start_date=days[0], end_date=days[-1],
        initial_capital=_decimal(initial_capital, "initial_capital"), signals=signal_rows,
        cohorts=cohorts, intents=ledger.intents, attempts=ledger.attempts,
        trades=ledger.trades, corporate_actions=action_definitions + ledger.corporate_actions,
        positions=ledger.positions, daily=daily_rows, benchmark=benchmark_rows,
        audit=engine_audit + ledger.corporate_action_audit,
        quality_errors=quality_errors,
    )


__all__ = [
    "ManualPortfolioInputManifest", "ManualDailyPortfolioV3Result",
    "run_manual_daily_portfolio_v3",
]
