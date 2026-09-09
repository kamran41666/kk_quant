"""Deterministic raw-price portfolio simulation for ManualDailyFactorBundleV2."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from quant_engine.factor.manual_daily_bundle import ManualDailyFactorBundleV2
from quant_engine.trading.effective_rules import EffectiveDatedTradingRuleRegistry


ZERO = Decimal("0")
CENT = Decimal("0.01")
SHARE = Decimal("1")
LOT = Decimal("100")
COSTS = {
    "baseline": {"commission_rate": Decimal("0.00025"), "min_commission": Decimal("5"), "transfer_rate": Decimal("0.00002"), "slippage_rate": Decimal("0.001")},
    "stress": {"commission_rate": Decimal("0.0005"), "min_commission": Decimal("5"), "transfer_rate": Decimal("0.00002"), "slippage_rate": Decimal("0.003")},
}


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _money(value: Any) -> Decimal:
    return _d(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _sha(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _as_date(value: Any) -> date:
    if type(value) is date:
        return value
    return pd.Timestamp(value).date()


@dataclass
class _Holding:
    cohort_id: str
    code: str
    quantity: Decimal
    cost: Decimal
    buy_date: date


@dataclass
class ManualDailyPortfolioResult:
    bundle_hash: str
    start_date: date
    end_date: date
    initial_capital: Decimal
    daily: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    cohorts: list[dict[str, Any]] = field(default_factory=list)
    audit: list[dict[str, Any]] = field(default_factory=list)

    @property
    def final_equity(self) -> Decimal:
        return _d(self.daily[-1]["equity"]) if self.daily else self.initial_capital

    @property
    def total_return(self) -> Decimal:
        return self.final_equity / self.initial_capital - Decimal("1")

    @property
    def result_hash(self) -> str:
        return _sha({"bundle_hash": self.bundle_hash, "daily": self.daily, "trades": self.trades, "cohorts": self.cohorts, "audit": self.audit})

    def summary(self) -> dict[str, Any]:
        return {
            "protocol_version": "manual-daily-portfolio-v2",
            "bundle_hash": self.bundle_hash, "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(), "initial_capital": str(self.initial_capital),
            "final_equity": str(self.final_equity), "total_return": str(self.total_return),
            "result_hash": self.result_hash, "trade_count": len(self.trades),
            "audit_count": len(self.audit), "research_only": True, "paper_authorized": False,
            "auto_submit": False,
        }

    def write(self, output_dir: str | Path) -> Path:
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(self.daily).to_parquet(output / "daily_portfolio.parquet", index=False)
        pd.DataFrame(self.trades).to_parquet(output / "trades.parquet", index=False)
        pd.DataFrame(self.cohorts).to_parquet(output / "cohorts.parquet", index=False)
        (output / "audit.json").write_text(json.dumps(self.audit, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        (output / "summary.json").write_text(json.dumps(self.summary(), ensure_ascii=False, indent=2), encoding="utf-8")
        return output.resolve()


def _bars(daily: pd.DataFrame) -> dict[tuple[date, str], dict[str, Any]]:
    required = {"date", "code", "open", "close"}
    missing = required - set(daily.columns)
    if missing:
        raise ValueError(f"manual daily portfolio missing fields: {sorted(missing)}")
    frame = daily.copy()
    frame["date"] = frame["date"].map(_as_date)
    frame["code"] = frame["code"].astype(str).str.upper()
    if frame.duplicated(["date", "code"]).any():
        raise ValueError("manual daily portfolio bars must be unique by date and code")
    result: dict[tuple[date, str], dict[str, Any]] = {}
    for row in frame.to_dict("records"):
        result[(row["date"], row["code"])] = row
    return result


def run_manual_daily_portfolio(
    *,
    daily: pd.DataFrame,
    signals: Mapping[date | str, Mapping[str, Any]],
    calendar: Any,
    bundle: ManualDailyFactorBundleV2,
    start: date,
    end: date,
    initial_capital: Decimal | int | str = Decimal("1000000"),
) -> ManualDailyPortfolioResult:
    """Run two rolling cohorts; inputs are archived data and injected calendar only."""
    if start > end:
        raise ValueError("start must not be after end")
    if bundle.cost_scenario not in COSTS:
        raise ValueError("unsupported manual daily cost scenario")
    coverage = calendar.ensure_coverage(start, end)
    if not coverage.get("complete"):
        raise ValueError("manual_daily_calendar_coverage_insufficient")
    days = [day for day in calendar.get_trading_days(start, end) if start <= day <= end]
    if not days:
        raise ValueError("manual_daily_no_trading_days")
    bars = _bars(daily)
    scores = {_as_date(day): {str(code).upper(): _d(value) for code, value in values.items()} for day, values in signals.items()}
    costs = COSTS[bundle.cost_scenario]
    rules = EffectiveDatedTradingRuleRegistry.default()
    cash = _money(initial_capital)
    holdings: dict[str, _Holding] = {}
    cohorts: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []
    last_equity = cash

    def add_issue(day: date, code: str, reason: str, **extra: Any) -> None:
        audit.append({"date": day.isoformat(), "code": code, "reason": reason, **extra})

    def mark(day: date) -> Decimal:
        total = cash
        for holding in holdings.values():
            bar = bars.get((day, holding.code))
            if not bar or pd.isna(bar.get("close")) or _d(bar["close"]) <= ZERO:
                add_issue(day, holding.code, "held_close_missing")
                continue
            total += holding.quantity * _d(bar["close"])
        return _money(total)

    def fees(gross: Decimal, side: str, day: date) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        commission = max(_money(gross * costs["commission_rate"]), costs["min_commission"]) if gross else ZERO
        transfer = _money(gross * costs["transfer_rate"])
        stamp_rate = rules.resolve("a-share", day).stamp_duty_rate if side == "sell" else ZERO
        stamp = _money(gross * stamp_rate)
        return commission, transfer, stamp, commission + transfer + stamp

    def trade(day: date, cohort_id: str, code: str, side: str, quantity: Decimal, price: Decimal, reason: str) -> bool:
        nonlocal cash
        gross = _money(quantity * price)
        commission, transfer, stamp, total_fee = fees(gross, side, day)
        if side == "buy" and cash < gross + total_fee:
            add_issue(day, code, "cash_insufficient", requested=str(quantity))
            return False
        if side == "sell":
            holding = holdings.get(f"{cohort_id}:{code}")
            if holding is None or holding.quantity < quantity:
                add_issue(day, code, "holding_insufficient", requested=str(quantity))
                return False
            holding.quantity -= quantity
            cash += gross - total_fee
            if holding.quantity == ZERO:
                del holdings[f"{cohort_id}:{code}"]
        else:
            cash -= gross + total_fee
            key = f"{cohort_id}:{code}"
            current = holdings.get(key)
            if current:
                current.cost += gross + total_fee
                current.quantity += quantity
            else:
                holdings[key] = _Holding(cohort_id, code, quantity, gross + total_fee, day)
        trades.append({"date": day.isoformat(), "cohort_id": cohort_id, "code": code, "side": side, "quantity": str(quantity), "price": str(price), "gross": str(gross), "commission": str(commission), "transfer_fee": str(transfer), "stamp_duty": str(stamp), "total_fee": str(total_fee), "reason": reason})
        return True

    for index, day in enumerate(days):
        # Opening enters are based only on the signal captured on the prior
        # close; no same-day close value leaks into the entry price.
        for cohort in cohorts:
            if cohort["status"] != "planned" or cohort["entry_date"] != day.isoformat():
                continue
            selected = sorted(scores.get(_as_date(cohort["signal_date"]), {}).items(), key=lambda item: (-item[1], item[0]))[:bundle.top_n]
            eligible = [item for item in selected if (day, item[0]) in bars and not bool(bars[(day, item[0])].get("is_suspended", False))]
            budget_each = _money(last_equity * bundle.cohort_gross_exposure / max(1, len(eligible)))
            for code, _score in eligible:
                raw_open = bars[(day, code)].get("open")
                if raw_open is None or pd.isna(raw_open) or _d(raw_open) <= ZERO:
                    add_issue(day, code, "entry_open_missing", cohort_id=cohort["id"])
                    continue
                price = _money(_d(raw_open) * (Decimal("1") + costs["slippage_rate"]))
                quantity = (budget_each / price).to_integral_value(rounding=ROUND_DOWN) // LOT * LOT
                while quantity >= LOT and cash < _money(quantity * price) + fees(_money(quantity * price), "buy", day)[3]:
                    quantity -= LOT
                if quantity >= LOT:
                    trade(day, cohort["id"], code, "buy", quantity, price, "cohort_entry")
            cohort["status"] = "open"

        # Close exits are allowed to sell the full remaining position,
        # including an odd final quantity.
        for cohort in cohorts:
            if cohort["status"] != "open" or cohort["exit_date"] != day.isoformat():
                continue
            for key, holding in list(holdings.items()):
                if holding.cohort_id != cohort["id"]:
                    continue
                bar = bars.get((day, holding.code))
                if not bar or bool(bar.get("is_suspended", False)) or bar.get("close") is None or pd.isna(bar.get("close")):
                    add_issue(day, holding.code, "exit_close_unavailable", cohort_id=cohort["id"])
                    cohort["exit_date"] = days[min(index + 1, len(days) - 1)]
                    continue
                price = _money(_d(bar["close"]) * (Decimal("1") - costs["slippage_rate"]))
                trade(day, cohort["id"], holding.code, "sell", holding.quantity, price, "cohort_exit")
            if not any(item.cohort_id == cohort["id"] for item in holdings.values()):
                cohort["status"] = "closed"
                cohort["closed_date"] = day.isoformat()

        equity = mark(day)
        daily_rows.append({"date": day.isoformat(), "cash": str(_money(cash)), "market_value": str(_money(equity - cash)), "equity": str(equity), "daily_return": str(equity / last_equity - Decimal("1") if last_equity else ZERO), "open_positions": sum(1 for item in holdings.values() if item.quantity > ZERO)})
        last_equity = equity

        # Capture a close signal only after the day's mark.  A cohort is
        # created if the two-cohort capacity has become available.
        if day in scores and sum(item["status"] in {"planned", "open"} for item in cohorts) < bundle.cohort_count:
            entry_index = index + bundle.entry_offset
            exit_index = index + bundle.exit_offset
            if entry_index < len(days) and exit_index < len(days):
                cohort = {"id": f"cohort-{day.isoformat()}", "signal_date": day.isoformat(), "entry_date": days[entry_index].isoformat(), "exit_date": days[exit_index].isoformat(), "status": "planned", "budget": str(_money(last_equity * bundle.cohort_gross_exposure))}
                cohorts.append(cohort)
    return ManualDailyPortfolioResult(bundle.bundle_hash, days[0], days[-1], _money(initial_capital), daily_rows, trades, cohorts, audit)


__all__ = ["ManualDailyPortfolioResult", "run_manual_daily_portfolio"]
