"""NAV-based domestic-fund backtest engine.

Fund NAVs are published once per day, so this engine uses a strict two-step
contract: a target produced from D NAV executes at the next available NAV.
That makes the fund run compatible with the paper observation boundary.
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Type

import pandas as pd

from quant_engine.backtest.context import StrategyContext
from quant_engine.data.calendar import TradingCalendar
from quant_engine.backtest.strategy import Strategy
from quant_engine.backtest.types import OrderSide, Trade


class FundNavBacktestEngine:
    """Small deterministic engine for archived domestic-fund NAV datasets."""

    def __init__(
        self,
        strategy_class: Type[Strategy],
        *,
        paper_fee_rate: float = 0.0,
        **strategy_kwargs: Any,
    ):
        self.strategy_class = strategy_class
        self.strategy_kwargs = strategy_kwargs
        # No public fund feed in this project provides a broker fee schedule;
        # keep the paper model explicit and deterministic.  A user may provide
        # an assumed rate for sensitivity analysis, but it is never presented
        # as a real subscription/redemption fee.
        try:
            normalized_fee_rate = float(paper_fee_rate)
        except (TypeError, ValueError) as exc:
            raise ValueError("paper_fee_rate must be finite and non-negative") from exc
        if not math.isfinite(normalized_fee_rate) or normalized_fee_rate < 0:
            raise ValueError("paper_fee_rate must be finite and non-negative")
        self.fee_rate = normalized_fee_rate

    @staticmethod
    def _is_rebalance_day(day: date, frequency: str, previous: date | None) -> bool:
        if previous is None:
            return True
        if frequency == "daily":
            return True
        if frequency == "weekly":
            return day.isocalendar().week != previous.isocalendar().week
        if frequency == "monthly":
            return (day.year, day.month) != (previous.year, previous.month)
        raise ValueError("rebalance_frequency must be daily, weekly, or monthly")

    @staticmethod
    def _rows_by_date(
        rows: Mapping[str, list[dict[str, Any]]],
        start: date,
        end: date,
        calendar: TradingCalendar | None = None,
    ) -> tuple[list[date], dict[str, dict[str, float]]]:
        by_code: dict[str, dict[str, float]] = {}
        for code, values in rows.items():
            series: dict[str, float] = {}
            for item in values:
                try:
                    day = date.fromisoformat(str(item["date"]))
                    nav = float(item["nav"])
                except (KeyError, TypeError, ValueError):
                    continue
                if start <= day <= end and math.isfinite(nav) and nav > 0:
                    series[day.isoformat()] = nav
            if not series:
                raise ValueError(f"fund dataset has no rows in requested range: {code}")
            by_code[str(code)] = series
        if not by_code:
            raise ValueError("at least one fund dataset is required")
        dates = sorted(set.intersection(*(set(series) for series in by_code.values())))
        if calendar is not None:
            allowed = {day.isoformat() for day in calendar.get_trading_days(start, end)}
            dates = [day for day in dates if day in allowed]
        if not dates:
            if calendar is not None:
                raise ValueError("fund datasets have no common NAV dates on the supplied trading calendar")
            raise ValueError("fund datasets have no common NAV dates")
        return [date.fromisoformat(item) for item in dates], by_code

    def run(
        self,
        *,
        rows: Mapping[str, list[dict[str, Any]]],
        start: date,
        end: date,
        initial_capital: float,
        output_dir: str,
        rebalance_frequency: str = "daily",
        cancel_check=None,
        calendar: TradingCalendar | None = None,
    ) -> str:
        if not math.isfinite(float(initial_capital)) or initial_capital <= 0:
            raise ValueError("initial_capital must be positive and finite")
        trading_calendar = calendar or TradingCalendar(start_year=start.year, end_year=end.year)
        calendar_report = trading_calendar.ensure_coverage(start, end)
        if not calendar_report.get("complete"):
            raise ValueError(
                "trading_calendar_coverage_insufficient: "
                + json.dumps(calendar_report, ensure_ascii=False, sort_keys=True)
            )
        # Always bind the NAV intersection to the verified calendar selected
        # above, including when callers omit ``calendar`` and the engine
        # creates its own year-scoped TradingCalendar.  Passing ``None`` here
        # would validate the calendar but still simulate weekends/holidays.
        trading_days, navs = self._rows_by_date(rows, start, end, trading_calendar)
        context = StrategyContext(trading_calendar)
        strategy = self.strategy_class(context, **self.strategy_kwargs)
        try:
            strategy.initialize()
        except BaseException:
            # Initialization may allocate resources before the event loop is
            # entered, so the loop guard below cannot handle this path.  Keep
            # the original initialization error as the one surfaced to the
            # caller even if cleanup itself is imperfect.
            try:
                strategy.teardown()
            except Exception:
                pass
            raise
        # Strategies must consume the same canonical, valid NAV rows that the
        # execution loop uses.  Passing the raw archive here allowed a
        # malformed weekend/holiday NAV to influence lookback rankings even
        # though that row was excluded from simulated trading.
        allowed_history_dates = {day.isoformat() for day in trading_days}
        strategy._fund_history = {
            code: [
                {"date": day, "nav": value}
                for day, value in sorted(series.items())
                if day in allowed_history_dates
            ]
            for code, series in navs.items()
        }

        cash = float(initial_capital)
        positions: dict[str, float] = {}
        previous_total = cash
        pending: dict[str, float] | None = None
        pending_date: date | None = None
        last_rebalance: date | None = None
        portfolio_rows: list[dict[str, Any]] = []
        position_rows: list[dict[str, Any]] = []
        trade_rows: list[dict[str, Any]] = []
        signal_rows: list[dict[str, Any]] = []

        def value_at(day: date) -> float:
            return cash + sum(units * navs[code][day.isoformat()] for code, units in positions.items())

        def portfolio_view(day: date):
            """Expose the current NAV portfolio through StrategyContext."""
            position_view = {
                code: SimpleNamespace(
                    code=code,
                    shares=units,
                    avg_cost=navs[code][day.isoformat()],
                    market_value=units * navs[code][day.isoformat()],
                )
                for code, units in positions.items() if units > 0
            }
            market_value = sum(item.market_value for item in position_view.values())
            return SimpleNamespace(
                cash=cash,
                market_value=market_value,
                total_value=cash + market_value,
                positions=position_view,
            )

        def managed_trading_days():
            """Close the strategy lifecycle even when a run is interrupted.

            The generator's ``finally`` executes both after normal exhaustion
            and when the loop body raises (the runtime is CPython, where the
            iterator is deterministically finalized).  Keeping this guard
            around the existing event loop avoids emitting partial result
            files while still giving strategies a cleanup callback.
            """
            try:
                yield from trading_days
            finally:
                strategy.teardown()

        for day in managed_trading_days():
            if cancel_check and cancel_check():
                raise RuntimeError("fund backtest cancelled")
            day_key = day.isoformat()
            nav_for_day = {code: series[day_key] for code, series in navs.items()}
            context.set_date(day)
            context.set_portfolio(portfolio_view(day))
            strategy.before_trading()
            if pending is not None:
                total = value_at(day)
                target_codes = set(pending) | set(positions)
                proposals: list[dict[str, Any]] = []
                # Build the full target from the same pre-rebalance equity,
                # then execute sells before buys. Sorting the two legs makes a
                # code-order change unable to turn an otherwise fundable
                # rotation into a false insufficient-cash rejection.
                for code in sorted(target_codes):
                    nav = nav_for_day[code]
                    target_units = max(0.0, total * float(pending.get(code, 0.0)) / nav)
                    # Floor to the NAV precision so a 100% target cannot be
                    # rejected solely because binary rounding exceeds cash by
                    # a fraction of a cent.
                    target_units = math.floor(max(0.0, target_units) * 100.0 + 1e-9) / 100.0
                    delta = target_units - positions.get(code, 0.0)
                    if abs(delta) < 0.005:
                        continue
                    side = "buy" if delta > 0 else "sell"
                    units = abs(delta)
                    gross = units * nav
                    fee = gross * self.fee_rate
                    proposals.append({"code": code, "nav": nav, "side": side, "units": units, "gross": gross, "fee": fee})

                for proposal in sorted(proposals, key=lambda item: (item["side"] != "sell", item["code"])):
                    code = proposal["code"]
                    nav = proposal["nav"]
                    side = proposal["side"]
                    requested_units = proposal["units"]
                    units = requested_units
                    gross = proposal["gross"]
                    fee = proposal["fee"]
                    status = "filled"
                    reason = None
                    if side == "sell":
                        positions[code] = max(0.0, positions.get(code, 0.0) - units)
                        cash += gross - fee
                    else:
                        if gross + fee > cash + 1e-9:
                            # Spend only the cash that is actually available
                            # under the configured paper fee assumption.  This
                            # is an explicit partial fill, never a silent
                            # substitution of a later NAV.
                            affordable = cash / (nav * (1.0 + self.fee_rate)) if nav > 0 else 0.0
                            units = math.floor(max(0.0, affordable) * 100.0 + 1e-9) / 100.0
                            if units < 0.005:
                                trade_rows.append({
                                    "date": day, "code": code, "side": side,
                                    "units": 0.0, "requested_units": requested_units,
                                    "executed_units": 0.0, "price": nav,
                                    "amount": 0.0, "fee": 0.0, "status": "rejected",
                                    "reason": "insufficient_cash",
                                })
                                continue
                            gross = units * nav
                            fee = gross * self.fee_rate
                            status = "partial"
                            reason = "cash_limited"
                        cash -= gross + fee
                        positions[code] = positions.get(code, 0.0) + units
                    trade_rows.append({
                        "date": day, "code": code, "side": side, "units": units,
                        "requested_units": requested_units, "executed_units": units,
                        "price": nav, "amount": gross, "fee": fee,
                        "status": status, "reason": reason,
                    })
                    strategy.on_order_filled(Trade(
                        trade_id=f"fund-{day.isoformat()}-{code}-{side}",
                        order_id=f"fund-{day.isoformat()}-{code}-{side}",
                        code=code, date=day, side=OrderSide(side), shares=units,
                        price=nav, amount=gross, commission=fee,
                    ))
                pending = None
                pending_date = None
                context.set_portfolio(portfolio_view(day))

            total = value_at(day)
            if self._is_rebalance_day(day, rebalance_frequency, last_rebalance):
                signals = strategy.generate_signals(day)
                if not isinstance(signals, dict):
                    raise ValueError("fund strategy returned invalid target weights")
                normalized: dict[str, float] = {}
                for code, raw_weight in signals.items():
                    if not isinstance(code, str):
                        raise ValueError("fund strategy returned invalid target weights")
                    try:
                        weight = float(raw_weight)
                    except (TypeError, ValueError) as exc:
                        raise ValueError("fund strategy returned invalid target weights") from exc
                    if not math.isfinite(weight) or weight < 0:
                        raise ValueError("fund strategy returned invalid target weights")
                    normalized[code] = weight
                if sum(normalized.values()) > 1.0 + 1e-9:
                    raise ValueError("fund strategy returned invalid target weights")
                unknown = sorted(set(normalized) - set(navs))
                if unknown:
                    raise ValueError(f"fund strategy returned unknown fund code(s): {', '.join(unknown)}")
                old_total = value_at(day)
                old_weights = {
                    code: (units * nav_for_day[code]) / old_total
                    for code, units in positions.items() if units > 0 and old_total > 0
                }
                pending = {str(code): weight for code, weight in normalized.items() if weight > 0}
                pending_date = day
                last_rebalance = day
                for code, weight in pending.items():
                    signal_rows.append({"date": day, "code": code, "target_weight": weight})
                # Preserve explicit zero-weight targets in the lifecycle
                # callback even though the execution proposal only stores
                # positive positions; this mirrors the A-share contract and
                # lets stateful strategies observe liquidations.
                strategy.on_rebalance(day, old_weights, normalized)

            market_value = sum(units * nav_for_day[code] for code, units in positions.items())
            equity = cash + market_value
            daily_return = equity / previous_total - 1.0 if previous_total > 0 else 0.0
            portfolio_rows.append({"date": day, "total_value": equity, "cash": cash, "market_value": market_value, "daily_return": daily_return, "n_positions": sum(1 for units in positions.values() if units > 0)})
            for code, units in positions.items():
                if units > 0:
                    position_rows.append({"date": day, "code": code, "shares": units, "market_value": units * nav_for_day[code], "weight": (units * nav_for_day[code]) / equity if equity > 0 else 0.0})
            previous_total = equity
            context.set_portfolio(portfolio_view(day))

        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        portfolio = pd.DataFrame(portfolio_rows)
        if not portfolio.empty:
            portfolio["cumulative_return"] = (1 + portfolio["daily_return"]).cumprod() - 1
            portfolio.to_parquet(output / "daily_portfolio.parquet", index=False)
        if position_rows:
            pd.DataFrame(position_rows).to_parquet(output / "daily_positions.parquet", index=False)
        if trade_rows:
            pd.DataFrame(trade_rows).to_parquet(output / "trades.parquet", index=False)
        if signal_rows:
            pd.DataFrame(signal_rows).to_parquet(output / "signals.parquet", index=False)
        final_value = float(portfolio.iloc[-1]["total_value"]) if not portfolio.empty else initial_capital
        summary = {
            "start_date": trading_days[0].isoformat(), "end_date": trading_days[-1].isoformat(),
            "data_available_start": trading_days[0].isoformat(),
            "data_available_end": trading_days[-1].isoformat(),
            "n_trading_days": len(portfolio_rows), "n_trades": len([row for row in trade_rows if row["status"] in {"filled", "partial"}]),
            "n_orders": len(trade_rows), "n_signals_snapshots": len(signal_rows),
            "final_value": final_value, "total_return": final_value / initial_capital - 1.0,
            "execution_model": "next_valid_nav-v1", "execution_lag": "next_valid_nav",
            "calendar_version": calendar_report.get("calendar_version"),
            "calendar_source": calendar_report.get("source"),
            "calendar_content_hash": calendar_report.get("content_hash"),
            "calendar_coverage_start": calendar_report.get("coverage_start"),
            "calendar_coverage_end": calendar_report.get("coverage_end"),
            "calendar_verified": bool(calendar_report.get("verified")),
            "fee_model": "fund_nav_configurable_rate-v1", "fee_rate": self.fee_rate,
            "unexecuted_signal_date": pending_date.isoformat() if pending is not None and pending_date else None,
        }
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(output.resolve())
