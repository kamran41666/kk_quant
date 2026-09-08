from datetime import date

import numpy as np
import pandas as pd
import pytest

from quant_engine.backtest.context import StrategyContext
from quant_engine.backtest.data_handler import DataHandler
from quant_engine.backtest.protocol import (
    DataRequirement,
    StrategySpec,
    resolve_strategy_data_requirements,
)
from quant_engine.data.calendar import TradingCalendar
from quant_engine.factor import compute_factor, list_factor_definitions
from quant_engine.factor.operators import delay
from quant_engine.factor.research import run_factor_research
from scripts.run_factor_research import build_forward_open_returns


def panel_data(*, periods: int = 60, codes: tuple[str, ...] = ("A", "B", "C", "D")) -> pd.DataFrame:
    dates = pd.date_range("2024-01-02", periods=periods, freq="B").date
    index = pd.MultiIndex.from_product([codes, dates], names=["code", "date"])
    rng = np.random.default_rng(20260908)
    trend = np.tile(np.arange(periods, dtype=float), len(codes))
    frame = pd.DataFrame(index=index)
    frame["close"] = 10.0 + trend * 0.02 + rng.normal(0.0, 0.08, len(index))
    frame["open"] = frame["close"] * (1.0 + rng.normal(0.0, 0.004, len(index)))
    frame["high"] = frame[["open", "close"]].max(axis=1) * 1.01
    frame["low"] = frame[["open", "close"]].min(axis=1) * 0.99
    frame["volume"] = rng.lognormal(12.0, 0.35, len(index))
    frame["amount"] = frame["close"] * frame["volume"]
    frame["turnover_rate"] = rng.uniform(0.1, 4.0, len(index))
    return frame


def test_catalog_has_complete_project_raw_material_categories():
    definitions = list_factor_definitions()
    categories = {item.category for item in definitions}
    assert {"raw_price", "raw_volume", "raw_liquidity", "raw_risk", "raw_fundamental"} <= categories
    assert len(definitions) == 21
    advanced = [item for item in definitions if item.category in {"advanced", "alpha101_material"}]
    assert len(advanced) == 6
    assert all("rank" in item.operators or "ts_corr" in item.operators for item in advanced)
    assert all(len(set(item.operators)) >= 3 for item in advanced)


def test_factor_output_is_aligned_winsorized_and_point_in_time_standardized():
    frame = panel_data()
    result = compute_factor("volume_price_divergence_corr_10_ohlcv", frame)
    valid = result.dropna()
    by_date = result.groupby(level="date")
    assert len(valid) > 0
    assert by_date.mean().dropna().abs().max() < 1e-12
    assert by_date.std(ddof=0).dropna().min() == pytest.approx(1.0)


def test_factor_accepts_date_ticker_index_and_does_not_mix_tickers():
    frame = panel_data(codes=("A", "B", "C"))
    reordered = frame.reorder_levels(["date", "code"]).sort_index()
    reordered.index = reordered.index.set_names(["date", "ticker"])
    result = compute_factor("raw_return_lagged_1_close", reordered)
    expected = reordered["close"].unstack("ticker").shift(1).pct_change(fill_method=None)
    expected_long = expected.stack(future_stack=True).reindex(result.index)
    expected_rank = expected_long.groupby(level="date").rank()
    result_rank = result.groupby(level="date").rank()
    pd.testing.assert_series_equal(result_rank, expected_rank, check_names=False)


@pytest.mark.parametrize(
    "factor_name",
    [
        "alpha002_lag_safe_6_ohlcv",
        "volume_price_divergence_corr_10_ohlcv",
        "breakout_dryup_quantile_20_ohlcv",
        "liquidity_shock_reversal_decay_20_ohlcv",
        "tail_asymmetry_rank_20_close",
        "range_compression_volume_release_20_ohlcv",
    ],
)
def test_t_close_mutation_cannot_change_t_factor(factor_name):
    frame = panel_data()
    target_date = frame.index.get_level_values("date").max()
    before = compute_factor(factor_name, frame)
    changed = frame.copy()
    changed.loc[(slice(None), target_date), "close"] *= 25.0
    after = compute_factor(factor_name, changed)
    pd.testing.assert_series_equal(
        before.xs(target_date, level="date"),
        after.xs(target_date, level="date"),
    )


@pytest.mark.parametrize(
    "factor_name",
    [item.name for item in list_factor_definitions() if item.category != "raw_fundamental"],
)
def test_appending_future_rows_cannot_rewrite_historical_factor(factor_name):
    frame = panel_data()
    cutoff = sorted(frame.index.get_level_values("date").unique())[44]
    prefix = frame.loc[frame.index.get_level_values("date") <= cutoff]
    historical = compute_factor(factor_name, prefix).xs(cutoff, level="date")
    full = compute_factor(factor_name, frame).xs(cutoff, level="date")
    pd.testing.assert_series_equal(historical, full)


def test_negative_delay_is_rejected():
    with pytest.raises(ValueError, match="look-ahead"):
        delay(panel_data()["close"], -1)


def test_research_label_matches_next_open_execution_protocol():
    opens = pd.DataFrame(
        {"A": [10.0, 20.0, 30.0, 40.0], "B": [5.0, 5.0, 10.0, 20.0]},
        index=pd.date_range("2024-01-02", periods=4, freq="B"),
    )
    labels = build_forward_open_returns(opens, holding_period=1)
    assert labels.iloc[0].to_dict() == pytest.approx({"A": 0.5, "B": 1.0})
    assert labels.iloc[1].to_dict() == pytest.approx({"A": 1 / 3, "B": 1.0})
    assert labels.iloc[2:].isna().all().all()


def test_research_gate_rejects_duplicate_existing_factor():
    frame = panel_data()
    duplicate = compute_factor("volume_price_divergence_corr_10_ohlcv", frame)
    result = run_factor_research(
        frame,
        candidate_names=("volume_price_divergence_corr_10_ohlcv",),
        existing_factors={"already_present": duplicate},
    )
    assert result.accepted == ()
    assert result.evidence[0].maximum_absolute_correlation == pytest.approx(1.0)
    assert result.evidence[0].most_correlated_factor == "already_present"
    assert result.evidence[0].decision_reason == "correlation_limit_exceeded"


def test_research_gate_rejects_candidate_when_correlation_is_not_computable():
    frame = panel_data(periods=2)
    result = run_factor_research(
        frame,
        candidate_names=("volume_price_divergence_corr_10_ohlcv",),
        existing_factors={
            "baseline": compute_factor("raw_return_lagged_1_close", frame),
        },
    )
    assert result.accepted == ()
    assert result.values["volume_price_divergence_corr_10_ohlcv"].notna().sum().sum() == 0
    assert result.evidence[0].maximum_absolute_correlation is None
    assert result.evidence[0].decision_reason == "insufficient_evidence"


def test_protocol_resolves_factor_inputs_and_warmup():
    spec = StrategySpec(
        id="factor-contract-test",
        name="factor contract",
        version="1.0.0",
        description="test",
        markets=("a-share",),
        data=(DataRequirement(
            "a_share_daily",
            (),
            1,
            factors=("volume_price_divergence_corr_10_ohlcv",),
        ),),
    )
    resolved = resolve_strategy_data_requirements(
        spec,
        {},
        supported={"a_share_daily": ("1d", "event_driven")},
    )[0]
    assert resolved.fields == ("close", "volume")
    assert resolved.required_bars == 22
    assert resolved.factors == ("volume_price_divergence_corr_10_ohlcv",)


def test_data_handler_returns_only_requested_as_of_cross_section():
    frame = panel_data(periods=30)
    handler = object.__new__(DataHandler)
    handler._daily_data = frame
    handler._codes = ["A", "B", "C", "D"]
    handler._current_date = date(2024, 2, 12)
    handler._factor_cache = {}
    result = handler.get_factor("raw_return_lagged_1_close", date(2024, 2, 12))
    assert result.index.tolist() == handler._codes
    assert result.notna().all()
    with pytest.raises(ValueError, match="current simulation date"):
        handler.get_factor("raw_return_lagged_1_close", date(2024, 2, 13))


def test_data_handler_uses_latest_visible_fundamental_announcement():
    frame = panel_data(periods=30)

    class FundamentalAPI:
        def fundamentals(self, codes, fields, as_of):
            rows = [
                item
                for offset, code in enumerate(codes)
                for item in (
                    {"code": code, "report_date": date(2023, 9, 30), "field": "pe",
                     "value": 20.0 + offset, "announce_date": date(2023, 10, 30)},
                    {"code": code, "report_date": date(2023, 12, 31), "field": "pe",
                     "value": 10.0 + offset, "announce_date": date(2024, 2, 1)},
                )
            ]
            return pd.DataFrame(
                rows
            ).set_index(["code", "report_date", "field"])

    handler = object.__new__(DataHandler)
    handler._daily_data = frame
    handler._codes = ["A", "B", "C", "D"]
    handler._current_date = date(2024, 2, 12)
    handler._factor_cache = {}
    handler._api = FundamentalAPI()
    result = handler.get_factor("raw_earnings_yield_1_fundamental", date(2024, 2, 12))
    assert result.notna().all()
    assert result.loc["A"] > result.loc["D"]


def test_context_factor_requires_bound_positioned_portal():
    context = StrategyContext(TradingCalendar())
    with pytest.raises(RuntimeError, match="not bound"):
        context.get_factor("raw_return_lagged_1_close", date(2024, 1, 2))
