"""因子评估模块测试 — IC分析 + 分层回测 + 因子相关性 + Fama-MacBeth"""

import pytest
import pandas as pd
import numpy as np

from quant_engine.factor.evaluation import (
    calc_ic,
    calc_ic_summary,
    quantile_analysis,
    factor_correlation_matrix,
    fama_macbeth,
)


# ============================================================================
# IC 分析
# ============================================================================

class TestICAnalysis:
    @pytest.fixture
    def sample_data(self):
        np.random.seed(42)
        codes = ["A", "B", "C", "D", "E"]
        dates = pd.date_range("2024-01-01", "2024-01-31", freq="B")
        factor = pd.DataFrame(
            np.random.randn(len(dates), len(codes)),
            index=dates,
            columns=codes,
        )
        forward_returns = pd.DataFrame(
            np.random.randn(len(dates), len(codes)),
            index=dates,
            columns=codes,
        )
        return factor, forward_returns

    def test_calc_ic_rank(self, sample_data):
        factor, fwd_ret = sample_data
        ic = calc_ic(factor, fwd_ret, method="rank")
        assert len(ic) > 0
        assert isinstance(ic, pd.Series)
        # Rank IC must be in [-1, 1]
        assert (-1.0 <= ic).all() and (ic <= 1.0).all()

    def test_calc_ic_pearson(self, sample_data):
        factor, fwd_ret = sample_data
        ic = calc_ic(factor, fwd_ret, method="pearson")
        assert len(ic) > 0
        assert isinstance(ic, pd.Series)

    def test_calc_ic_invalid_method_raises(self, sample_data):
        factor, fwd_ret = sample_data
        with pytest.raises(ValueError, match="Unknown method"):
            calc_ic(factor, fwd_ret, method="kendall")

    def test_calc_ic_with_nans(self):
        """含 NaN 的因子值应被跳过且不报错。"""
        np.random.seed(7)
        codes = ["X", "Y", "Z"]
        dates = pd.date_range("2024-02-01", periods=5, freq="B")
        factor = pd.DataFrame(
            [[1, 2, np.nan], [3, np.nan, 4], [5, 6, 7], [np.nan] * 3, [8, 9, 10]],
            index=dates,
            columns=codes,
        )
        fwd_ret = pd.DataFrame(
            np.random.randn(5, 3), index=dates, columns=codes
        )
        ic = calc_ic(factor, fwd_ret, method="rank")
        assert isinstance(ic, pd.Series)

    def test_calc_ic_too_few_stocks_skipped(self):
        """每期有效股票数 < 3 时应跳过该期。"""
        codes = ["S1", "S2"]
        dates = pd.date_range("2024-03-01", periods=3, freq="B")
        factor = pd.DataFrame(np.random.randn(3, 2), index=dates, columns=codes)
        fwd_ret = pd.DataFrame(np.random.randn(3, 2), index=dates, columns=codes)
        ic = calc_ic(factor, fwd_ret, method="rank")
        # < 3 valid stocks per cross-section -> 所有期都被跳过
        assert len(ic) == 0

    def test_ic_summary(self, sample_data):
        factor, fwd_ret = sample_data
        ic = calc_ic(factor, fwd_ret, method="rank")
        summary = calc_ic_summary(ic)

        assert "ic_mean" in summary
        assert "ic_std" in summary
        assert "icir" in summary
        assert "ic_positive_ratio" in summary
        assert "t_stat" in summary
        assert "p_value" in summary
        assert "n_obs" in summary

        # ICIR = IC_mean / IC_std
        expected_icir = summary["ic_mean"] / summary["ic_std"]
        assert summary["icir"] == pytest.approx(expected_icir, rel=0.01)

    def test_ic_summary_empty(self):
        empty_ic = pd.Series([], dtype=float)
        summary = calc_ic_summary(empty_ic)
        assert summary["n_obs"] == 0
        assert np.isnan(summary["ic_mean"])

    def test_ic_positive_correlation_yields_high_ratio(self):
        """构造正向相关数据，验证 ic_positive_ratio > 0.5. """
        dates = pd.date_range("2024-01-01", periods=50, freq="B")
        codes = [f"S{i}" for i in range(20)]
        # 因子值和收益高度正相关
        np.random.seed(1)
        base = np.random.randn(50, 20)
        noise = np.random.randn(50, 20) * 0.1
        factor = pd.DataFrame(base, index=dates, columns=codes)
        fwd_ret = pd.DataFrame(base * 0.8 + noise, index=dates, columns=codes)

        ic = calc_ic(factor, fwd_ret, method="rank")
        summary = calc_ic_summary(ic)
        assert summary["ic_positive_ratio"] > 0.5
        assert summary["ic_mean"] > 0


# ============================================================================
# 分层回测
# ============================================================================

class TestQuantileAnalysis:
    @pytest.fixture
    def sample_data(self):
        np.random.seed(99)
        codes = [f"S{i}" for i in range(30)]
        dates = pd.date_range("2024-01-01", periods=20, freq="B")
        factor = pd.DataFrame(np.random.randn(20, 30), index=dates, columns=codes)
        fwd_ret = pd.DataFrame(np.random.randn(20, 30), index=dates, columns=codes)
        return factor, fwd_ret

    def test_basic_quantiles(self, sample_data):
        factor, fwd_ret = sample_data
        result = quantile_analysis(factor, fwd_ret, n_groups=5)

        assert "group_returns" in result
        assert "top_bottom_spread" in result

        gr = result["group_returns"]
        assert isinstance(gr, pd.DataFrame)
        # 五组应有 Q1..Q5
        assert list(gr.columns) == ["Q1", "Q2", "Q3", "Q4", "Q5"]

        spread = result["top_bottom_spread"]
        assert isinstance(spread, pd.Series)
        # 应与 group_returns 行数一致
        assert len(spread) == len(gr)

    def test_quantile_with_nans(self):
        """含 NaN 时仍能正常分组。"""
        dates = pd.date_range("2024-01-01", periods=5, freq="B")
        codes = [f"C{i}" for i in range(20)]
        factor = pd.DataFrame(np.random.randn(5, 20), index=dates, columns=codes)
        factor.iloc[0, 0] = np.nan
        factor.iloc[2, 3:7] = np.nan
        fwd_ret = pd.DataFrame(np.random.randn(5, 20), index=dates, columns=codes)

        result = quantile_analysis(factor, fwd_ret, n_groups=3)
        assert len(result["group_returns"]) > 0

    def test_quantile_too_few_stocks(self):
        """股票数少于分组数时应返回空结果。"""
        dates = pd.date_range("2024-01-01", periods=2, freq="B")
        codes = ["A", "B"]
        factor = pd.DataFrame(np.random.randn(2, 2), index=dates, columns=codes)
        fwd_ret = pd.DataFrame(np.random.randn(2, 2), index=dates, columns=codes)

        result = quantile_analysis(factor, fwd_ret, n_groups=5)
        assert result["group_returns"].empty
        assert len(result["top_bottom_spread"]) == 0

    def test_top_bottom_spread_computation(self):
        """验证 spread 确实 = top - bottom。"""
        np.random.seed(77)
        codes = [f"S{i}" for i in range(50)]
        dates = pd.date_range("2024-06-01", periods=10, freq="B")
        factor = pd.DataFrame(np.random.randn(10, 50), index=dates, columns=codes)
        fwd_ret = pd.DataFrame(np.random.randn(10, 50), index=dates, columns=codes)

        result = quantile_analysis(factor, fwd_ret, n_groups=5)
        gr = result["group_returns"]
        spread_calc = gr["Q5"] - gr["Q1"]
        pd.testing.assert_series_equal(
            result["top_bottom_spread"], spread_calc, check_names=False
        )


# ============================================================================
# 因子相关性
# ============================================================================

class TestFactorCorrelationMatrix:
    def test_single_factor(self):
        dates = pd.date_range("2024-01-01", periods=5, freq="B")
        codes = ["A", "B", "C"]
        f1 = pd.DataFrame(np.random.randn(5, 3), index=dates, columns=codes)
        corr = factor_correlation_matrix({"f1": f1})
        assert corr.shape == (1, 1)
        assert corr.loc["f1", "f1"] == 1.0

    def test_two_factors_diag_is_one(self):
        np.random.seed(3)
        dates = pd.date_range("2024-03-01", periods=10, freq="B")
        codes = [f"C{i}" for i in range(15)]
        f1 = pd.DataFrame(np.random.randn(10, 15), index=dates, columns=codes)
        f2 = pd.DataFrame(np.random.randn(10, 15), index=dates, columns=codes)

        corr = factor_correlation_matrix({"a": f1, "b": f2})
        assert corr.shape == (2, 2)
        assert corr.loc["a", "a"] == 1.0
        assert corr.loc["b", "b"] == 1.0
        # 对称
        assert corr.loc["a", "b"] == pytest.approx(corr.loc["b", "a"], abs=1e-9)

    def test_highly_correlated_factors(self):
        """两个几乎相同的因子应给出接近 1.0 的相关性。"""
        np.random.seed(5)
        dates = pd.date_range("2024-04-01", periods=20, freq="B")
        codes = [f"S{i}" for i in range(30)]
        base = np.random.randn(20, 30)
        f1 = pd.DataFrame(base, index=dates, columns=codes)
        f2 = pd.DataFrame(base + np.random.randn(20, 30) * 0.01, index=dates, columns=codes)

        corr = factor_correlation_matrix({"f1": f1, "f2": f2})
        assert corr.loc["f1", "f2"] > 0.95


# ============================================================================
# Fama-MacBeth
# ============================================================================

class TestFamaMacBeth:
    def test_single_factor_basic(self):
        """单因子 Fama-MacBeth — 应能返回结果字典。"""
        np.random.seed(13)
        codes = [f"S{i}" for i in range(30)]
        dates = pd.date_range("2024-01-01", periods=60, freq="B")

        factor = pd.DataFrame(np.random.randn(60, 30), index=dates, columns=codes)
        ret = pd.DataFrame(np.random.randn(60, 30), index=dates, columns=codes)

        result = fama_macbeth({"momentum": factor}, ret)

        assert "momentum" in result
        for key in ("lambda_mean", "lambda_std", "t_stat", "p_value"):
            assert key in result["momentum"]

    def test_multi_factor(self):
        """多因子回归 — 检查结构。"""
        np.random.seed(17)
        codes = [f"S{i}" for i in range(40)]
        dates = pd.date_range("2024-01-01", periods=80, freq="B")

        f1 = pd.DataFrame(np.random.randn(80, 40), index=dates, columns=codes)
        f2 = pd.DataFrame(np.random.randn(80, 40), index=dates, columns=codes)
        ret = pd.DataFrame(np.random.randn(80, 40), index=dates, columns=codes)

        result = fama_macbeth({"f1": f1, "f2": f2}, ret)

        assert "f1" in result
        assert "f2" in result
        for nm in ("f1", "f2"):
            assert "lambda_mean" in result[nm]

    def test_insufficient_data_returns_nan(self):
        """数据不足时返回 nan。"""
        codes = ["A", "B"]
        dates = pd.date_range("2024-01-01", periods=5, freq="B")
        f1 = pd.DataFrame(np.random.randn(5, 2), index=dates, columns=codes)
        ret = pd.DataFrame(np.random.randn(5, 2), index=dates, columns=codes)

        result = fama_macbeth({"f": f1}, ret)
        assert np.isnan(result["f"]["lambda_mean"])

    def test_fama_macbeth_with_nans(self):
        """含 NaN 的数据不应崩溃。"""
        np.random.seed(23)
        codes = [f"S{i}" for i in range(30)]
        dates = pd.date_range("2024-01-01", periods=50, freq="B")

        factor = pd.DataFrame(np.random.randn(50, 30), index=dates, columns=codes)
        factor.iloc[0:5, 0:3] = np.nan
        ret = pd.DataFrame(np.random.randn(50, 30), index=dates, columns=codes)
        ret.iloc[10:15, 5:8] = np.nan

        result = fama_macbeth({"factor": factor}, ret)
        assert "factor" in result
