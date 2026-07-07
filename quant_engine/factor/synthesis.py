"""因子合成 + 中性化 — 多因子加权合成、行业/市值中性化、截面标准化"""

from __future__ import annotations

import numpy as np
import pandas as pd
from typing import Optional


# ---------------------------------------------------------------------------
# 截面标准化
# ---------------------------------------------------------------------------

def cross_sectional_zscore(values: pd.DataFrame) -> pd.DataFrame:
    """截面 z-score 标准化。

    对每一行（日期），计算 (x - 行均值) / 行标准差，
    使每个交易日所有股票因子值的截面均值为 0、标准差为 1。

    Parameters
    ----------
    values : pd.DataFrame
        (date x code) 原始因子值。

    Returns
    -------
    pd.DataFrame
        同 shape，截面标准化后的因子值。
    """
    row_mean = values.mean(axis=1)
    row_std = values.std(axis=1, ddof=1)
    # 避免除零
    row_std = row_std.replace(0.0, np.nan)
    z = values.sub(row_mean, axis=0).div(row_std, axis=0)
    return z


# ---------------------------------------------------------------------------
# 多因子合成
# ---------------------------------------------------------------------------

def synthesize_factors(
    factor_values: dict[str, pd.DataFrame],
    weights: Optional[dict[str, float]] = None,
    method: str = "equal",
) -> pd.DataFrame:
    """多因子合成 — 将多个因子加权合成为一个 alpha。

    流程：
    1. 对每个因子做截面 z-score 标准化。
    2. 按指定权重加权求和。

    Parameters
    ----------
    factor_values : dict[str, pd.DataFrame]
        因子名 -> (date x code) 因子值。
    weights : dict[str, float] or None
        各因子权重。 ``method="weighted"`` 时使用提供的权重；
        ``method="icir_weighted"`` 时传入预计算的 ICIR 权重。
    method : str
        ``"equal"`` — 等权合成；
        ``"weighted"`` — 使用 ``weights`` 参数按权重合成；
        ``"icir_weighted"`` — 同 ``"weighted"``，建议传入 ICIR 作为权重。

    Returns
    -------
    pd.DataFrame
        (date x code) 合成 alpha 值。
    """
    names = list(factor_values.keys())
    if not names:
        raise ValueError("No factors provided to synthesize.")

    # 1) 截面 z-score
    zscored: dict[str, pd.DataFrame] = {}
    for nm in names:
        zscored[nm] = cross_sectional_zscore(factor_values[nm])

    # 找出公共行列
    base_name = names[0]
    common_dates = zscored[base_name].index
    common_codes = zscored[base_name].columns
    for nm in names[1:]:
        common_dates = common_dates.intersection(zscored[nm].index)
        common_codes = common_codes.intersection(zscored[nm].columns)

    # 2) 权重
    if method == "equal":
        wt = {nm: 1.0 / len(names) for nm in names}
    elif method == "weighted":
        if weights is None:
            raise ValueError("weights must be provided for method='weighted'")
        total = sum(weights.get(nm, 0.0) for nm in names)
        if total == 0:
            raise ValueError("Sum of provided weights must be > 0")
        wt = {nm: weights.get(nm, 0.0) / total for nm in names}
    elif method == "icir_weighted":
        if weights is None:
            raise ValueError("icir_weighted requires weights dict with pre-computed ICIR values")
        total = sum(weights.get(nm, 0.0) for nm in names)
        if total == 0:
            raise ValueError("Sum of ICIR weights must be > 0")
        wt = {nm: weights.get(nm, 0.0) / total for nm in names}
    else:
        raise ValueError(
            f"Unknown method: {method!r}. Use 'equal', 'weighted', or 'icir_weighted'."
        )

    # 3) 加权合成
    alpha = pd.DataFrame(0.0, index=common_dates, columns=common_codes)
    for nm in names:
        z = zscored[nm].loc[common_dates, common_codes]
        alpha += wt[nm] * z

    return alpha


# ---------------------------------------------------------------------------
# 中性化
# ---------------------------------------------------------------------------

def neutralize(
    alpha: pd.DataFrame,
    industry: pd.DataFrame,
    market_cap: pd.DataFrame,
) -> pd.DataFrame:
    """行业 + 市值中性化。

    每日截面回归::

        alpha_i = industry_dummies_i * gamma + beta * log(market_cap_i) + epsilon_i

    取残差 epsilon_i 作为中性化后的纯 alpha。

    Parameters
    ----------
    alpha : pd.DataFrame
        (date x code) 原始 alpha。
    industry : pd.DataFrame
        (date x code) 行业分类（字符串或整数均可）。
    market_cap : pd.DataFrame
        (date x code) 市值（原始值，会自动取对数）。

    Returns
    -------
    pd.DataFrame
        (date x code) 中性化后的 alpha（回归残差）。缺失值保留 NaN。
    """
    residual = pd.DataFrame(np.nan, index=alpha.index, columns=alpha.columns)

    for dt in alpha.index:
        # 检查该日期是否在 industry 和 market_cap 中
        if dt not in industry.index or dt not in market_cap.index:
            continue

        a = alpha.loc[dt].copy()
        ind = industry.loc[dt].copy()
        mcap = market_cap.loc[dt].copy()

        # 三者对齐
        common = a.index.intersection(ind.index).intersection(mcap.index)
        if len(common) < 10:
            continue

        a = a[common]
        ind = ind[common]
        mcap = mcap[common]

        # 行业哑变量
        ind_dummies = pd.get_dummies(ind, drop_first=True, dtype=float)
        ind_dummies.index = common

        # log 市值（剔除 <=0 的异常值）
        mcap_clean = mcap.replace([np.inf, -np.inf], np.nan)
        log_mcap = np.log(mcap_clean.clip(lower=1e-10))

        # 设计矩阵
        X = pd.concat([ind_dummies, log_mcap.rename("log_mcap")], axis=1)

        # 有效行
        valid = a.notna() & X.notna().all(axis=1)
        n_valid = int(valid.sum())
        if n_valid < X.shape[1] + 1:
            continue

        y = a[valid].values
        X_mat = X.loc[valid].values

        try:
            coeffs, *_ = np.linalg.lstsq(X_mat, y, rcond=None)
        except np.linalg.LinAlgError:
            continue

        y_pred = X_mat @ coeffs
        resid = y - y_pred
        residual.loc[dt, common[valid]] = resid

    return residual
