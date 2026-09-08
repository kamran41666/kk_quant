from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from quant_engine.backtest.context import StrategyContext
from quant_engine.backtest.protocol import (
    StrategyProtocolError,
    normalize_strategy_output,
)
from quant_engine.backtest.registry import strategy_registry
from quant_engine.data.calendar import TradingCalendar
from server.services.strategy_evidence import strategy_research_gate_passed
from strategies.research_low_volatility import ResearchLowVolatilityStrategy
from strategies.research_momentum import ResearchMomentumStrategy
from strategies.research_short_reversal import ResearchShortReversalStrategy


class _WindowPortal:
    """Small point-in-time portal with the same tail-window contract as the engine."""

    def __init__(self, history: pd.DataFrame, as_of: date, universe=None):
        self._history = history
        self._as_of = as_of
        self._universe = universe
        self.calls: list[tuple[tuple[str, ...] | None, int, tuple[str, ...]]] = []

    @property
    def universe(self):
        if self._universe is not None:
            return tuple(self._universe)
        return tuple(sorted(set(self._history.index.get_level_values("code"))))

    def history(self, codes, lookback, fields):
        self.calls.append((tuple(codes) if codes else None, lookback, tuple(fields)))
        frame = self._history
        dates = pd.to_datetime(frame.index.get_level_values("date")).date
        frame = frame.loc[dates <= self._as_of]
        if codes:
            frame = frame.loc[frame.index.get_level_values("code").isin(codes)]
        return frame[list(fields)].groupby(level="code", group_keys=False).tail(lookback)

    def current(self, code, field):
        frame = self.history([code], 1, [field])
        return float(frame.iloc[-1][field])


def _history(series: dict[str, np.ndarray], *, start="2023-01-02") -> pd.DataFrame:
    rows = []
    for code, closes in series.items():
        dates = pd.bdate_range(start, periods=len(closes))
        for day, close in zip(dates, closes, strict=True):
            rows.append({
                "code": code,
                "date": day.date(),
                "close": float(close),
                "volume": 1_000_000.0,
                "amount": 50_000_000.0,
            })
    return pd.DataFrame(rows).set_index(["code", "date"]).sort_index()


def _run(strategy_class, history, **params):
    as_of = max(history.index.get_level_values("date"))
    portal = _WindowPortal(history, as_of)
    context = StrategyContext(TradingCalendar())
    context.bind_data(portal)
    context.set_date(as_of)
    strategy = strategy_class(context, **params)
    strategy.initialize()
    output = normalize_strategy_output(strategy.generate_signals(as_of), spec=strategy.SPEC)
    return output, portal


def test_candidate_specs_are_bounded_discoverable_and_research_gated():
    classes = (
        ResearchLowVolatilityStrategy,
        ResearchShortReversalStrategy,
        ResearchMomentumStrategy,
    )
    entries = {entry.spec.id: entry for entry in strategy_registry.discover()}
    assert set(entries) >= {
        "research-low-volatility",
        "research-short-reversal",
        "research-momentum",
    }
    for strategy_class in classes:
        spec = strategy_class.SPEC
        params = spec.validate_params()
        assert spec.version == "0.1.1"
        assert spec.data[0].lookback == 252
        assert params["top_n"] == 30
        assert params["min_amount"] == 20_000_000.0
        assert params["gross_exposure"] == 0.9
        assert spec.max_gross_exposure == 0.9
        assert spec.extensions["research_gate_required"] is True
        assert spec.extensions["research_engine_required"] is True
        assert "research_gate_result" not in spec.extensions
        assert spec.research_document == "docs/research-runs/ashare-multi-strategy-research.md"
        implementation = f"{strategy_class.__module__}.{strategy_class.__name__}"
        assert strategy_research_gate_passed(implementation, params) is False

    assert ResearchLowVolatilityStrategy.SPEC.data[0].resolved_lookback(
        ResearchLowVolatilityStrategy.SPEC.validate_params()
    ) == 253
    assert ResearchShortReversalStrategy.SPEC.data[0].resolved_lookback(
        ResearchShortReversalStrategy.SPEC.validate_params()
    ) == 252
    assert ResearchMomentumStrategy.SPEC.data[0].resolved_lookback(
        ResearchMomentumStrategy.SPEC.validate_params()
    ) == 253
    assert ResearchLowVolatilityStrategy.SPEC.data[0].resolved_lookback(
        ResearchLowVolatilityStrategy.SPEC.validate_params({"lookback": 60})
    ) == 252
    assert ResearchMomentumStrategy.SPEC.data[0].resolved_lookback(
        ResearchMomentumStrategy.SPEC.validate_params({"lookback": 126})
    ) == 252


def test_parameter_bounds_and_momentum_relationship_are_enforced():
    with pytest.raises(StrategyProtocolError, match="lookback must be >= 60"):
        ResearchLowVolatilityStrategy.SPEC.validate_params({"lookback": 59})
    with pytest.raises(StrategyProtocolError, match="gross_exposure must be <= 0.9"):
        ResearchShortReversalStrategy.SPEC.validate_params({"gross_exposure": 1.0})

    context = StrategyContext(TradingCalendar())
    strategy = ResearchMomentumStrategy(context, lookback=126, skip=126)
    with pytest.raises(ValueError, match="skip must be lower than lookback"):
        strategy.initialize()


def test_low_volatility_prefers_lower_realized_volatility_and_reads_pool_once():
    periods = 253
    low_returns = np.where(np.arange(periods - 1) % 2 == 0, 0.001, -0.001)
    high_returns = np.where(np.arange(periods - 1) % 2 == 0, 0.02, -0.02)
    low = 10.0 * np.cumprod(np.r_[1.0, 1.0 + low_returns])
    high = 10.0 * np.cumprod(np.r_[1.0, 1.0 + high_returns])
    output, portal = _run(
        ResearchLowVolatilityStrategy,
        _history({"000002.SZ": high, "000001.SZ": low}),
        top_n=1,
    )
    assert dict(output.target_weights) == {"000001.SZ": 0.1}
    assert output.diagnostics == {
        "eligible_count": 2,
        "selected_count": 1,
        "gross_exposure": 0.1,
    }
    assert portal.calls == [(("000001.SZ", "000002.SZ"), 253, ("close", "volume", "amount"))]


def test_short_reversal_prefers_the_trailing_loser():
    periods = 120
    winner = 10.0 * np.power(1.002, np.arange(periods))
    loser = 10.0 * np.power(0.998, np.arange(periods))
    output, portal = _run(
        ResearchShortReversalStrategy,
        _history({"000001.SZ": winner, "000002.SZ": loser}),
        top_n=1,
    )
    assert list(output.target_weights) == ["000002.SZ"]
    assert portal.calls == [(("000001.SZ", "000002.SZ"), 252, ("close", "volume", "amount"))]


def test_skipped_momentum_prefers_the_long_term_winner():
    periods = 253
    winner = 10.0 * np.power(1.002, np.arange(periods))
    loser = 10.0 * np.power(0.998, np.arange(periods))
    output, portal = _run(
        ResearchMomentumStrategy,
        _history({"000002.SZ": loser, "000001.SZ": winner}),
        top_n=1,
    )
    assert list(output.target_weights) == ["000001.SZ"]
    assert portal.calls == [(("000001.SZ", "000002.SZ"), 253, ("close", "volume", "amount"))]


def test_skipped_momentum_requires_valid_signal_endpoints():
    periods = 253
    valid = 10.0 * np.power(1.001, np.arange(periods))
    invalid_endpoint = 10.0 * np.power(1.003, np.arange(periods))
    frame = _history({"000001.SZ": valid, "000002.SZ": invalid_endpoint})
    endpoint = frame.xs("000002.SZ").index[-22]
    frame.loc[("000002.SZ", endpoint), "volume"] = 0.0

    output, _ = _run(ResearchMomentumStrategy, frame, top_n=2)
    assert dict(output.target_weights) == {"000001.SZ": 0.1}
    assert output.diagnostics["eligible_count"] == 1


def test_suspended_carried_prices_do_not_create_fake_low_volatility():
    periods = 253
    normal_returns = np.where(np.arange(periods - 1) % 2 == 0, 0.005, -0.005)
    volatile_returns = np.where(np.arange(periods - 1) % 2 == 0, 0.03, -0.03)
    normal = 10.0 * np.cumprod(np.r_[1.0, 1.0 + normal_returns])
    suspended = 10.0 * np.cumprod(np.r_[1.0, 1.0 + volatile_returns])
    frame = _history({"000001.SZ": normal, "000002.SZ": suspended})
    suspended_dates = frame.xs("000002.SZ").index[10:62:2]
    frame.loc[("000002.SZ", suspended_dates), "volume"] = 0.0

    output, _ = _run(ResearchLowVolatilityStrategy, frame, top_n=1)
    assert dict(output.target_weights) == {"000001.SZ": 0.1}
    assert output.diagnostics["eligible_count"] == 1


def test_listing_age_and_liquidity_filters_and_partial_cash_weighting():
    good = np.linspace(12.0, 10.0, 120)
    illiquid = np.linspace(11.0, 10.0, 120)
    young = np.linspace(13.0, 10.0, 119)
    frame = _history({"000001.SZ": good, "000002.SZ": illiquid, "000003.SZ": young})
    frame.loc[("000002.SZ", slice(None)), "amount"] = 1_000_000.0

    output, _ = _run(ResearchShortReversalStrategy, frame)
    assert dict(output.target_weights) == {"000001.SZ": 0.1}
    assert output.diagnostics == {
        "eligible_count": 1,
        "selected_count": 1,
        "gross_exposure": 0.1,
    }


@pytest.mark.parametrize(
    "strategy_class,params",
    [
        (ResearchLowVolatilityStrategy, {"lookback": 60}),
        (ResearchShortReversalStrategy, {"lookback": 20}),
    ],
)
def test_short_signal_windows_use_a_separate_252_day_eligibility_window(
    strategy_class,
    params,
):
    closes = 10.0 * np.power(1.001, np.arange(252))
    frame = _history({"000001.SZ": closes})
    # One old suspension is inside the former 120-row fetch but outside both
    # signal windows and the recent-20 check. It must not exclude the stock.
    suspended_day = frame.xs("000001.SZ").index[150]
    frame.loc[("000001.SZ", suspended_day), "volume"] = 0.0

    output, portal = _run(strategy_class, frame, top_n=1, **params)
    assert dict(output.target_weights) == {"000001.SZ": 0.1}
    assert portal.calls == [(("000001.SZ",), 252, ("close", "volume", "amount"))]


def test_only_the_latest_252_days_can_satisfy_the_120_day_eligibility_floor():
    closes = 10.0 * np.power(1.001, np.arange(505))
    frame = _history({"000001.SZ": closes})
    recent_252 = frame.xs("000001.SZ").index[-252:]
    frame.loc[("000001.SZ", recent_252[:133]), "volume"] = 0.0

    output, portal = _run(
        ResearchMomentumStrategy,
        frame,
        lookback=504,
        skip=21,
        top_n=1,
    )
    assert output.target_weights == {}
    assert output.diagnostics["eligible_count"] == 0
    assert portal.calls == [(("000001.SZ",), 505, ("close", "volume", "amount"))]


def test_tied_scores_are_ordered_by_code_and_full_pool_reaches_target_gross():
    flat = np.full(120, 10.0)
    frame = _history({f"0000{index:02d}.SZ": flat for index in range(10, 0, -1)})
    output, _ = _run(ResearchShortReversalStrategy, frame, top_n=10)
    assert list(output.target_weights) == sorted(output.target_weights)
    assert all(weight == pytest.approx(0.09) for weight in output.target_weights.values())
    assert output.diagnostics["gross_exposure"] == pytest.approx(0.9)


@pytest.mark.parametrize(
    "strategy_class,periods",
    [
        (ResearchLowVolatilityStrategy, 253),
        (ResearchShortReversalStrategy, 120),
        (ResearchMomentumStrategy, 253),
    ],
)
def test_dynamic_universe_is_the_only_signal_pool(strategy_class, periods):
    eligible = 10.0 * np.power(1.001, np.arange(periods))
    excluded = 10.0 * np.power(0.99, np.arange(periods))
    frame = _history({"000001.SZ": eligible, "000002.SZ": excluded})
    as_of = max(frame.index.get_level_values("date"))
    portal = _WindowPortal(frame, as_of, universe=("000001.SZ",))
    context = StrategyContext(TradingCalendar())
    context.bind_data(portal)
    strategy = strategy_class(context, top_n=1)
    strategy.initialize()

    output = strategy.generate_signals(as_of)
    assert dict(output.target_weights) == {"000001.SZ": 0.1}
    assert portal.calls[0][0] == ("000001.SZ",)
    assert len(portal.calls) == 1


@pytest.mark.parametrize(
    "strategy_class",
    [ResearchLowVolatilityStrategy, ResearchShortReversalStrategy, ResearchMomentumStrategy],
)
def test_empty_dynamic_universe_does_not_request_full_history(strategy_class):
    frame = _history({"000001.SZ": np.full(253, 10.0)})
    as_of = max(frame.index.get_level_values("date"))
    portal = _WindowPortal(frame, as_of, universe=())
    context = StrategyContext(TradingCalendar())
    context.bind_data(portal)
    strategy = strategy_class(context)
    strategy.initialize()

    output = strategy.generate_signals(as_of)
    assert output.target_weights == {}
    assert output.diagnostics == {
        "eligible_count": 0,
        "selected_count": 0,
        "gross_exposure": 0.0,
    }
    assert portal.calls == []


def test_portal_window_prevents_future_rows_from_affecting_signal():
    periods = 121
    first = np.linspace(12.0, 10.0, periods)
    second = np.linspace(10.0, 12.0, periods)
    frame = _history({"000001.SZ": first, "000002.SZ": second})
    as_of = sorted(set(frame.index.get_level_values("date")))[-2]
    portal = _WindowPortal(frame, as_of)
    context = StrategyContext(TradingCalendar())
    context.bind_data(portal)
    context.set_date(as_of)
    strategy = ResearchShortReversalStrategy(context, top_n=1)
    strategy.initialize()
    before = strategy.generate_signals(as_of)

    future_date = max(frame.index.get_level_values("date"))
    frame.loc[("000001.SZ", future_date), "close"] = 10_000.0
    frame.loc[("000002.SZ", future_date), "close"] = 0.01
    after = strategy.generate_signals(as_of)
    assert before.target_weights == after.target_weights == {"000001.SZ": 0.1}
    assert len(portal.calls) == 2
