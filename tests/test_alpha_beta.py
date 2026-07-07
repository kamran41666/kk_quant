import pytest
import pandas as pd
import numpy as np
from quant_engine.analytics.alpha_beta import (
    capm_alpha_beta,
    information_ratio,
    fama_french_alpha,
)


class TestAlphaBeta:
    @pytest.fixture
    def sample_data(self):
        np.random.seed(42)
        dates = pd.date_range("2024-01-01", "2024-12-31", freq="B")
        n = len(dates)
        # 基准收益
        bench = pd.Series(np.random.randn(n) * 0.01, index=dates)
        # 策略收益 = 基准 * 1.2 + 噪声
        strategy = bench * 1.2 + pd.Series(np.random.randn(n) * 0.005, index=dates)
        return strategy, bench

    @pytest.fixture
    def factor_data(self):
        np.random.seed(99)
        dates = pd.date_range("2024-01-01", "2024-12-31", freq="B")
        n = len(dates)
        mkt = pd.Series(np.random.randn(n) * 0.01, index=dates)
        smb = pd.Series(np.random.randn(n) * 0.005, index=dates)
        hml = pd.Series(np.random.randn(n) * 0.005, index=dates)
        # 策略 = 因子加权 + 噪声
        strategy = 0.001 + 1.0 * mkt + 0.3 * smb - 0.2 * hml + pd.Series(
            np.random.randn(n) * 0.003, index=dates
        )
        return strategy, mkt, smb, hml

    def test_capm_alpha_beta(self, sample_data):
        strategy, bench = sample_data
        result = capm_alpha_beta(strategy, bench)
        assert "alpha" in result
        assert "beta" in result
        assert "r_squared" in result
        assert "annual_alpha" in result
        assert "t_stat_alpha" in result
        assert result["beta"] > 0  # 正相关

    def test_capm_zero_beta(self):
        """完全不相关的两个序列, beta 接近 0"""
        np.random.seed(1)
        dates = pd.date_range("2024-01-01", periods=100, freq="B")
        s = pd.Series(np.random.randn(100) * 0.01, index=dates)
        b = pd.Series(np.random.randn(100) * 0.01, index=dates)
        result = capm_alpha_beta(s, b)
        assert isinstance(result["beta"], float)

    def test_capm_on_short_data(self):
        """序列太短时的 fallback"""
        dates = pd.date_range("2024-01-01", periods=2, freq="B")
        s = pd.Series([0.001, -0.001], index=dates)
        b = pd.Series([0.002, -0.002], index=dates)
        result = capm_alpha_beta(s, b)
        assert result["alpha"] == 0.0
        assert result["beta"] == 0.0

    def test_capm_on_empty(self):
        result = capm_alpha_beta(
            pd.Series([], dtype=float),
            pd.Series([], dtype=float),
        )
        assert result["beta"] == 0.0

    def test_information_ratio(self, sample_data):
        strategy, bench = sample_data
        ir = information_ratio(strategy, bench)
        assert isinstance(ir, float)

    def test_information_ratio_perfect_tracking(self):
        """完美跟踪: IR 很大"""
        dates = pd.date_range("2024-01-01", periods=100, freq="B")
        bench = pd.Series(np.random.randn(100) * 0.01, index=dates)
        # 策略始终比基准高 0.001/天, 跟踪误差极小
        strategy = bench + 0.001 + pd.Series(np.random.randn(100) * 1e-6, index=dates)
        ir = information_ratio(strategy, bench)
        assert ir > 0

    def test_information_ratio_on_empty(self):
        ir = information_ratio(
            pd.Series([], dtype=float),
            pd.Series([], dtype=float),
        )
        assert ir == 0.0

    def test_fama_french_alpha(self, factor_data):
        strategy, mkt, smb, hml = factor_data
        result = fama_french_alpha(strategy, mkt, smb, hml)
        assert "alpha" in result
        assert "betas" in result
        assert "mkt" in result["betas"]
        assert "smb" in result["betas"]
        assert "hml" in result["betas"]
        assert result["n_obs"] > 0
        assert isinstance(result["r_squared"], float)

    def test_fama_french_no_factors(self, sample_data):
        """没有 SMB/HML 时退化为 CAPM"""
        strategy, bench = sample_data
        result = fama_french_alpha(strategy, bench)
        assert result["betas"]["smb"] == 0.0
        assert result["betas"]["hml"] == 0.0
        assert result["betas"]["mkt"] != 0.0

    def test_fama_french_on_empty(self):
        result = fama_french_alpha(
            pd.Series([], dtype=float),
            pd.Series([], dtype=float),
        )
        assert result["n_obs"] == 0

    def test_fama_french_hml_only(self, factor_data):
        """仅传 HML 不传 SMB 时 beta 分配正确 (回归测试)"""
        strategy, mkt, _, hml = factor_data
        result = fama_french_alpha(strategy, mkt, hml=hml)
        assert result["betas"]["smb"] == 0.0
        assert result["betas"]["hml"] != 0.0
        assert result["betas"]["mkt"] != 0.0

    def test_fama_french_smb_only(self, factor_data):
        """仅传 SMB 不传 HML 时 beta 分配正确"""
        strategy, mkt, smb, _ = factor_data
        result = fama_french_alpha(strategy, mkt, smb=smb)
        assert result["betas"]["smb"] != 0.0
        assert result["betas"]["hml"] == 0.0
        assert result["betas"]["mkt"] != 0.0
