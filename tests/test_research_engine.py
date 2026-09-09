"""Deterministic raw-price, action-accounting and point-in-time research tests."""
from datetime import date
import json

import numpy as np
import pandas as pd
import pytest

from quant_engine.backtest.research_engine import ResearchBacktestEngine, ResearchDataPortal
from quant_engine.backtest.research_ledger import ResearchAction, ResearchLedger
from quant_engine.backtest.protocol import DataRequirement, StrategySpec
from quant_engine.backtest.strategy import Strategy
from quant_engine.factor.expression import FactorExpressionSpec
from quant_engine.factor.strategy import build_expression_rank_strategy


CODE = "600000.SH"
OTHER = "000001.SZ"


class Calendar:
    def __init__(self):
        self.days = list(pd.bdate_range("2023-12-01", "2024-12-31").date)

    def get_trading_days(self, start, end):
        return [day for day in self.days if start <= day <= end]

    def ensure_coverage(self, start, end):
        return dict(complete=True, source="test:synthetic_calendar", content_hash="test-calendar")


def bar(price=10., volume=1_000_000., preclose=None, **extra):
    return dict(open=price, high=price, low=price, close=price,
                preclose=price if preclose is None else preclose, volume=volume, amount=volume * price,
                turnover_rate=1., is_suspended=False, is_st=False, adjusted_close=price, **extra)


def frames(codes=(CODE,), end="2024-01-12"):
    daily = pd.DataFrame([dict(code=code, date=day, **bar()) for code in codes
                          for day in pd.bdate_range("2023-12-01", end).date])
    securities = pd.DataFrame([dict(code=code, ipo_date="2000-01-01", out_date=None) for code in codes])
    return daily, securities


class BuyAndHold(Strategy):
    def initialize(self):
        self.first = True

    def generate_signals(self, day):
        if self.first:
            self.first = False
            return {CODE: .5}
        portfolio = self.ctx.portfolio
        return {code: pos.market_value / portfolio.total_value for code, pos in portfolio.positions.items()}


def run(tmp_path, daily=None, securities=None, actions=None, strategy=BuyAndHold, scenario="baseline", end=date(2024, 1, 12)):
    if daily is None:
        daily, securities = frames()
    result = ResearchBacktestEngine(strategy, cost_scenario=scenario).run(
        daily=daily, actions=actions if actions is not None else pd.DataFrame(), securities=securities,
        start=date(2024, 1, 3), end=end, calendar=Calendar(), output_dir=str(tmp_path), initial_capital=10000.,
        rebalance_frequency="daily")
    return json.loads((tmp_path / "summary.json").read_text()), pd.read_parquet(tmp_path / "daily_portfolio.parquet")


def acquired_ledger(scenario="baseline"):
    ledger = ResearchLedger(10000., scenario)
    outcomes = ledger.rebalance({CODE: .5}, {CODE: bar()}, {CODE: bar()}, {CODE}, date(2024, 1, 3))
    assert outcomes[0][1] is not None
    ledger.mark_close({CODE: bar()}, date(2024, 1, 3))
    return ledger


def test_raw_execution_is_independent_of_adjustment_base(tmp_path):
    daily, securities = frames()
    first, curve1 = run(tmp_path / "one", daily, securities)
    daily["adjusted_close"] *= 100
    second, curve2 = run(tmp_path / "hundred", daily, securities)
    pd.testing.assert_frame_equal(curve1, curve2)
    trades1 = pd.read_parquet(tmp_path / "one" / "trades.parquet")
    trades2 = pd.read_parquet(tmp_path / "hundred" / "trades.parquet")
    pd.testing.assert_frame_equal(trades1, trades2)
    assert trades1.iloc[0]["price"] == pytest.approx(10.01)
    assert pd.Timestamp(trades1.iloc[0]["date"]).date() == date(2024, 1, 4)
    assert first["final_value"] == second["final_value"]
    assert first["paper_authorized"] is False


def test_dividend_record_ex_receivable_and_later_cash():
    ledger = acquired_ledger()
    action = ResearchAction("cash", CODE, date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 6), None, 1., 0.)
    ledger.record_entitlement(action, date(2024, 1, 3))
    shares = ledger.positions[CODE].shares
    initial_cash, equity = ledger.cash, ledger.total_value
    ledger.apply_action(action, date(2024, 1, 4))
    ledger.mark_close({CODE: bar(9.)}, date(2024, 1, 4))
    assert ledger.cash == initial_cash
    assert ledger.receivable_cash == shares
    assert ledger.total_value == pytest.approx(equity)
    ledger.settle_due(date(2024, 1, 5))
    assert ledger.cash == initial_cash
    # Saturday payment becomes usable at the following session.
    ledger.settle_due(date(2024, 1, 8))
    assert ledger.cash == initial_cash + shares
    assert ledger.receivable_cash == 0
    ledger.mark_close({CODE: bar(18.)}, date(2024, 1, 8))
    assert ledger.total_value == pytest.approx(initial_cash + shares + shares * 18.)


def test_bonus_is_equity_on_ex_date_but_locks_until_listing():
    ledger = acquired_ledger()
    action = ResearchAction("bonus", CODE, date(2024, 1, 3), date(2024, 1, 4), None, date(2024, 1, 8), 0., 1.)
    ledger.record_entitlement(action, date(2024, 1, 3))
    old_shares, equity = ledger.positions[CODE].shares, ledger.total_value
    ledger.apply_action(action, date(2024, 1, 4))
    ledger.mark_close({CODE: bar(5.)}, date(2024, 1, 4))
    assert ledger.positions[CODE].shares == old_shares * 2
    assert ledger.total_value == pytest.approx(equity)
    assert ledger.sellable(CODE, date(2024, 1, 4)) == old_shares
    ledger.settle_due(date(2024, 1, 8))
    assert ledger.sellable(CODE, date(2024, 1, 8)) == old_shares * 2


def test_entitlement_survives_sale_before_ex_date():
    ledger = acquired_ledger()
    action = ResearchAction("cash", CODE, date(2024, 1, 3), date(2024, 1, 5), date(2024, 1, 8), None, 1., 0.)
    ledger.record_entitlement(action, date(2024, 1, 3))
    entitled = action.entitlement
    ledger.rebalance({}, {CODE: bar()}, {CODE: bar()}, {CODE}, date(2024, 1, 4))
    assert CODE not in ledger.positions
    ledger.apply_action(action, date(2024, 1, 5))
    assert ledger.receivable_cash == entitled


def test_current_open_equity_sizes_target_without_stale_close_sales():
    ledger = acquired_ledger()
    # Exact current weight must generate no churn even after an overnight gap.
    opening = {CODE: bar(10.5, preclose=10.)}
    equity = ledger.marked_equity(opening, "open", date(2024, 1, 4))
    target = ledger.positions[CODE].shares * 10.5 / equity
    assert ledger.rebalance({CODE: target}, opening, {CODE: bar()}, {CODE}, date(2024, 1, 4)) == []


def test_capacity_uses_previous_volume_and_cash_never_receivables():
    ledger = ResearchLedger(10000.)
    result = ledger.rebalance({CODE: 1.}, {CODE: bar(volume=100000000)}, {CODE: bar(volume=10000)}, {CODE}, date(2024, 1, 3))
    order, trade = result[0]
    assert trade.shares == 100
    assert order.status.value == "partial"
    assert ledger.cash == pytest.approx(10000. - trade.amount - trade.commission)
    assert ledger.sellable(CODE, date(2024, 1, 3)) == 0
    # Changing only today's total volume never changes the opening fill.
    other = ResearchLedger(10000.)
    other_result = other.rebalance({CODE: 1.}, {CODE: bar(volume=1)}, {CODE: bar(volume=10000)}, {CODE}, date(2024, 1, 3))
    assert other_result[0][1].shares == trade.shares


def test_portal_has_no_future_fill_and_dynamic_universe():
    daily, securities = frames((CODE, OTHER))
    gap = (daily.code == CODE) & (daily.date == date(2024, 1, 4))
    daily = daily[~gap].copy()
    daily.loc[(daily.code == OTHER) & (daily.date == date(2024, 1, 5)), "is_st"] = True
    securities["ipo_date"] = securities["ipo_date"].map(lambda x: pd.Timestamp(x).date())
    portal = ResearchDataPortal(daily, securities, Calendar().get_trading_days(date(2023, 12, 1), date(2024, 1, 12)))
    assert portal.history([CODE], 2, ["close"]).empty
    portal.set_date(date(2024, 1, 5))
    history = portal.history([CODE], 3, ["close", "volume"])
    assert history.index.get_level_values("date").tolist() == [date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)]
    assert np.isnan(history.loc[(CODE, date(2024, 1, 4)), "close"])
    assert OTHER not in portal.universe
    assert CODE in portal.universe


def test_research_engine_supports_point_in_time_panel_factors(tmp_path):
    daily, securities = frames((CODE, OTHER))
    securities["ipo_date"] = securities["ipo_date"].map(lambda value: pd.Timestamp(value).date())
    portal = ResearchDataPortal(
        daily,
        securities,
        Calendar().get_trading_days(date(2023, 12, 1), date(2024, 1, 12)),
    )
    portal.set_date(date(2024, 1, 5))
    values = portal.factor("raw_return_lagged_1_close", date(2024, 1, 5))
    assert values.index.tolist() == sorted([CODE, OTHER])
    assert np.isfinite(values).all()
    values.iloc[0] = 999
    assert portal.factor("raw_return_lagged_1_close", date(2024, 1, 5)).iloc[0] != 999

    class FactorStrategy(BuyAndHold):
        SPEC = StrategySpec(
            id="research-factor-supported-test",
            name="supported factor",
            version="1.0.0",
            description="research engine factor preflight",
            markets=("a-share",),
            data=(DataRequirement(
                "a_share_daily",
                (),
                1,
                factors=("raw_return_lagged_1_close",),
            ),),
        )

        def generate_signals(self, day):
            scores = self.get_factor("raw_return_lagged_1_close", day).dropna()
            return {code: 0.5 / len(scores) for code in scores.index}

    summary, _ = run(tmp_path, daily, securities, strategy=FactorStrategy)
    requirement = summary["strategy_data_requirements"][0]
    assert requirement["factors"] == ["raw_return_lagged_1_close"]
    assert requirement["fields"] == ["close"]


def test_research_engine_rejects_fundamental_factor_without_pit_adapter(tmp_path):
    class FundamentalFactorStrategy(BuyAndHold):
        SPEC = StrategySpec(
            id="research-fundamental-factor-unsupported-test",
            name="unsupported fundamental factor",
            version="1.0.0",
            description="research engine fundamental factor preflight",
            markets=("a-share",),
            data=(DataRequirement(
                "a_share_daily",
                (),
                1,
                factors=("raw_earnings_yield_1_fundamental",),
            ),),
        )

    with pytest.raises(ValueError, match="research_strategy_fundamental_factors_unsupported"):
        run(tmp_path, strategy=FundamentalFactorStrategy)


def test_frozen_expression_strategy_runs_through_research_portal(tmp_path):
    expression = FactorExpressionSpec.from_dict({
        "name": "test_dynamic_reversal",
        "hypothesis": "用于验证冻结表达式策略接缝。",
        "direction": -1,
        "role": "rank",
        "source": "test",
        "expression": {
            "op": "winsorize_zscore",
            "args": [{
                "op": "sub",
                "args": [
                    {
                        "op": "div",
                        "args": [
                            {"op": "delay", "args": [{"field": "close"}], "params": {"periods": 1}},
                            {"op": "delay", "args": [{"field": "close"}], "params": {"periods": 3}},
                        ],
                    },
                    {"constant": 1.0},
                ],
            }],
        },
    })
    strategy = build_expression_rank_strategy(expression)
    codes = tuple(f"6000{i:02d}.SH" for i in range(10))
    history_days = pd.bdate_range("2023-01-02", "2024-01-12").date
    daily = pd.DataFrame([
        {"code": code, "date": day, **bar(price=10 + index + offset * 0.01)}
        for index, code in enumerate(codes)
        for offset, day in enumerate(history_days)
    ])
    securities = pd.DataFrame([
        {"code": code, "ipo_date": "2000-01-01", "out_date": None}
        for code in codes
    ])
    calendar = Calendar()
    calendar.days = list(pd.bdate_range("2022-01-03", "2024-12-31").date)
    result_dir = tmp_path / "dynamic-factor"
    ResearchBacktestEngine(
        strategy,
        factor_expressions={expression.name: expression},
    ).run(
        daily=daily,
        actions=pd.DataFrame(),
        securities=securities,
        start=date(2024, 1, 3),
        end=date(2024, 1, 12),
        calendar=calendar,
        output_dir=str(result_dir),
        rebalance_frequency="weekly",
    )
    summary = json.loads((result_dir / "summary.json").read_text())
    requirement = summary["strategy_data_requirements"][0]
    assert requirement["factors"] == [expression.name]
    assert requirement["required_bars"] == 252
    assert summary["strategy_spec"]["extensions"]["factor_expression_hash"] == expression.expression_hash
    signals = pd.read_parquet(result_dir / "signals.parquet")
    assert set(signals["code"]) == set(codes)


def test_dynamic_factor_masks_historical_st_and_suspended_rows():
    daily, securities = frames((CODE, OTHER))
    securities["ipo_date"] = securities["ipo_date"].map(
        lambda value: pd.Timestamp(value).date()
    )
    daily.loc[
        (daily.code == CODE) & (daily.date == date(2024, 1, 4)), "is_st"
    ] = True
    expression = FactorExpressionSpec.from_dict({
        "name": "test_eligible_rolling_mean",
        "hypothesis": "测试历史逐日资格掩码。",
        "direction": 1,
        "role": "rank",
        "source": "test",
        "expression": {
            "op": "ts_mean",
            "args": [{"field": "close"}],
            "params": {"window": 2, "min_periods": 2},
        },
    })
    portal = ResearchDataPortal(
        daily,
        securities,
        Calendar().get_trading_days(date(2023, 12, 1), date(2024, 1, 12)),
        factor_expressions={expression.name: expression},
    )
    portal.set_date(date(2024, 1, 5))
    factor = portal.factor(expression.name, date(2024, 1, 5))
    assert np.isnan(factor[CODE])
    assert np.isfinite(factor[OTHER])


def test_strategy_observes_prior_session_before_trading_and_current_close_at_signal(tmp_path):
    seen = []
    class Observe(BuyAndHold):
        def before_trading(self):
            history = self.ctx.history(lookback=1, fields=["close"])
            seen.append(("before", self.ctx.current_date, history.index.get_level_values("date").max()))
        def generate_signals(self, day):
            history = self.ctx.history(lookback=1, fields=["close"])
            seen.append(("signal", day, history.index.get_level_values("date").max()))
            return super().generate_signals(day)
    summary, curve = run(tmp_path, strategy=Observe)
    for stage, day, observed in seen:
        assert observed < day if stage == "before" else observed == day
    expected = curve.cash + curve.market_value + curve.receivable_cash
    np.testing.assert_allclose(curve.total_value, expected)
    expected_returns = curve.total_value.pct_change().fillna(curve.iloc[0].total_value / 10000. - 1)
    np.testing.assert_allclose(curve.daily_return, expected_returns)
    assert summary["validated"] is True


@pytest.mark.parametrize("missing,reason", [("record_date", "corporate_action_record_date_unresolved"),
                                         ("stock_date", "bonus_listing_date_unresolved")])
def test_incomplete_held_actions_are_not_validated(tmp_path, missing, reason):
    action = dict(code=CODE, record_date="2024-01-04", ex_date="2024-01-05", pay_date=None,
                  stock_date="2024-01-08", cash_ps=0., bonus_ratio=1.)
    action[missing] = None
    summary, _ = run(tmp_path, actions=pd.DataFrame([action]))
    assert summary["validated"] is False
    assert reason in summary["limitations"]


def test_delisting_stress_is_reported_and_not_validated(tmp_path):
    daily, securities = frames()
    securities.loc[0, "out_date"] = "2024-01-08"
    daily = daily[daily.date < date(2024, 1, 8)]
    summary, curve = run(tmp_path, daily, securities)
    assert summary["validated"] is False
    assert "delisting_zero_recovery_stress" in summary["limitations"]
    assert curve.iloc[-1].market_value == 0


def test_stamp_duty_and_st_limit_effective_dates():
    ledger = ResearchLedger(10000.)
    from quant_engine.backtest.types import OrderSide
    assert ledger._fees(10000., OrderSide.SELL, date(2023, 8, 25))[2] == 10.
    assert ledger._fees(10000., OrderSide.SELL, date(2023, 8, 28))[2] == 5.
    st = {**bar(), "is_st": True}
    assert ledger._limits(st, date(2026, 7, 3)) == (9.5, 10.5)
    assert ledger._limits(st, date(2026, 7, 6)) == (9., 11.)


def test_stress_dividend_tax_and_fees_are_explicit():
    ledger = acquired_ledger("stress")
    action = ResearchAction("tax", CODE, date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5), None, 1., 0.)
    ledger.record_entitlement(action, date(2024, 1, 3))
    ledger.apply_action(action, date(2024, 1, 4))
    assert ledger.receivable_cash == action.entitlement * .8
    assert ledger.costs["participation_rate"] == .005


def test_engine_dividend_equity_bridge_and_payment(tmp_path):
    daily, securities = frames()
    after = daily.date >= date(2024, 1, 5)
    daily.loc[after, ["open", "high", "low", "close", "preclose"]] = 9.
    actions = pd.DataFrame([dict(code=CODE, record_date="2024-01-04", ex_date="2024-01-05",
                                 pay_date="2024-01-06", stock_date=None, cash_ps=1., bonus_ratio=0.)])
    summary, curve = run(tmp_path, daily, securities, actions)
    curve.index = pd.to_datetime(curve.date).dt.date
    record, ex, paid = [curve.loc[date(2024, 1, day)] for day in (4, 5, 8)]
    assert ex.total_value == pytest.approx(record.total_value)
    assert ex.cash == record.cash
    assert ex.receivable_cash == 500.
    assert paid.cash == pytest.approx(record.cash + 500.)
    assert paid.receivable_cash == 0.
    assert summary["validated"] is True


def test_engine_bonus_lock_blocks_sale_until_listing(tmp_path):
    class Liquidate(BuyAndHold):
        def generate_signals(self, day):
            return {CODE: .5} if day == date(2024, 1, 3) else {}
    daily, securities = frames()
    daily.loc[daily.date >= date(2024, 1, 5), ["open", "high", "low", "close", "preclose"]] = 5.
    actions = pd.DataFrame([dict(code=CODE, record_date="2024-01-04", ex_date="2024-01-05",
                                 pay_date=None, stock_date="2024-01-09", cash_ps=0., bonus_ratio=1.)])
    summary, _ = run(tmp_path, daily, securities, actions, strategy=Liquidate)
    trades = pd.read_parquet(tmp_path / "trades.parquet")
    sales = trades[trades.side == "sell"]
    assert pd.to_datetime(sales.date).dt.date.tolist() == [date(2024, 1, 5), date(2024, 1, 9)]
    assert sales.shares.tolist() == [500, 500]
    assert summary["validated"] is True


def test_missing_active_bar_is_audited_but_prelisting_gaps_are_not(tmp_path):
    daily, securities = frames()
    daily = daily[daily.date != date(2024, 1, 10)]
    summary, _ = run(tmp_path, daily, securities)
    assert summary["validated"] is False
    assert "active_daily_bar_missing" in summary["limitations"]


def test_suspended_history_has_zero_volume_and_amount(tmp_path):
    seen = []
    class Inspect(BuyAndHold):
        def generate_signals(self, day):
            if day == date(2024, 1, 5):
                seen.append(self.ctx.history([CODE], lookback=1, fields=["volume", "amount"]).iloc[0].tolist())
            return super().generate_signals(day)
    daily, securities = frames()
    daily.loc[daily.date == date(2024, 1, 5), "is_suspended"] = True
    run(tmp_path, daily, securities, strategy=Inspect)
    assert seen == [[0., 0.]]


def test_protocol_default_parameter_resolves_dynamic_history_requirement(tmp_path):
    from quant_engine.backtest.protocol import DataRequirement, ParameterSpec, ParameterType, StrategySpec
    observed = []
    class DefaultLookback(BuyAndHold):
        SPEC = StrategySpec(id="research-default-test", name="defaults", version="2", description="test",
            markets=("a-share",), parameters=(ParameterSpec("lookback", "window", ParameterType.INTEGER, 3),),
            data=(DataRequirement("a_share_daily", ("close",), 2, lookback_parameter="lookback"),))
        def generate_signals(self, day):
            observed.append((self.params["lookback"], len(self.ctx.history([CODE], lookback=self.params["lookback"], fields=["close"]))))
            return super().generate_signals(day)
    summary, _ = run(tmp_path, strategy=DefaultLookback)
    assert observed and set(observed) == {(3, 3)}
    assert summary["strategy_params"] == {"lookback": 3}
    assert summary["strategy_data_requirements"][0]["required_bars"] == 3


def test_slippage_cannot_cross_limit_and_missing_reference_is_audited():
    ledger = ResearchLedger(10000., "stress")
    opening = bar(10.99, preclose=10.)
    result = ledger.rebalance({CODE: .5}, {CODE: opening}, {CODE: bar()}, {CODE}, date(2024, 1, 3))
    assert result[0][1] is None
    assert result[0][0].reject_reason == "slippage_exceeds_price_limit"
    result = ledger.rebalance({CODE: .5}, {CODE: bar(preclose=0)}, {CODE: bar()}, {CODE}, date(2024, 1, 3))
    assert result[0][1] is None
    assert not ledger.validated


def test_receivable_is_equity_but_cannot_fund_buys():
    ledger = acquired_ledger()
    action = ResearchAction("unpaid", CODE, date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 10), None, 100., 0.)
    ledger.record_entitlement(action, date(2024, 1, 3))
    ledger.apply_action(action, date(2024, 1, 4))
    available_cash = ledger.cash
    result = ledger.rebalance({CODE: 1.}, {CODE: bar()}, {CODE: bar()}, {CODE}, date(2024, 1, 4))
    trade = result[0][1]
    assert trade is not None
    assert trade.amount + trade.commission <= available_cash
    assert ledger.cash >= 0
    assert ledger.receivable_cash == action.entitlement * 100.


def test_fully_liquidating_bonus_odd_lot_is_allowed():
    ledger = acquired_ledger()
    action = ResearchAction("odd", CODE, date(2024, 1, 3), date(2024, 1, 4), None, date(2024, 1, 5), 0., .05)
    ledger.record_entitlement(action, date(2024, 1, 3))
    ledger.apply_action(action, date(2024, 1, 4))
    result = ledger.rebalance({}, {CODE: bar()}, {CODE: bar()}, {CODE}, date(2024, 1, 5))
    assert result[0][1].shares == 525
    assert not ledger.positions


def test_slippage_fill_is_rounded_to_exchange_cent_tick():
    ledger = ResearchLedger(10000.)
    result = ledger.rebalance({CODE: .5}, {CODE: bar(9.)}, {CODE: bar(9.)}, {CODE}, date(2024, 1, 3))
    assert result[0][1].price == 9.01
    result = ledger.rebalance({}, {CODE: bar(9.)}, {CODE: bar(9.)}, {CODE}, date(2024, 1, 4))
    assert result[0][1].price == 8.99


def test_unverified_holder_bonus_is_not_booked_but_cash_is_processed(tmp_path):
    daily, securities = frames()
    daily.loc[daily.date >= date(2024, 1, 5), ["open", "high", "low", "close", "preclose"]] = 9.5
    actions = pd.DataFrame([dict(code=CODE, record_date="2024-01-04", ex_date="2024-01-05",
                                 pay_date="2024-01-08", stock_date="2024-01-08", cash_ps=.5,
                                 bonus_ratio=1.5, bonus_allocation_verified=False)])
    summary, curve = run(tmp_path, daily, securities, actions)
    positions = pd.read_parquet(tmp_path / "daily_positions.parquet")
    assert positions.shares.max() == 500
    assert summary["validated"] is False
    assert "bonus_holder_allocation_unverified" in summary["limitations"]
    assert "unverified_bonus_entitlements_not_included_in_equity" in summary["limitations"]
    assert summary["unconfirmed_bonus_shares"] == 750.
    events = json.loads((tmp_path / "corporate_actions.json").read_text())
    ex = next(item for item in events if item["stage"] == "ex")
    assert ex["bonus_shares_booked"] == 0
    assert ex["unconfirmed_bonus_shares"] == 750.
    assert ex["cash_receivable"] == 250.
    assert next(item for item in events if item["stage"] == "pay")["cash"] == 250.
    np.testing.assert_allclose(curve.total_value, curve.cash + curve.market_value + curve.receivable_cash)


def test_unverified_bonus_without_record_day_holding_does_not_add_error(tmp_path):
    # Initial position is bought Jan 4, after the Jan 3 record-date close.
    actions = pd.DataFrame([dict(code=CODE, record_date="2024-01-03", ex_date="2024-01-05",
                                 pay_date=None, stock_date="2024-01-08", cash_ps=0.,
                                 bonus_ratio=2., bonus_allocation_verified=False)])
    summary, _ = run(tmp_path, actions=actions)
    assert summary["validated"] is True
    assert "bonus_holder_allocation_unverified" not in summary["limitations"]


def test_unverified_bonus_uses_record_holdings_even_after_they_are_sold():
    ledger = acquired_ledger()
    action = ResearchAction("uncertain", CODE, date(2024, 1, 3), date(2024, 1, 5),
                            date(2024, 1, 8), None, 1., 2., bonus_allocation_verified=False)
    ledger.record_entitlement(action, date(2024, 1, 3))
    ledger.rebalance({}, {CODE: bar()}, {CODE: bar()}, {CODE}, date(2024, 1, 4))
    ledger.apply_action(action, date(2024, 1, 5))
    assert not ledger.positions
    assert ledger.receivable_cash == 500.
    assert not ledger.validated
    assert ledger.audit[0]["unconfirmed_bonus_shares"] == 1000.


def test_verification_flag_cannot_duplicate_economic_action(tmp_path):
    action = dict(code=CODE, record_date="2024-01-04", ex_date="2024-01-05",
                  pay_date="2024-01-08", stock_date="2024-01-08", cash_ps=1., bonus_ratio=1.)
    actions = pd.DataFrame([{**action, "bonus_allocation_verified": True},
                            {**action, "bonus_allocation_verified": False}])
    with pytest.raises(ValueError, match="duplicate research company action"):
        run(tmp_path, actions=actions)
