"""Independently replay A-share research result accounting artifacts."""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

TOLERANCE = 0.01


def _date(value: Any) -> date:
    return pd.Timestamp(value).date()


def _number(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _run_path(matrix_path: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (matrix_path.parent / path).resolve()


class _Audit:
    def __init__(self, run_id: str, label: str | None, result_dir: Path, tolerance: float):
        self.run_id = run_id
        self.label = label
        self.result_dir = result_dir
        self.tolerance = tolerance
        self.errors: list[dict[str, Any]] = []
        self.checks = {
            "artifacts_readable": True,
            "trading_days_increasing": True,
            "trades_valid": True,
            "corporate_actions_valid": True,
            "cash_replay": True,
            "receivable_replay": True,
            "shares_replay": True,
            "market_value_reconcile": True,
            "equity_identity": True,
            "daily_return_reconcile": True,
            "nonnegative_balances": True,
        }
        self.max_diff = {
            "cash": 0.0,
            "receivable_cash": 0.0,
            "shares": 0.0,
            "market_value": 0.0,
            "total_value": 0.0,
            "daily_return": 0.0,
            "compounded_equity": 0.0,
        }

    def fail(
        self,
        check: str,
        message: str,
        *,
        day: date | None = None,
        code: str | None = None,
        difference: float | None = None,
    ) -> None:
        self.checks[check] = False
        error: dict[str, Any] = {"check": check, "message": message}
        if day is not None:
            error["date"] = str(day)
        if code is not None:
            error["code"] = code
        if difference is not None and math.isfinite(difference):
            error["difference"] = float(difference)
        self.errors.append(error)

    def difference(
        self,
        key: str,
        actual: float,
        expected: float,
        check: str,
        *,
        day: date,
        message: str,
        code: str | None = None,
        tolerance: float | None = None,
    ) -> None:
        difference = abs(actual - expected)
        self.max_diff[key] = max(self.max_diff[key], difference)
        if difference > (self.tolerance if tolerance is None else tolerance):
            self.fail(check, message, day=day, code=code, difference=difference)

    def payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "label": self.label,
            "result_dir": str(self.result_dir),
            "passed": all(self.checks.values()),
            "checks": self.checks,
            "max_diff": self.max_diff,
            "error_count": len(self.errors),
            "errors": self.errors,
        }


def audit_run(
    result_dir: Path,
    *,
    run_id: str,
    label: str | None = None,
    tolerance: float = TOLERANCE,
) -> dict[str, Any]:
    """Replay one completed result without importing the research ledger."""
    result_dir = Path(result_dir).resolve()
    audit = _Audit(run_id, label, result_dir, tolerance)
    try:
        summary = json.loads((result_dir / "summary.json").read_text(encoding="utf-8"))
        portfolio = pd.read_parquet(result_dir / "daily_portfolio.parquet")
        positions = pd.read_parquet(result_dir / "daily_positions.parquet")
        trades = pd.read_parquet(result_dir / "trades.parquet")
        actions = json.loads((result_dir / "corporate_actions.json").read_text(encoding="utf-8"))
        issues = json.loads((result_dir / "audit.json").read_text(encoding="utf-8"))
        initial_cash = _number(summary["initial_capital"], "initial_capital")
    except Exception as exc:  # noqa: BLE001 - every malformed external artifact becomes a run result
        audit.fail("artifacts_readable", f"cannot read required result artifacts: {type(exc).__name__}: {exc}")
        return audit.payload()

    required_portfolio = {
        "date", "cash", "receivable_cash", "market_value", "total_value", "daily_return",
    }
    required_positions = {"date", "code", "shares", "market_value"}
    required_trades = {
        "date", "code", "side", "shares", "price", "amount",
        "commission", "stamp_duty", "slippage",
    }
    for name, frame, required in (
        ("daily_portfolio", portfolio, required_portfolio),
        ("daily_positions", positions, required_positions),
        ("trades", trades, required_trades),
    ):
        missing = sorted(required - set(frame.columns))
        if missing:
            audit.fail("artifacts_readable", f"{name} missing columns: {missing}")
    if not audit.checks["artifacts_readable"]:
        return audit.payload()

    try:
        portfolio = portfolio.copy()
        positions = positions.copy()
        trades = trades.copy()
        portfolio["date"] = portfolio["date"].map(_date)
        positions["date"] = positions["date"].map(_date)
        trades["date"] = trades["date"].map(_date)
    except (TypeError, ValueError, OverflowError) as exc:
        audit.fail("artifacts_readable", f"invalid artifact date: {type(exc).__name__}: {exc}")
        return audit.payload()

    days = portfolio["date"].tolist()
    if not days or len(days) != len(set(days)) or days != sorted(days):
        audit.fail("trading_days_increasing", "portfolio dates must be unique and strictly increasing")
        return audit.payload()
    trading_days = set(days)
    if positions.duplicated(["date", "code"]).any():
        audit.fail("shares_replay", "daily positions contain duplicate date/code rows")

    actions_by_day: dict[date, list[dict[str, Any]]] = defaultdict(list)
    action_dates: list[date] = []
    for event in actions:
        try:
            event_day = _date(event["date"])
            stage = str(event["stage"])
            if event_day not in trading_days:
                raise ValueError("event date is not a recorded trading day")
            if stage not in {"record", "ex", "pay", "stock_listing"}:
                raise ValueError(f"unknown stage {stage!r}")
            actions_by_day[event_day].append(event)
            action_dates.append(event_day)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            audit.fail(
                "corporate_actions_valid",
                f"invalid corporate action: {type(exc).__name__}: {exc}",
            )
    if action_dates != sorted(action_dates):
        audit.fail("trading_days_increasing", "corporate action dates must be nondecreasing")

    delistings_by_day: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for issue in issues:
        if issue.get("reason") != "delisting_zero_recovery_stress":
            continue
        try:
            issue_day = _date(issue["date"])
            if issue_day not in trading_days:
                raise ValueError("delisting date is not a recorded trading day")
            delistings_by_day[issue_day].append(issue)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            audit.fail("shares_replay", f"invalid delisting audit event: {type(exc).__name__}: {exc}")

    trades_by_day: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in trades.to_dict("records"):
        day = row["date"]
        code = str(row["code"])
        try:
            if day not in trading_days:
                raise ValueError("trade date is not a recorded trading day")
            side = str(row["side"])
            shares = _number(row["shares"], "trade shares")
            price = _number(row["price"], "trade price")
            amount = _number(row["amount"], "trade amount")
            if side not in {"buy", "sell"}:
                raise ValueError(f"unknown trade side {side!r}")
            if shares <= 0 or shares != math.floor(shares):
                raise ValueError("trade shares must be a positive integer")
            if side == "buy" and int(shares) % 100:
                raise ValueError("buy shares must be a 100-share lot")
            if price <= 0 or abs(price * 100 - round(price * 100)) > 1e-7:
                raise ValueError("trade price must be a positive CNY cent tick")
            if abs(amount - shares * price) > tolerance:
                raise ValueError("trade amount does not equal shares times price")
            for field in ("commission", "stamp_duty", "slippage"):
                if _number(row[field], field) < 0:
                    raise ValueError(f"{field} must be nonnegative")
            trades_by_day[day].append(row)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            audit.fail("trades_valid", f"invalid trade: {type(exc).__name__}: {exc}", day=day, code=code)
    if trades["date"].tolist() != sorted(trades["date"].tolist()):
        audit.fail("trading_days_increasing", "trade dates must be nondecreasing")
    if positions["date"].tolist() != sorted(positions["date"].tolist()):
        audit.fail("trading_days_increasing", "position snapshot dates must be nondecreasing")

    cash = initial_cash
    shares_by_code: dict[str, int] = {}
    receivables: dict[str, float] = {}
    entitlements: dict[str, int] = {}
    previous_total = initial_cash
    compounded = initial_cash

    for day in days:
        for event in actions_by_day.get(day, []):
            stage = str(event["stage"])
            if stage == "record":
                continue
            action_id = str(event.get("action_id", ""))
            code = str(event.get("code", ""))
            try:
                if stage == "ex":
                    if action_id in entitlements:
                        entitled = int(_number(event.get("entitled_shares"), "entitled_shares"))
                        if entitled != entitlements[action_id]:
                            audit.fail(
                                "corporate_actions_valid",
                                "ex-date entitlement differs from the recorded holding",
                                day=day,
                                code=code,
                                difference=abs(entitled - entitlements[action_id]),
                            )
                    incoming = _number(event.get("cash_receivable", 0), "cash_receivable")
                    if incoming < 0:
                        raise ValueError("cash receivable must be nonnegative")
                    receivables[action_id] = receivables.get(action_id, 0.0) + incoming
                    if "bonus_shares_booked" in event:
                        bonus = _number(event["bonus_shares_booked"], "bonus_shares_booked")
                    elif event.get("bonus_allocation_verified") is False:
                        bonus = 0.0
                    else:
                        bonus = _number(event.get("bonus_shares", 0), "bonus_shares")
                    if bonus < 0 or bonus != math.floor(bonus):
                        raise ValueError("booked bonus shares must be a nonnegative integer")
                    if bonus:
                        shares_by_code[code] = shares_by_code.get(code, 0) + int(bonus)
                elif stage == "pay":
                    paid = _number(event.get("cash"), "paid cash")
                    available = receivables.get(action_id, 0.0)
                    if paid < 0 or paid > available + tolerance:
                        raise ValueError("paid cash exceeds the action receivable")
                    cash += paid
                    remaining = available - paid
                    if remaining > tolerance:
                        receivables[action_id] = remaining
                    else:
                        receivables.pop(action_id, None)
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                audit.fail(
                    "corporate_actions_valid",
                    f"cannot replay corporate action: {type(exc).__name__}: {exc}",
                    day=day,
                    code=code,
                )

        for issue in delistings_by_day.get(day, []):
            code = str(issue.get("code", ""))
            try:
                removed = int(_number(issue["shares"], "delisted shares"))
                held = shares_by_code.get(code, 0)
                if removed != held:
                    audit.fail(
                        "shares_replay",
                        "delisting audit shares do not equal replayed holdings",
                        day=day,
                        code=code,
                        difference=abs(removed - held),
                    )
                shares_by_code.pop(code, None)
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                audit.fail("shares_replay", f"cannot replay delisting: {type(exc).__name__}: {exc}", day=day, code=code)

        for trade in trades_by_day.get(day, []):
            code = str(trade["code"])
            side = str(trade["side"])
            try:
                quantity = int(_number(trade["shares"], "trade shares"))
                amount = _number(trade["amount"], "trade amount")
                fees = sum(
                    _number(trade[field], field)
                    for field in ("commission", "stamp_duty", "slippage")
                )
                if side == "buy":
                    cash -= amount + fees
                    shares_by_code[code] = shares_by_code.get(code, 0) + quantity
                elif side == "sell":
                    cash += amount - fees
                    shares_by_code[code] = shares_by_code.get(code, 0) - quantity
                    if shares_by_code[code] == 0:
                        shares_by_code.pop(code)
                if cash < -tolerance or shares_by_code.get(code, 0) < 0:
                    audit.fail(
                        "nonnegative_balances",
                        "replayed cash or shares became negative",
                        day=day,
                        code=code,
                    )
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                audit.fail("trades_valid", f"cannot replay trade: {type(exc).__name__}: {exc}", day=day, code=code)

        for event in actions_by_day.get(day, []):
            if str(event["stage"]) != "record":
                continue
            action_id = str(event.get("action_id", ""))
            code = str(event.get("code", ""))
            try:
                recorded = int(_number(event.get("shares"), "record-date shares"))
                replayed = shares_by_code.get(code, 0)
                entitlements[action_id] = recorded
                if recorded != replayed:
                    audit.fail(
                        "corporate_actions_valid",
                        "record-date entitlement differs from replayed closing shares",
                        day=day,
                        code=code,
                        difference=abs(recorded - replayed),
                    )
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                audit.fail(
                    "corporate_actions_valid",
                    f"cannot replay record-date entitlement: {type(exc).__name__}: {exc}",
                    day=day,
                    code=code,
                )

        snapshot = portfolio.loc[portfolio["date"] == day].iloc[0]
        day_positions = positions.loc[positions["date"] == day]
        recorded_shares: dict[str, int] = {}
        position_market_value = 0.0
        for row in day_positions.to_dict("records"):
            code = str(row["code"])
            try:
                quantity = _number(row["shares"], "position shares")
                market_value = _number(row["market_value"], "position market value")
                if quantity <= 0 or quantity != math.floor(quantity) or market_value < 0:
                    raise ValueError("position shares/value must be nonnegative and shares integral")
                recorded_shares[code] = int(quantity)
                position_market_value += market_value
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                audit.fail("shares_replay", f"invalid position snapshot: {type(exc).__name__}: {exc}", day=day, code=code)

        for code in sorted(set(recorded_shares) | set(shares_by_code)):
            actual = float(recorded_shares.get(code, 0))
            expected = float(shares_by_code.get(code, 0))
            audit.difference(
                "shares", actual, expected, "shares_replay", day=day, code=code,
                message="position shares differ from trades/actions replay",
            )

        recorded_cash = _number(snapshot["cash"], "portfolio cash")
        recorded_receivable = _number(snapshot["receivable_cash"], "portfolio receivable_cash")
        recorded_market_value = _number(snapshot["market_value"], "portfolio market_value")
        recorded_total = _number(snapshot["total_value"], "portfolio total_value")
        replayed_receivable = sum(receivables.values())
        audit.difference(
            "cash", recorded_cash, cash, "cash_replay", day=day,
            message="portfolio cash differs from independent replay",
        )
        audit.difference(
            "receivable_cash", recorded_receivable, replayed_receivable,
            "receivable_replay", day=day,
            message="portfolio receivable differs from independent replay",
        )
        audit.difference(
            "market_value", recorded_market_value, position_market_value,
            "market_value_reconcile", day=day,
            message="portfolio market value differs from position snapshots",
        )
        identity_total = recorded_cash + recorded_receivable + position_market_value
        audit.difference(
            "total_value", recorded_total, identity_total, "equity_identity", day=day,
            message="total equity does not equal cash plus receivables plus positions",
        )

        expected_return = recorded_total / previous_total - 1.0
        recorded_return = _number(snapshot["daily_return"], "daily_return")
        audit.difference(
            "daily_return", recorded_return, expected_return,
            "daily_return_reconcile", day=day,
            message="daily return does not match consecutive equity snapshots",
            tolerance=1e-12,
        )
        compounded *= 1.0 + recorded_return
        audit.difference(
            "compounded_equity", recorded_total, compounded,
            "daily_return_reconcile", day=day,
            message="daily return compounding does not reconstruct equity",
        )
        if cash < -tolerance or replayed_receivable < -tolerance or any(
            value < 0 for value in shares_by_code.values()
        ):
            audit.fail("nonnegative_balances", "negative replayed balance at close", day=day)
        previous_total = recorded_total

    payload = audit.payload()
    delisting_count = sum(len(values) for values in delistings_by_day.values())
    payload["external_validity_flags"] = {
        "delisting_zero_recovery_stress_count": delisting_count,
        "delisting_is_verified_real_settlement": False if delisting_count else None,
    }
    return payload


def audit_matrix(
    matrix_path: Path,
    *,
    output_path: Path | None = None,
    tolerance: float = TOLERANCE,
) -> dict[str, Any]:
    """Audit every completed run referenced by a research matrix."""
    matrix_path = Path(matrix_path).resolve()
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    completed = [run for run in matrix.get("runs", []) if run.get("status") == "completed"]
    results = []
    for run in completed:
        run_id = str(run.get("run_id") or Path(str(run["result_dir"])).name)
        results.append(audit_run(
            _run_path(matrix_path, str(run["result_dir"])),
            run_id=run_id,
            label=run.get("label"),
            tolerance=tolerance,
        ))
    passed = sum(result["passed"] for result in results)
    payload = {
        "matrix": str(matrix_path),
        "scope": "independent_accounting_replay_only",
        "note": "Passing verifies artifact accounting consistency only; it does not validate source completeness or profitability.",
        "tolerance": tolerance,
        "matrix_run_count": len(matrix.get("runs", [])),
        "completed_run_count": len(completed),
        "passed_run_count": passed,
        "failed_run_count": len(results) - passed,
        "all_completed_runs_passed": bool(results) and passed == len(results),
        "runs": results,
    }
    target = Path(output_path).resolve() if output_path else matrix_path.parent / "accounting-audit.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = audit_matrix(args.matrix, output_path=args.output)
    print(json.dumps({
        "completed": result["completed_run_count"],
        "passed": result["passed_run_count"],
        "failed": result["failed_run_count"],
        "output": str((args.output or args.matrix.parent / "accounting-audit.json").resolve()),
    }, ensure_ascii=False))
    return 0 if result["all_completed_runs_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
