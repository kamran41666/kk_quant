from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from quant_engine.backtest.engine import BacktestEngine
from quant_engine.backtest.protocol import normalize_strategy_output
from quant_engine.backtest.registry import strategy_registry
from strategies.cycle_of_price_action import CycleOfPriceActionStrategy, _ActiveSignal


class _Context:
    def __init__(self, history: pd.DataFrame):
        self._history = history
        self.universe = list(dict.fromkeys(history.index.get_level_values("code")))
        self.portfolio = None

    def history(self, codes=None, *, lookback=30, fields=("close",)):
        selected = self._history
        if codes:
            selected = selected.loc[selected.index.get_level_values("code").isin(codes)]
        return selected[list(fields)].groupby(level="code", group_keys=False).tail(lookback)

    def log(self, _message):
        pass


def _daily_frame(periods: int = 320) -> pd.DataFrame:
    dates = pd.bdate_range("2023-01-03", periods=periods)
    rows = []
    for code, slope in (("600001.SH", 0.002), ("000001.SZ", -0.0002)):
        closes = 10.0 * np.power(1.0 + slope, np.arange(periods))
        for index, (day, close) in enumerate(zip(dates, closes, strict=True)):
            volume = 1_000_000.0 * (1.3 if index % 30 == 0 else 1.0)
            rows.append({
                "code": code,
                "date": day.date(),
                "open": close * 0.998,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": volume,
                "amount": volume * close,
                "turnover_rate": 1.0,
                "up_limit": close * 1.1,
                "down_limit": close * 0.9,
                "is_suspended": False,
            })
    return pd.DataFrame(rows).set_index(["code", "date"]).sort_index()


def _strategy(history: pd.DataFrame, **params) -> CycleOfPriceActionStrategy:
    resolved = {
        "ablation_stage": "S1",
        "leader_quantile": 0.5,
        "history_bars": 280,
        "min_avg_amount": 0.0,
        "risk_on_breadth": 0.5,
        **params,
    }
    strategy = CycleOfPriceActionStrategy(_Context(history), **resolved)
    strategy.initialize()
    return strategy


def test_copa_strategy_is_discoverable_with_frozen_v2_contract():
    entry = strategy_registry.load(
        "strategies.cycle_of_price_action.CycleOfPriceActionStrategy"
    )
    assert entry.spec.id == "cycle-of-price-action-price-only"
    assert entry.spec.version == "1.0.0"
    assert entry.spec.rebalance_frequency == "daily"
    assert entry.spec.extensions["research_status"] == "blocked_by_research_gate"
    assert entry.spec.extensions["research_gate_required"] is True
    assert entry.spec.max_gross_exposure == 0.9


def test_rvol_uses_only_prior_twenty_sessions():
    history = _daily_frame(280)
    strategy = _strategy(history)
    code_frame = history.xs("600001.SH", level="code").copy()
    code_frame.loc[code_frame.index[-21:-1], "volume"] = 100.0
    code_frame.loc[code_frame.index[-1], "volume"] = 1_000.0
    features = strategy._features(code_frame)
    assert features.iloc[-1]["rvol"] == 10.0


def test_market_regime_requires_two_consecutive_confirmations():
    breadth = pd.Series([0.60, 0.60, 0.30, 0.30, 0.50, 0.50])
    states = CycleOfPriceActionStrategy._confirmed_regimes(breadth, 0.55, 0.40)
    assert states.tolist() == [
        "neutral", "risk-on", "risk-on", "risk-off", "risk-off", "neutral",
    ]


def test_replay_enforces_exit_and_three_session_cooldown():
    history = _daily_frame(280)
    strategy = _strategy(history, ablation_stage="S2", cooldown_bars=3)
    index = pd.RangeIndex(8)
    frame = pd.DataFrame({
        "close": [10.0] * 8,
        "high": [10.2] * 8,
        "atr": [1.0] * 8,
        "ema20": [9.5] * 8,
        "return63": [0.2] * 8,
        "return63_percentile": [1.0] * 8,
        "leader": [True] * 8,
        "regime": ["risk-on"] * 8,
        "long_trend": [False] * 8,
        "wedge_pop": [True, False, False, False, True, True, True, False],
        "crossback": [False] * 8,
        "base_break": [False] * 8,
        "weak_exit": [False, False, True, False, False, False, False, False],
        "wedge_drop": [False] * 8,
        "exhaustion": [False] * 8,
        "base_low10": [9.0] * 8,
        "prior_high10": [9.8] * 8,
    }, index=index)
    active, _ = strategy._replay("600001.SH", frame)
    assert active is not None
    assert active.entry_index == 6


def test_s3_crossback_add_requires_actual_fill_profit():
    strategy = _strategy(_daily_frame(280), ablation_stage="S3")
    frame = pd.DataFrame({
        "close": [10.0, 10.0, 10.1, 10.1],
        "high": [12.0, 10.1, 10.2, 10.2],
        "low": [9.8] * 4,
        "atr": [1.0] * 4,
        "ema20": [9.5] * 4,
        "return63": [0.2] * 4,
        "return63_percentile": [1.0] * 4,
        "leader": [True] * 4,
        "regime": ["risk-on"] * 4,
        "long_trend": [False] * 4,
        "wedge_pop": [True, False, False, False],
        "crossback": [False, False, True, False],
        "base_break": [False] * 4,
        "weak_exit": [False] * 4,
        "wedge_drop": [False] * 4,
        "exhaustion": [False] * 4,
        "base_low10": [9.0] * 4,
        "prior_high10": [9.8] * 4,
    })
    active, _ = strategy._replay("600001.SH", frame)
    assert active is not None
    assert active.signal == "ema-crossback-add"
    initial_weight = strategy._weight(active)
    confirmed_weight = strategy._weight(active, actual_entry=10.0, current_close=10.6)
    assert confirmed_weight == pytest.approx(initial_weight * 2.0)
    without_crossback = _ActiveSignal(
        **{**active.__dict__, "signal": "wedge-pop"}
    )
    assert strategy._weight(
        without_crossback,
        actual_entry=10.0,
        current_close=10.6,
    ) == initial_weight


def test_signal_specific_stop_formulas_match_frozen_rules():
    row = pd.Series({"low": 9.2, "ema20": 10.0, "atr": 1.0, "base_low10": 9.0})
    assert CycleOfPriceActionStrategy._entry_stop("wedge-pop", row) == 9.0
    assert CycleOfPriceActionStrategy._entry_stop("ema-crossback", row) == 9.2
    assert CycleOfPriceActionStrategy._entry_stop("base-break", row) == 9.5


def test_s2_s4_event_marking_covers_wedge_crossback_and_base_break():
    strategy = _strategy(_daily_frame(280), ablation_stage="S5")
    frame = pd.DataFrame({
        "reset_seen": [False] * 25,
        "tight5": [False] * 25,
        "close": [10.0] * 25,
        "prior_high10": [9.5] * 25,
        "ema10": [9.8] * 25,
        "ema20": [9.7] * 25,
        "rvol": [1.0] * 25,
        "extension": [1.0] * 25,
        "leader": [True] * 25,
        "regime": ["risk-on"] * 25,
        "long_trend": [False] * 25,
        "base_contracting": [False] * 25,
        "base_width_ok": [False] * 25,
        "base_low10": [9.6] * 25,
        "atr": [1.0] * 25,
        "low": [9.8] * 25,
        "upper_half": [True] * 25,
    })
    frame.loc[5, ["reset_seen", "tight5"]] = True
    frame.loc[5, "rvol"] = 1.3
    frame.loc[5, "extension"] = 2.0  # Wedge Pop has no extension filter.
    frame.loc[10, "long_trend"] = True
    frame.loc[24, ["long_trend", "base_contracting", "base_width_ok"]] = True
    frame.loc[24, "rvol"] = 1.3
    marked = strategy._mark_events(frame)
    assert bool(marked.loc[5, "wedge_pop"])
    assert bool(marked.loc[10, "crossback"])
    assert bool(marked.loc[24, "base_break"])


def test_s5_exhaustion_reduces_replayed_target_once():
    strategy = _strategy(_daily_frame(280), ablation_stage="S5")
    frame = pd.DataFrame({
        "close": [10.0, 10.0, 10.0],
        "high": [10.1, 10.1, 10.1],
        "low": [9.8] * 3,
        "atr": [1.0] * 3,
        "ema20": [9.5] * 3,
        "return63": [0.2] * 3,
        "return63_percentile": [1.0] * 3,
        "leader": [True] * 3,
        "regime": ["risk-on"] * 3,
        "long_trend": [False] * 3,
        "wedge_pop": [True, False, False],
        "crossback": [False] * 3,
        "base_break": [False] * 3,
        "weak_exit": [False] * 3,
        "wedge_drop": [False] * 3,
        "exhaustion": [False, True, True],
        "base_low10": [9.0] * 3,
        "prior_high10": [9.8] * 3,
    })
    active, _ = strategy._replay("600001.SH", frame)
    assert active is not None and active.exhausted
    assert strategy._weight(active) == pytest.approx(0.0025 * 10.0 / 1.0 * 0.5 * 2.0 / 3.0)


def test_same_point_in_time_history_produces_identical_targets():
    history = _daily_frame(320)
    first = _strategy(history).generate_signals(date(2024, 1, 31))
    second = _strategy(history).generate_signals(date(2024, 1, 31))
    first = normalize_strategy_output(first, spec=CycleOfPriceActionStrategy.SPEC)
    second = normalize_strategy_output(second, spec=CycleOfPriceActionStrategy.SPEC)
    assert dict(first.target_weights) == dict(second.target_weights)
    assert dict(first.diagnostics) == dict(second.diagnostics)
    assert first.target_weights
    assert sum(first.target_weights.values()) <= 0.9 + 1e-9
    assert all(0 <= weight <= 0.15 for weight in first.target_weights.values())


def test_copa_engine_signals_execute_on_next_session_open(tmp_path):
    history = _daily_frame(320)
    simulation_start = sorted(set(history.index.get_level_values("date")))[-20]
    api = MagicMock()
    api.daily.return_value = history
    api.daily_coverage = None
    with patch("quant_engine.backtest.data_handler.DataAPI", return_value=api):
        result_dir = BacktestEngine(
            CycleOfPriceActionStrategy,
            stock_list=["600001.SH", "000001.SZ"],
            ablation_stage="S1",
            leader_quantile=0.5,
            history_bars=280,
            min_avg_amount=0.0,
            risk_on_breadth=0.5,
        ).run(
            start=simulation_start,
            end=history.index.get_level_values("date").max(),
            initial_capital=1_000_000,
            output_dir=str(tmp_path),
            rebalance_frequency="daily",
        )
    signals = pd.read_parquet(Path(result_dir) / "signals.parquet")
    trades = pd.read_parquet(Path(result_dir) / "trades.parquet")
    first_signal = pd.Timestamp(signals["date"].min())
    first_trade = pd.Timestamp(trades["date"].min())
    assert first_trade > first_signal
    raw_open = history.loc[(trades.iloc[0]["code"], first_trade.date()), "open"]
    assert float(trades.iloc[0]["price"]) == pytest.approx(float(raw_open) * 1.001)
    portfolio = pd.read_parquet(Path(result_dir) / "daily_portfolio.parquet")
    assert pd.Timestamp(portfolio["date"].min()).date() == simulation_start
    assert api.daily.call_args.kwargs["start"] < simulation_start
