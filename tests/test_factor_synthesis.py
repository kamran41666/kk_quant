"""因子合成 + 中性化测试"""

import pytest
import pandas as pd
import numpy as np

from quant_engine.factor.synthesis import (
    cross_sectional_zscore,
    synthesize_factors,
    neutralize,
)


# ============================================================================
# 截面标准化
# ============================================================================

class TestCrossSectionalZscore:
    @pytest.fixture
    def sample_df(self):
        np.random.seed(42)
        codes = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
        dates = pd.date_range("2024-01-01", "2024-01-15", freq="B")
        return pd.DataFrame(
            np.random.randn(len(dates), len(codes)),
            index=dates,
            columns=codes,
        )

    def test_zscore_mean_zero(self, sample_df):
        z = cross_sectional_zscore(sample_df)
        assert abs(z.mean(axis=1)).max() < 1e-10

    def test_zscore_std_one(self, sample_df):
        z = cross_sectional_zscore(sample_df)
        assert abs(z.std(axis=1, ddof=1) - 1.0).max() < 1e-10

    def test_zscore_with_nans(self):
        """含 NaN 的行仍可对其他有效值做 z-score。"""
        dates = pd.date_range("2024-02-01", periods=3, freq="B")
        codes = ["X", "Y", "Z", "W"]
        vals = pd.DataFrame(
            [[1, 2, np.nan, 5], [3, np.nan, 4, 8], [np.nan, 6, 7, 1]],
            index=dates,
            columns=codes,
        )
        z = cross_sectional_zscore(vals)
        # 形状不变
        assert z.shape == vals.shape
        # 每行的非 NaN 部分，均值接近 0
        for dt in dates:
            row = z.loc[dt].dropna()
            assert abs(row.mean()) < 1e-10

    def test_zscore_constant_row(self):
        """某行全为同一值时 std=0 -> 全部 NaN。"""
        dates = pd.date_range("2024-03-01", periods=2, freq="B")
        codes = ["A", "B", "C"]
        vals = pd.DataFrame(
            [[5.0, 5.0, 5.0], [1.0, 2.0, 3.0]],
            index=dates,
            columns=codes,
        )
        z = cross_sectional_zscore(vals)
        assert z.loc[dates[0]].isna().all()
        # 第二行正常
        assert not z.loc[dates[1]].isna().any()

    def test_zscore_preserves_index_columns(self, sample_df):
        z = cross_sectional_zscore(sample_df)
        assert z.index.equals(sample_df.index)
        assert z.columns.equals(sample_df.columns)


# ============================================================================
# 多因子合成
# ============================================================================

class TestSynthesizeFactors:
    @pytest.fixture
    def sample_factors(self):
        np.random.seed(42)
        codes = ["A", "B", "C", "D", "E"]
        dates = pd.date_range("2024-01-01", "2024-01-15", freq="B")
        f1 = pd.DataFrame(
            np.random.randn(len(dates), len(codes)),
            index=dates,
            columns=codes,
        )
        f2 = pd.DataFrame(
            np.random.randn(len(dates), len(codes)),
            index=dates,
            columns=codes,
        )
        return {"momentum": f1, "value": f2}

    def test_equal_weight_synthesis(self, sample_factors):
        alpha = synthesize_factors(sample_factors, method="equal")
        assert alpha.shape == sample_factors["momentum"].shape

    def test_weighted_synthesis(self, sample_factors):
        weights = {"momentum": 0.7, "value": 0.3}
        alpha = synthesize_factors(sample_factors, weights=weights, method="weighted")
        assert alpha.shape == sample_factors["momentum"].shape

    def test_weighted_synthesis_normalizes(self, sample_factors):
        """权重自动归一化（传入未归一化的权重）。"""
        weights = {"momentum": 3.0, "value": 1.0}
        # 应等价于 0.75 / 0.25
        alpha1 = synthesize_factors(sample_factors, weights=weights, method="weighted")
        weights_norm = {"momentum": 0.75, "value": 0.25}
        alpha2 = synthesize_factors(sample_factors, weights=weights_norm, method="weighted")
        pd.testing.assert_frame_equal(alpha1, alpha2)

    def test_icir_weighted(self, sample_factors):
        icir_weights = {"momentum": 0.5, "value": 0.3}
        alpha = synthesize_factors(
            sample_factors, weights=icir_weights, method="icir_weighted"
        )
        assert alpha.shape == sample_factors["momentum"].shape

    def test_single_factor(self):
        np.random.seed(1)
        dates = pd.date_range("2024-05-01", periods=5, freq="B")
        codes = ["X", "Y", "Z"]
        f = pd.DataFrame(np.random.randn(5, 3), index=dates, columns=codes)
        alpha = synthesize_factors({"only": f}, method="equal")
        # 单因子 z-score 后就是 alpha 本身
        z = cross_sectional_zscore(f)
        pd.testing.assert_frame_equal(alpha, z)

    def test_misaligned_factors(self):
        """因子矩阵日期/股票不完全对齐时仍能合成。"""
        # Deliberately construct date ranges that only partially overlap
        dates1 = pd.date_range("2024-06-03", periods=5, freq="B")  # Mon-Fri
        dates2 = pd.date_range("2024-06-06", periods=5, freq="B")  # Thu-Wed
        codes1 = ["A", "B", "C", "D"]
        codes2 = ["B", "C", "D", "E"]
        f1 = pd.DataFrame(
            np.random.randn(len(dates1), len(codes1)),
            index=dates1,
            columns=codes1,
        )
        f2 = pd.DataFrame(
            np.random.randn(len(dates2), len(codes2)),
            index=dates2,
            columns=codes2,
        )
        alpha = synthesize_factors({"f1": f1, "f2": f2}, method="equal")
        # 交集: dates1 ∩ dates2 = 2 business days (Thu-Fri),
        # codes1 ∩ codes2 = [B, C, D]
        assert alpha.shape == (2, 3)
        assert list(alpha.columns) == ["B", "C", "D"]

    def test_empty_factors_raises(self):
        with pytest.raises(ValueError, match="No factors"):
            synthesize_factors({})

    def test_weighted_missing_weight_raises(self):
        dates = pd.date_range("2024-07-01", periods=2, freq="B")
        codes = ["A", "B"]
        f = pd.DataFrame(np.random.randn(2, 2), index=dates, columns=codes)
        with pytest.raises(ValueError, match="weights must be provided"):
            synthesize_factors({"f": f}, method="weighted")

    def test_icir_weighted_zero_sum_raises(self):
        dates = pd.date_range("2024-07-01", periods=2, freq="B")
        codes = ["A", "B"]
        f = pd.DataFrame(np.random.randn(2, 2), index=dates, columns=codes)
        with pytest.raises(ValueError, match="Sum of ICIR weights must be > 0"):
            synthesize_factors(
                {"f": f}, weights={"f": 0.0}, method="icir_weighted"
            )

    def test_unknown_method_raises(self):
        dates = pd.date_range("2024-08-01", periods=2, freq="B")
        codes = ["A", "B"]
        f = pd.DataFrame(np.random.randn(2, 2), index=dates, columns=codes)
        with pytest.raises(ValueError, match="Unknown method"):
            synthesize_factors({"f": f}, method="pca")


# ============================================================================
# 中性化
# ============================================================================

class TestNeutralize:
    @pytest.fixture
    def sample_data(self):
        np.random.seed(7)
        codes = [f"S{i}" for i in range(50)]
        dates = pd.date_range("2024-01-01", periods=10, freq="B")

        # alpha
        alpha = pd.DataFrame(np.random.randn(10, 50), index=dates, columns=codes)

        # 行业: 5 种行业
        industry_groups = ["Tech", "Finance", "Health", "Energy", "Consumer"]
        industry = pd.DataFrame(
            np.random.choice(industry_groups, size=(10, 50)),
            index=dates,
            columns=codes,
        )

        # 市值
        market_cap = pd.DataFrame(
            np.random.uniform(1e8, 1e11, size=(10, 50)),
            index=dates,
            columns=codes,
        )

        return alpha, industry, market_cap

    def test_neutralize_returns_dataframe(self, sample_data):
        alpha, ind, mcap = sample_data
        result = neutralize(alpha, ind, mcap)
        assert isinstance(result, pd.DataFrame)
        assert result.shape == alpha.shape

    def test_neutralize_preserves_nan_structure(self, sample_data):
        """输入 alpha 含 NaN 时应保留。"""
        alpha, ind, mcap = sample_data
        alpha.iloc[0, 0] = np.nan
        result = neutralize(alpha, ind, mcap)
        assert np.isnan(result.loc[alpha.index[0], alpha.columns[0]])

    def test_neutralize_invalid_mcap_is_excluded(self):
        """非正市值必须退出拟合并保持 NaN。"""
        dates = pd.date_range("2024-01-01", periods=3, freq="B")
        codes = [f"S{i}" for i in range(30)]
        alpha = pd.DataFrame(np.random.randn(3, 30), index=dates, columns=codes)
        industry = pd.DataFrame("Tech", index=dates, columns=codes)
        mcap = pd.DataFrame(1e8, index=dates, columns=codes)
        mcap.iloc[0, 0] = 0
        mcap.iloc[1, 1] = -1
        mcap.iloc[2, 2] = np.inf

        result = neutralize(alpha, industry, mcap)
        assert result.shape == (3, 30)
        assert np.isnan(result.iloc[0, 0])
        assert np.isnan(result.iloc[1, 1])
        assert np.isnan(result.iloc[2, 2])

    def test_neutralize_removes_known_intercept_size_and_industry_exposure(self):
        """精确线性暴露的残差应只剩浮点误差。"""
        dates = pd.DatetimeIndex(["2024-01-02"])
        codes = [f"S{i:02d}" for i in range(12)]
        log_mcap = np.arange(1.0, 13.0)
        industries = np.array(["A"] * 6 + ["B"] * 6)
        alpha_values = 3.0 + 2.0 * log_mcap + (industries == "B") * 5.0
        alpha = pd.DataFrame([alpha_values], index=dates, columns=codes)
        industry = pd.DataFrame([industries], index=dates, columns=codes)
        market_cap = pd.DataFrame([np.exp(log_mcap)], index=dates, columns=codes)

        result = neutralize(alpha, industry, market_cap)

        assert np.nanmax(np.abs(result.to_numpy())) < 1e-10

    def test_neutralize_single_industry(self):
        """单一行业 — 行业哑变量会被 drop_first 移除，仅剩市值回归。"""
        dates = pd.date_range("2024-06-01", periods=5, freq="B")
        codes = [f"S{i}" for i in range(20)]
        alpha = pd.DataFrame(np.random.randn(5, 20), index=dates, columns=codes)
        industry = pd.DataFrame("OnlyOne", index=dates, columns=codes)
        mcap = pd.DataFrame(
            np.random.uniform(1e8, 1e11, size=(5, 20)),
            index=dates,
            columns=codes,
        )
        result = neutralize(alpha, industry, mcap)
        assert not result.isna().all(axis=None)

    def test_neutralize_misaligned_dates(self):
        """industry/market_cap 日期与 alpha 不完全对齐时应只处理交集。"""
        dates_alpha = pd.date_range("2024-01-01", periods=5, freq="B")
        dates_other = pd.date_range("2024-01-03", periods=3, freq="B")
        codes = [f"S{i}" for i in range(10)]
        alpha = pd.DataFrame(np.random.randn(5, 10), index=dates_alpha, columns=codes)
        industry = pd.DataFrame("Tech", index=dates_other, columns=codes)
        mcap = pd.DataFrame(
            np.random.uniform(1e8, 1e11, size=(3, 10)),
            index=dates_other,
            columns=codes,
        )
        result = neutralize(alpha, industry, mcap)
        # 不在交集内的日期保留 NaN
        assert result.loc[dates_alpha[0]].isna().all()
        # 交集内的日期应非全 NaN
        assert not result.loc[dates_other[0]].isna().all()
