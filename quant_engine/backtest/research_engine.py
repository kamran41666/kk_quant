"""Isolated daily research runner with raw fills and corporate-action accounts.

This runner never creates paper-trading authorization. Its inputs are archived
frames, not network providers; the caller attaches their verified identities.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from quant_engine.backtest.context import StrategyContext
from quant_engine.backtest.protocol import (StrategySpec, normalize_strategy_output,
    normalize_strategy_diagnostics, resolve_strategy_data_requirements)
from quant_engine.backtest.recorder import Recorder
from quant_engine.backtest.research_ledger import (
    ResearchAction, ResearchLedger, canonical_code, finite_positive, main_board,
)
from quant_engine.factor import compute_factor, get_factor_definition


DAILY_FIELDS = ("open", "high", "low", "close", "preclose", "volume", "amount",
                "turnover_rate", "is_suspended", "is_st", "adjusted_close")


def _date(value):
    if value is None or pd.isna(value) or str(value).strip() == "":
        return None
    return pd.Timestamp(value).date()


class ResearchDataPortal:
    """Dense date-indexed signal views; gaps remain explicit NaNs."""
    def __init__(
        self,
        daily: pd.DataFrame,
        securities: pd.DataFrame,
        days: list[date],
        factor_expressions: dict[str, object] | None = None,
    ):
        self.days = days
        self.codes = sorted(securities["code"].tolist())
        self._code_indices = {code: i for i, code in enumerate(self.codes)}
        self._day_indices = {day: i for i, day in enumerate(days)}
        self.securities = securities.set_index("code").to_dict("index")
        self._raw = {}
        for field in DAILY_FIELDS:
            table = daily.pivot(index="date", columns="code", values=field).reindex(index=days, columns=self.codes)
            self._raw[field] = table.to_numpy(dtype=float, na_value=np.nan)
        self._signals = dict(self._raw)
        ratio = np.divide(self._raw["adjusted_close"], self._raw["close"],
                          out=np.full_like(self._raw["close"], np.nan), where=self._raw["close"] > 0)
        for field in ("open", "high", "low"):
            self._signals[field] = self._raw[field] * ratio
        self._signals["close"] = self._raw["adjusted_close"]
        self._cursor = -1
        self._factor_cache: dict[tuple[str, date], pd.Series] = {}
        self._factor_expressions = dict(factor_expressions or {})

    def set_date(self, day: date | None):
        self._cursor = self._day_indices.get(day, -1)

    @property
    def universe(self):
        if self._cursor < 0:
            return []
        return self.eligible(self.days[self._cursor])

    def eligible(self, day: date) -> list[str]:
        cursor = self._day_indices[day]
        usable = np.isfinite(self._raw["close"][cursor]) & (self._raw["close"][cursor] > 0)
        usable &= self._raw["is_suspended"][cursor] == 0
        usable &= self._raw["is_st"][cursor] == 0
        result = []
        for index in np.flatnonzero(usable):
            code = self.codes[index]
            security = self.securities[code]
            ipo, out = security["ipo_date"], security["out_date"]
            if ipo and (day - ipo).days >= 180 and (out is None or day < out):
                result.append(code)
        return result

    def raw_bars(self, day: date | None) -> dict[str, dict]:
        cursor = self._day_indices.get(day, -1)
        if cursor < 0:
            return {}
        result = {}
        for i, code in enumerate(self.codes):
            if not np.isfinite(self._raw["close"][cursor, i]):
                continue
            row = {field: values[cursor, i] for field, values in self._raw.items()}
            row["is_suspended"] = row["is_suspended"] != 0
            row["is_st"] = row["is_st"] != 0
            result[code] = row
        return result

    def _history_at(self, cursor: int, codes, lookback, fields):
        selected = list(codes) if codes else list(self.codes)
        unknown = set(selected) - set(self._code_indices)
        if unknown:
            raise ValueError(f"unknown research history codes: {sorted(unknown)}")
        missing = set(fields) - set(self._signals)
        if missing:
            raise ValueError(f"unsupported research history fields: {sorted(missing)}")
        if cursor < 0:
            return pd.DataFrame(columns=list(fields), index=pd.MultiIndex.from_arrays([[], []], names=["code", "date"]))
        first = max(0, cursor - lookback + 1)
        window = self.days[first:cursor + 1]
        indices = [self._code_indices[code] for code in selected]
        index = pd.MultiIndex.from_product([selected, window], names=["code", "date"])
        return pd.DataFrame({field: self._signals[field][first:cursor + 1, indices].T.reshape(-1)
                             for field in fields}, index=index)

    def history(self, codes, lookback, fields):
        return self._history_at(self._cursor, codes, lookback, fields)

    def _factor_history_at(self, cursor: int, lookback: int, fields) -> pd.DataFrame:
        """Factor panel with eligibility evaluated independently on every date."""
        panel = self._history_at(cursor, self.codes, lookback, fields)
        if panel.empty:
            return panel
        first = max(0, cursor - lookback + 1)
        window = self.days[first:cursor + 1]
        eligible = (
            np.isfinite(self._raw["close"][first:cursor + 1])
            & (self._raw["close"][first:cursor + 1] > 0)
            & (self._raw["is_suspended"][first:cursor + 1] == 0)
            & (self._raw["is_st"][first:cursor + 1] == 0)
        )
        for code_index, code in enumerate(self.codes):
            security = self.securities[code]
            ipo, out = security["ipo_date"], security["out_date"]
            active = np.fromiter(
                (
                    bool(ipo and (day - ipo).days >= 180 and (out is None or day < out))
                    for day in window
                ),
                dtype=bool,
                count=len(window),
            )
            eligible[:, code_index] &= active
        valid_rows = pd.Series(eligible.T.reshape(-1), index=panel.index)
        panel.loc[~valid_rows, :] = np.nan
        return panel

    def current(self, code, field):
        if self._cursor < 0 or code not in self._code_indices:
            return float("nan")
        if field not in self._signals:
            raise ValueError(f"unsupported research current field: {field}")
        return float(self._signals[field][self._cursor, self._code_indices[code]])

    def factor(self, name: str, as_of: date) -> pd.Series:
        """Return a factor cross-section computed from the frozen signal view."""
        if self._cursor < 0:
            raise RuntimeError("research data portal is not positioned on a trading date")
        as_of_cursor = self._day_indices.get(as_of)
        if as_of_cursor is None:
            raise ValueError(f"factor as_of date is outside the research calendar: {as_of}")
        if as_of_cursor > self._cursor:
            raise ValueError("factor as_of date cannot exceed the visible research date")
        cache_key = (name, as_of)
        cached = self._factor_cache.get(cache_key)
        if cached is not None:
            return cached.copy()

        expression = self._factor_expressions.get(name)
        definition = expression or get_factor_definition(name)
        if getattr(definition, "category", None) == "raw_fundamental":
            raise ValueError(
                f"research factor {name!r} requires a point-in-time fundamental adapter"
            )
        inputs = tuple(
            getattr(definition, "required_fields", getattr(definition, "inputs", ()))
        )
        window = getattr(definition, "lookback", getattr(definition, "window", None))
        if not inputs or not isinstance(window, int):
            raise ValueError(f"research factor definition is incomplete: {name}")
        missing = sorted(set(inputs) - set(self._signals))
        if missing:
            raise ValueError(
                f"research factor {name!r} requires unavailable fields: {', '.join(missing)}"
            )
        panel = self._factor_history_at(
            as_of_cursor,
            window,
            inputs,
        )
        calculated = expression.compute(panel) if expression else compute_factor(name, panel)
        factor_dates = calculated.index.get_level_values("date")
        current = calculated.loc[factor_dates == as_of]
        if current.empty:
            result = pd.Series(index=self.codes, dtype=float, name=name)
        else:
            result = current.droplevel("date").reindex(self.codes)
            result.index.name = "code"
            result.name = name
        self._factor_cache[cache_key] = result.copy()
        return result


class ResearchBacktestEngine:
    def __init__(
        self,
        strategy_class,
        parameters: dict | None = None,
        cost_scenario="baseline",
        factor_expressions: dict[str, object] | None = None,
    ):
        self.strategy_class = strategy_class
        self.parameters = dict(parameters or {})
        self.cost_scenario = cost_scenario
        self.factor_expressions = dict(factor_expressions or {})

    def run(self, *, daily: pd.DataFrame, actions: pd.DataFrame, securities: pd.DataFrame,
            start: date, end: date, calendar, output_dir: str,
            initial_capital: float = 1_000_000., rebalance_frequency: str | None = None) -> str:
        if start > end:
            raise ValueError("start must not be after end")
        ledger = ResearchLedger(float(initial_capital), self.cost_scenario)
        daily = daily.copy()
        securities = securities.copy()
        missing = {"code", "date", *DAILY_FIELDS} - set(daily.columns)
        if missing:
            raise ValueError(f"missing research daily fields: {sorted(missing)}")
        if not {"code", "ipo_date", "out_date"}.issubset(securities.columns):
            raise ValueError("securities needs code, ipo_date, out_date")
        daily["code"] = daily["code"].map(canonical_code)
        securities["code"] = securities["code"].map(canonical_code)
        daily["date"] = daily["date"].map(_date)
        for field in ("ipo_date", "out_date"):
            securities[field] = securities[field].map(_date).astype(object)
            securities[field] = securities[field].where(securities[field].notna(), None)
        if daily["date"].isna().any() or daily.duplicated(["code", "date"]).any():
            raise ValueError("research daily dates must be valid and unique per code")
        if securities["code"].duplicated().any() or not len(securities):
            raise ValueError("research securities must be nonempty and unique")
        if any(not main_board(code) for code in securities["code"]):
            raise ValueError("research engine only supports the declared SSE/SZSE main-board prefixes")
        unknown = set(daily["code"]) - set(securities["code"])
        if unknown:
            raise ValueError(f"daily contains unknown securities: {sorted(unknown)}")
        if daily.empty:
            raise ValueError("research daily dataset is empty")
        history_start = min(start, min(daily["date"]))
        report = calendar.ensure_coverage(history_start, end)
        if not report.get("complete"):
            raise ValueError("research_calendar_coverage_insufficient")
        days = calendar.get_trading_days(history_start, end)
        trading_days = [day for day in days if start <= day <= end]
        if not trading_days:
            raise ValueError("no research trading days in requested range")
        calendar_days = set(days)
        extra_dates = sorted(set(daily.loc[daily["date"] <= end, "date"]) - calendar_days)
        if extra_dates:
            raise ValueError(f"research rows outside supplied trading calendar: {extra_dates[:5]}")
        daily = daily[daily["date"] <= end]
        for field in DAILY_FIELDS:
            daily[field] = pd.to_numeric(daily[field], errors="coerce")
        daily.loc[daily["is_suspended"] == 1, ["volume", "amount"]] = 0.
        portal = ResearchDataPortal(
            daily, securities, days, factor_expressions=self.factor_expressions
        )
        for code, security in portal.securities.items():
            if security["ipo_date"] is None:
                ledger.issue(start, code, "ipo_date_unresolved")
        spec = getattr(self.strategy_class, "SPEC", None)
        requirements = ()
        if isinstance(spec, StrategySpec):
            if "a-share" not in spec.markets:
                raise ValueError("research strategy must support a-share")
            requirements = resolve_strategy_data_requirements(spec, self.parameters,
                              supported={"a_share_daily": ("1d", "event_driven")},
                              factor_definitions=self.factor_expressions)
            for requirement in requirements:
                for factor_name in requirement.factors:
                    definition = self.factor_expressions.get(factor_name)
                    if definition is None:
                        definition = get_factor_definition(factor_name)
                    if getattr(definition, "category", None) == "raw_fundamental":
                        raise ValueError("research_strategy_fundamental_factors_unsupported")
                if set(requirement.fields) - set(portal._signals):
                    raise ValueError("research_strategy_fields_unsupported")
            required = max([spec.warmup_bars] + [item.required_bars for item in requirements])
            available = sum(day < start for day in days)
            if available < required:
                raise ValueError(f"research_warmup_calendar_insufficient: required={required}, available={available}")
        frequency = rebalance_frequency or (spec.rebalance_frequency if isinstance(spec, StrategySpec) else "weekly")
        if frequency not in {"daily", "weekly", "monthly"}:
            raise ValueError("unsupported research rebalance frequency")
        # Resolve last sessions using the full supplied calendar, not the
        # requested end date, so a midweek run ending cannot create a signal.
        schedule_days = calendar.get_trading_days(start, date(end.year, 12, 31))
        groups = defaultdict(list)
        for day in schedule_days:
            key = day.isocalendar()[:2] if frequency == "weekly" else (day.year, day.month)
            groups[key].append(day)
        rebalance_days = set(trading_days) if frequency == "daily" else {max(values) for values in groups.values()}
        parsed_actions = self._actions(actions, start, end, set(portal.codes), ledger)
        records, ex_events = defaultdict(list), defaultdict(list)
        for action in parsed_actions:
            if action.record_date and action.record_date < start:
                action.entitlement = 0
            elif action.record_date:
                records[action.record_date].append(action)
            if action.ex_date:
                ex_events[action.ex_date].append(action)
        context = StrategyContext(calendar)
        context.bind_data(portal)
        context.set_portfolio(ledger)
        strategy = self.strategy_class(context, **self.parameters)
        recorder = Recorder(output_dir)
        pending = None
        pending_day = None
        extra_portfolio = []
        position_rows = []
        action_dates = sorted(set(records) | set(ex_events))
        action_cursor = 0
        try:
            strategy.initialize()
            for day in trading_days:
                index = portal._day_indices[day]
                previous_day = days[index - 1] if index else None
                bars = portal.raw_bars(day)
                previous_bars = portal.raw_bars(previous_day)
                for code, security in portal.securities.items():
                    ipo, out = security["ipo_date"], security["out_date"]
                    if ipo and ipo <= day and (out is None or day < out) and code not in bars:
                        ledger.issue(day, code, "active_daily_bar_missing")
                # Run all events up to this session, including off-calendar
                # cash/share settlement; record dates themselves require an
                # observed session close rather than inventing a holding.
                while action_cursor < len(action_dates) and action_dates[action_cursor] <= day:
                    event_day = action_dates[action_cursor]
                    if event_day >= start:
                        for action in ex_events[event_day]:
                            ledger.apply_action(action, event_day)
                        if event_day < day and event_day in records:
                            for action in records[event_day]:
                                if action.entitlement is None:
                                    ledger.issue(event_day, action.code, "record_date_without_session", action_id=action.action_id)
                    action_cursor += 1
                ledger.settle_due(day)
                for code in list(ledger.positions):
                    out = portal.securities[code]["out_date"]
                    if out and out <= day:
                        ledger.delist(code, day)
                ex_codes = {action.code for action in ex_events.get(day, [])}
                for code, bar in bars.items():
                    previous_close = previous_bars.get(code, {}).get("close")
                    preclose = bar.get("preclose")
                    if finite_positive(previous_close) and finite_positive(preclose) and code not in ex_codes:
                        if abs(float(preclose) - float(previous_close)) > max(.011, float(previous_close) * .001):
                            ledger.issue(day, code, "unexplained_preclose_adjustment",
                                         previous_close=float(previous_close), preclose=float(preclose))
                context.set_date(day)
                portal.set_date(previous_day)
                strategy.before_trading()
                eligible = set(portal.eligible(day))
                if pending is not None:
                    for order, trade in ledger.rebalance(pending, bars, previous_bars, eligible, day):
                        recorder.record_order(order)
                        if trade is not None:
                            recorder.record_trade(trade)
                            strategy.on_order_filled(trade)
                    pending = None
                    pending_day = None
                ledger.mark_close(bars, day)
                portal.set_date(day)
                context.set_portfolio(ledger)
                for action in records.get(day, []):
                    ledger.record_entitlement(action, day)
                if day in rebalance_days:
                    output = normalize_strategy_output(strategy.generate_signals(day), spec=spec)
                    target = dict(output.target_weights)
                    unknown = set(target) - set(portal.codes)
                    if unknown:
                        raise ValueError(f"strategy produced unknown research symbols: {sorted(unknown)}")
                    old = {code: pos.market_value / ledger.total_value for code, pos in ledger.positions.items()} if ledger.total_value else {}
                    strategy.on_rebalance(day, old, target)
                    recorder.record_signal(day, target)
                    recorder.record_strategy_output(day, output.diagnostics)
                    pending, pending_day = target, day
                for item in context.drain_diagnostics():
                    recorder.record_strategy_output(item["date"] or day,
                        normalize_strategy_diagnostics({item["key"]: item["value"]}, spec=spec))
                recorder.record_portfolio(day, ledger)
                extra_portfolio.append(dict(date=day, receivable_cash=ledger.receivable_cash))
                for code, pos in ledger.positions.items():
                    position_rows.append(dict(date=day, code=code, shares=pos.shares, avg_cost=pos.avg_cost,
                                              market_value=pos.market_value, weight=pos.market_value / ledger.total_value if ledger.total_value else 0.,
                                              sellable_shares=ledger.sellable(code, day)))
        finally:
            strategy.teardown()
        actual = daily[(daily["date"] >= start) & (daily["date"] <= end)]
        limits = [
            "research_only_not_paper_authorization", "initial_historical_pool_with_dynamic_daily_eligibility",
            "opening_liquidity_uses_previous_session_volume", "transfer_fee_flat_conservative_assumption",
            "dividend_tax_scenario_not_investor_specific_tax_collection", "fractional_bonus_shares_are_floored",
            "derived_main_board_limits_not_exchange_order_book_evidence",
        ]
        limits.extend(sorted({item["reason"] for item in ledger.audit}))
        unconfirmed_bonus_shares = sum(item.get("unconfirmed_bonus_shares", 0.) for item in ledger.audit
                                       if item["reason"] == "bonus_holder_allocation_unverified")
        if unconfirmed_bonus_shares:
            limits.append("unverified_bonus_entitlements_not_included_in_equity")
        metadata = dict(strategy=self.strategy_class.__name__, start_date=str(start), end_date=str(end),
                        initial_capital=initial_capital, final_value=ledger.total_value,
                        total_return=ledger.total_value / initial_capital - 1.,
                        execution_model="research_raw_open_corporate_actions_v1", execution_lag="next_trading_day_open",
                        unfilled_order_policy="expires_after_one_opening_attempt",
                        market="a-share", research_only=True, paper_authorized=False,
                        validated=ledger.validated, issue_count=len(ledger.audit),
                        error_count=sum(item["severity"] == "error" for item in ledger.audit),
                        cost_scenario=self.cost_scenario, cost_model={**ledger.costs,
                            "stamp_duty": {"before_2023_08_28": .001, "from_2023_08_28": .0005},
                            "trade_commission_includes_transfer_fee": True,
                            "fill_tick": "0.01_CNY_ROUND_HALF_UP_after_slippage",
                            "fee_model": "paper_research_assumptions_v1"},
                        data_available_start=str(actual["date"].min()) if not actual.empty else None,
                        data_available_end=str(actual["date"].max()) if not actual.empty else None,
                        cash=ledger.cash, receivable_cash=ledger.receivable_cash, market_value=ledger.market_value,
                        unlisted_bonus_shares=sum(item["shares"] for item in ledger._deliveries),
                        unconfirmed_bonus_shares=unconfirmed_bonus_shares,
                        unpaid_dividend_events=len(ledger._receivables),
                        equity_identity="cash + receivable_cash + raw_marked_economic_shares",
                        universe=portal.codes, universe_count=len(portal.codes),
                        universe_selection="initial_historical_pool_dynamic_eligibility", rebalance_frequency=frequency,
                        strategy_params=dict(strategy.params), strategy_data_requirements=[item.as_dict() for item in requirements],
                        strategy_spec=spec.as_dict() if isinstance(spec, StrategySpec) else None,
                        calendar_content_hash=report.get("content_hash"), calendar_source=report.get("source"),
                        unexecuted_signal_date=str(pending_day) if pending_day else None, limitations=limits)
        for key, value in metadata.items():
            recorder.set_meta(key, value)
        recorder.save()
        output_dir = Path(output_dir)
        portfolio = pd.read_parquet(output_dir / "daily_portfolio.parquet")
        portfolio["receivable_cash"] = [item["receivable_cash"] for item in extra_portfolio]
        portfolio.to_parquet(output_dir / "daily_portfolio.parquet", index=False)
        pd.DataFrame(position_rows, columns=["date", "code", "shares", "avg_cost", "market_value", "weight", "sellable_shares"]).to_parquet(
            output_dir / "daily_positions.parquet", index=False)
        # Always replace even empty outputs; retries must not retain trades
        # or signals from a previous run in the same output directory.
        for name, rows, columns in (
            ("trades", recorder._trades, ["date", "code", "side", "shares", "price", "amount", "commission", "stamp_duty", "slippage"]),
            ("orders", recorder._orders, ["order_id", "date", "code", "side", "shares", "fill_shares", "status", "reject_reason"]),
            ("signals", recorder._signals, ["date", "code", "target_weight"]),
            ("strategy_outputs", recorder._strategy_outputs, ["date", "key", "value_json"]),
        ):
            if not rows:
                pd.DataFrame(columns=columns).to_parquet(output_dir / f"{name}.parquet", index=False)
        (output_dir / "corporate_actions.json").write_text(json.dumps(ledger.corporate_actions, ensure_ascii=False, indent=2, default=str))
        (output_dir / "audit.json").write_text(json.dumps(ledger.audit, ensure_ascii=False, indent=2, default=str))
        return str(output_dir.resolve())

    @staticmethod
    def _actions(frame, start, end, codes, ledger):
        if frame.empty:
            return []
        fields = {"code", "record_date", "ex_date", "pay_date", "stock_date", "cash_ps", "bonus_ratio"}
        if not fields.issubset(frame.columns):
            raise ValueError(f"missing company action fields: {sorted(fields - set(frame.columns))}")
        parsed = []
        for row in frame.to_dict("records"):
            code = canonical_code(row["code"])
            if code not in codes:
                continue
            dates = {field: _date(row[field]) for field in ("record_date", "ex_date", "pay_date", "stock_date")}
            cash, bonus = float(row["cash_ps"]), float(row["bonus_ratio"])
            if not math.isfinite(cash) or not math.isfinite(bonus) or cash < 0 or bonus < 0:
                raise ValueError("company action cash/bonus values must be finite and nonnegative")
            if dates["ex_date"] and not start <= dates["ex_date"] <= end:
                continue
            if not cash and not bonus:
                continue
            verification = row.get("bonus_allocation_verified", True)
            verified = verification is True or (isinstance(verification, np.bool_) and bool(verification))
            parsed.append(dict(code=code, **dates, cash_ps=cash, bonus_ratio=bonus,
                               bonus_allocation_verified=verified))
        parsed.sort(key=lambda row: tuple(str(row[field]) for field in sorted(row)))
        actions = []
        seen = set()
        for i, row in enumerate(parsed):
            # Verification metadata cannot turn duplicate economic events
            # into two separate entitlements or double their cash payment.
            identity = tuple(row[field] for field in sorted(row) if field != "bonus_allocation_verified")
            if identity in seen:
                raise ValueError("duplicate research company action")
            seen.add(identity)
            action = ResearchAction(action_id=f"action-{i:06d}", **row)
            if action.ex_date is None:
                ledger.issue(start, action.code, "corporate_action_ex_date_unresolved", action_id=action.action_id)
            if action.record_date and action.ex_date and action.record_date >= action.ex_date:
                ledger.issue(action.ex_date, action.code, "invalid_record_ex_date_order", action_id=action.action_id)
                action.record_date = None
            actions.append(action)
        return actions
