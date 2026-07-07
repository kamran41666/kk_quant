"""α/β 归因 — CAPM, 信息比率, Fama-French 三因子"""
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats


def capm_alpha_beta(
    strategy_returns: pd.Series,
    benchmark_returns: pd.Series,
    rf: float = 0.02,
) -> dict:
    """CAPM 回归: 策略超额收益 ~ 基准超额收益

    R_s - rf_daily = α_daily + β * (R_b - rf_daily) + ε

    Args:
        strategy_returns: 策略日收益率序列
        benchmark_returns: 基准日收益率序列 (同 index)
        rf: 年化无风险利率 (默认 0.02)

    Returns:
        {
            alpha: 日度 α,
            beta: β 系数,
            r_squared: 拟合 R²,
            annual_alpha: 年化 α,
            t_stat_alpha: α 的 t 统计量,
            p_value_alpha: α 的 p 值,
        }
    """
    # 对齐日期
    common = strategy_returns.dropna().index.intersection(
        benchmark_returns.dropna().index
    )
    if len(common) < 3:
        return {
            "alpha": 0.0,
            "beta": 0.0,
            "r_squared": 0.0,
            "annual_alpha": 0.0,
            "t_stat_alpha": 0.0,
            "p_value_alpha": 1.0,
        }

    s = strategy_returns.loc[common].astype(float)
    b = benchmark_returns.loc[common].astype(float)

    # 日度无风险利率
    rf_daily = (1 + rf) ** (1 / 252) - 1
    y = s - rf_daily  # 策略超额
    X = b - rf_daily  # 基准超额

    # 线性回归
    result = stats.linregress(X, y)

    # α 的 t 统计量 = α / SE(α)
    # SE(α) = SE(残差) * sqrt(1/n + x̄² / Σ(x-̄x)²)
    n = len(X)
    residuals = y - (result.intercept + result.slope * X)
    residual_std = np.std(residuals, ddof=2)
    x_mean = X.mean()
    sum_sq_dev = ((X - x_mean) ** 2).sum()
    se_alpha = residual_std * np.sqrt(1 / n + x_mean ** 2 / sum_sq_dev) if sum_sq_dev > 0 else float("inf")
    t_stat_alpha = result.intercept / se_alpha if se_alpha > 0 else 0.0
    # 双侧 p 值
    p_value_alpha = float(2 * stats.t.sf(abs(t_stat_alpha), df=n - 2))

    annual_alpha = float((1 + result.intercept) ** 252 - 1)

    return {
        "alpha": float(result.intercept),
        "beta": float(result.slope),
        "r_squared": float(result.rvalue ** 2),
        "annual_alpha": annual_alpha,
        "t_stat_alpha": float(t_stat_alpha),
        "p_value_alpha": p_value_alpha,
    }


def information_ratio(
    strategy_returns: pd.Series,
    benchmark_returns: pd.Series,
) -> float:
    """信息比率 = 超额收益均值 / 跟踪误差标准差 (均年化)

    IR = mean(R_s - R_b) * 252 / (std(R_s - R_b) * sqrt(252))

    Args:
        strategy_returns: 策略日收益率序列
        benchmark_returns: 基准日收益率序列 (同 index)

    Returns:
        信息比率 (年化)
    """
    common = strategy_returns.dropna().index.intersection(
        benchmark_returns.dropna().index
    )
    if len(common) < 2:
        return 0.0

    excess = strategy_returns.loc[common] - benchmark_returns.loc[common]
    excess = excess.dropna()
    if len(excess) < 2:
        return 0.0

    mean_excess = excess.mean()
    std_excess = excess.std(ddof=1)
    if std_excess == 0:
        return 0.0

    return float(mean_excess / std_excess * np.sqrt(252))


def fama_french_alpha(
    strategy_returns: pd.Series,
    market_returns: pd.Series,
    smb: Optional[pd.Series] = None,
    hml: Optional[pd.Series] = None,
) -> dict:
    """Fama-French 三因子 α

    回归: R_s - R_f = α + β_m * MKT + β_s * SMB + β_h * HML + ε

    如果没有 SMB/HML, 则退化为 CAPM。

    Args:
        strategy_returns: 策略日收益率序列
        market_returns: 市场超额收益序列 (R_m - R_f)
        smb: 规模因子 SMB (可选)
        hml: 价值因子 HML (可选)

    Returns:
        {
            alpha: 日度 α (经三因子调整后的超额收益),
            annual_alpha: 年化 α,
            betas: {mkt: β_m, smb: β_s, hml: β_h},
            r_squared: 拟合 R²,
            t_stat_alpha: α 的 t 统计量,
            p_value_alpha: α 的 p 值,
            n_obs: 样本数,
        }
    """
    # 对齐
    frames = {
        "strategy": strategy_returns,
        "mkt": market_returns,
    }
    if smb is not None:
        frames["smb"] = smb
    if hml is not None:
        frames["hml"] = hml

    combined = pd.DataFrame(frames).dropna()
    # 至少需要 strategy + mkt (2 列), 加上截距至少需要 5 个观测值
    if len(combined) < 5 or len(combined.columns) < 2:
        return {
            "alpha": 0.0,
            "annual_alpha": 0.0,
            "betas": {"mkt": 0.0, "smb": 0.0, "hml": 0.0},
            "r_squared": 0.0,
            "t_stat_alpha": 0.0,
            "p_value_alpha": 1.0,
            "n_obs": len(combined),
        }

    y = combined["strategy"]
    X_cols = ["mkt"]
    if "smb" in combined.columns:
        X_cols.append("smb")
    if "hml" in combined.columns:
        X_cols.append("hml")

    X = combined[X_cols]
    # 加截距
    X_with_const = np.column_stack([np.ones(len(X)), X.values])

    # OLS: β = (X'X)^(-1) X'y
    try:
        beta_hat = np.linalg.lstsq(X_with_const, y.values, rcond=None)[0]
    except np.linalg.LinAlgError:
        return {
            "alpha": 0.0,
            "annual_alpha": 0.0,
            "betas": {"mkt": 0.0, "smb": 0.0, "hml": 0.0},
            "r_squared": 0.0,
            "t_stat_alpha": 0.0,
            "p_value_alpha": 1.0,
            "n_obs": len(combined),
        }

    alpha_daily = float(beta_hat[0])
    # 用列名映射避免基于位置的索引 bug:
    # X_cols 可能为 ["mkt", "hml"] (无 SMB) 或 ["mkt", "smb", "hml"]
    col_to_idx = {col: i + 1 for i, col in enumerate(X_cols)}
    betas = {
        "mkt": float(beta_hat[col_to_idx["mkt"]]),
        "smb": float(beta_hat[col_to_idx["smb"]]) if "smb" in col_to_idx else 0.0,
        "hml": float(beta_hat[col_to_idx["hml"]]) if "hml" in col_to_idx else 0.0,
    }

    # R²
    y_pred = X_with_const @ beta_hat
    ss_res = ((y.values - y_pred) ** 2).sum()
    ss_tot = ((y.values - y.mean()) ** 2).sum()
    r_squared = float(1 - ss_res / ss_tot) if ss_tot > 0 else 0.0

    # α 的 t 统计量
    n, k = X_with_const.shape
    residuals = y.values - y_pred
    sigma2 = (residuals ** 2).sum() / (n - k) if n > k else 0.0
    if sigma2 > 0:
        XtX_inv = np.linalg.inv(X_with_const.T @ X_with_const)
        se_alpha = np.sqrt(sigma2 * XtX_inv[0, 0])
        t_stat_alpha = float(alpha_daily / se_alpha) if se_alpha > 0 else 0.0
    else:
        t_stat_alpha = 0.0
        se_alpha = float("inf")

    p_value_alpha = float(2 * stats.t.sf(abs(t_stat_alpha), df=n - k))

    annual_alpha = float((1 + alpha_daily) ** 252 - 1)

    return {
        "alpha": alpha_daily,
        "annual_alpha": annual_alpha,
        "betas": betas,
        "r_squared": r_squared,
        "t_stat_alpha": t_stat_alpha,
        "p_value_alpha": p_value_alpha,
        "n_obs": len(combined),
    }
