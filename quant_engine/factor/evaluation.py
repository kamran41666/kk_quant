"""因子评估模块 — IC 分析 + 分层回测 + 因子相关性 + Fama-MacBeth 回归

所有函数均对对齐后的 (date x code) DataFrame 进行操作，
缺失值在截面上按有效股票处理（逐行剔除 NaN）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from typing import Any


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _align_and_filter(
    factor_values: pd.DataFrame,
    forward_returns: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """对齐因子值和收益矩阵的行（日期）和列（股票），统一去除不匹配部分。"""
    fv, fr = factor_values.align(forward_returns, join="inner")
    # 列对齐
    common_cols = fv.columns.intersection(fr.columns)
    common_idx = fv.index.intersection(fr.index)
    return fv.loc[common_idx, common_cols], fr.loc[common_idx, common_cols]


def _try_import_scipy():
    """惰性导入 scipy.stats（p-value 计算用），缺失时返回 None。"""
    try:
        from scipy import stats as _stats

        return _stats
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# IC 分析
# ---------------------------------------------------------------------------

def calc_ic(
    factor_values: pd.DataFrame,
    forward_returns: pd.DataFrame,
    method: str = "rank",
) -> pd.Series:
    """计算 IC 时间序列。

    Parameters
    ----------
    factor_values : pd.DataFrame
        (date x code) 因子值矩阵。
    forward_returns : pd.DataFrame
        (date x code) 下期收益矩阵（与 factor_values 对齐的日期为因子值的日期，
        收益应为该日期对应的**下一期**收益）。
    method : str
        ``"rank"`` — Spearman 秩相关系数（默认）；
        ``"pearson"`` — Pearson 线性相关系数。

    Returns
    -------
    pd.Series
        IC 时间序列，索引为交易日，值为当日的截面相关系数。
    """
    fv, fr = _align_and_filter(factor_values, forward_returns)

    ic_records: list[tuple[Any, float]] = []
    for dt in fv.index:
        x = fv.loc[dt]
        y = fr.loc[dt]
        mask = x.notna() & y.notna()
        if mask.sum() < 3:
            continue
        x = x[mask]
        y = y[mask]
        if method == "rank":
            val = x.rank().corr(y.rank())
        elif method == "pearson":
            val = x.corr(y)
        else:
            raise ValueError(f"Unknown method: {method!r}. Use 'rank' or 'pearson'.")
        ic_records.append((dt, val))

    if not ic_records:
        return pd.Series([], name="IC", dtype=float)
    idx, vals = zip(*ic_records)
    return pd.Series(vals, index=pd.DatetimeIndex(idx), name="IC")


def calc_ic_summary(ic_series: pd.Series) -> dict[str, float]:
    """IC 统计汇总。

    Parameters
    ----------
    ic_series : pd.Series
        ``calc_ic()`` 的返回值。

    Returns
    -------
    dict
        键包括: ic_mean, ic_std, icir, ic_positive_ratio, t_stat, p_value, n_obs。
    """
    ic = ic_series.dropna().astype(float)
    if len(ic) == 0:
        return {
            "ic_mean": np.nan,
            "ic_std": np.nan,
            "icir": np.nan,
            "ic_positive_ratio": np.nan,
            "t_stat": np.nan,
            "p_value": np.nan,
            "n_obs": 0,
        }

    ic_mean = float(ic.mean())
    ic_std = float(ic.std(ddof=1))
    icir = ic_mean / ic_std if ic_std > 0 else np.nan
    ic_positive_ratio = float((ic > 0).mean())
    t_stat = ic_mean / (ic_std / np.sqrt(len(ic))) if ic_std > 0 else np.nan
    p_value = np.nan

    stats_mod = _try_import_scipy()
    if stats_mod is not None and not np.isnan(t_stat):
        p_value = 2.0 * (1.0 - stats_mod.t.cdf(abs(t_stat), df=len(ic) - 1))

    return {
        "ic_mean": ic_mean,
        "ic_std": ic_std,
        "icir": icir,
        "ic_positive_ratio": ic_positive_ratio,
        "t_stat": t_stat,
        "p_value": p_value,
        "n_obs": len(ic),
    }


# ---------------------------------------------------------------------------
# 分层回测
# ---------------------------------------------------------------------------

def quantile_analysis(
    factor_values: pd.DataFrame,
    forward_returns: pd.DataFrame,
    n_groups: int = 5,
) -> dict[str, pd.DataFrame | pd.Series]:
    """分层回测 — 按因子值分组后计算每组等权收益。

    每日按因子值将股票分为 *n_groups* 组，Q1 为因子值最低组，
    Q{n_groups} 为因子值最高组，计算每组等权平均下期收益。

    Parameters
    ----------
    factor_values : pd.DataFrame
        (date x code) 因子值。
    forward_returns : pd.DataFrame
        (date x code) 下期收益。
    n_groups : int
        分组数（默认 5）。

    Returns
    -------
    dict
        ``group_returns`` — (date x group_label) 每组日度收益；
        ``top_bottom_spread`` — 最高组 - 最低组的收益差序列。
    """
    fv, fr = _align_and_filter(factor_values, forward_returns)

    labels = [f"Q{i + 1}" for i in range(n_groups)]
    group_returns_list: list[pd.Series] = []

    for dt in fv.index:
        x = fv.loc[dt]
        y = fr.loc[dt]
        mask = x.notna() & y.notna()
        x = x[mask]
        y = y[mask]
        if len(x) < n_groups:
            continue

        try:
            groups = pd.qcut(x, q=n_groups, labels=labels, duplicates="drop")
        except ValueError:
            continue

        grp_ret = y.groupby(groups).mean()
        grp_ret.name = dt
        group_returns_list.append(grp_ret)

    if not group_returns_list:
        return {"group_returns": pd.DataFrame(), "top_bottom_spread": pd.Series(dtype=float)}

    gr_df = pd.DataFrame(group_returns_list)
    # 确保列按标签排序
    existing_labels = [lab for lab in labels if lab in gr_df.columns]
    gr_df = gr_df[existing_labels]

    spread = pd.Series(dtype=float)
    if len(existing_labels) >= 2:
        top_q = existing_labels[-1]
        bottom_q = existing_labels[0]
        spread = gr_df[top_q] - gr_df[bottom_q]

    return {"group_returns": gr_df, "top_bottom_spread": spread}


# ---------------------------------------------------------------------------
# 因子相关性
# ---------------------------------------------------------------------------

def factor_correlation_matrix(
    factor_values: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """多因子截面 Spearman 相关性矩阵（每日截面相关后取均值）。

    Parameters
    ----------
    factor_values : dict[str, pd.DataFrame]
        因子名 -> (date x code) 因子值 DataFrame。

    Returns
    -------
    pd.DataFrame
        (n_factors x n_factors) 平均截面 Spearman 相关系数矩阵。
    """
    names = list(factor_values.keys())
    n = len(names)
    if n == 0:
        return pd.DataFrame()
    if n == 1:
        return pd.DataFrame([[1.0]], index=names, columns=names)

    # 找到所有因子的公共日期和股票
    common_dates = factor_values[names[0]].index
    common_codes = factor_values[names[0]].columns
    for nm in names[1:]:
        common_dates = common_dates.intersection(factor_values[nm].index)
        common_codes = common_codes.intersection(factor_values[nm].columns)

    # 按上三角累积
    corr_sum: dict[tuple[int, int], float] = {}
    counts: dict[tuple[int, int], int] = {}
    idx_map = {nm: i for i, nm in enumerate(names)}

    for i in range(n):
        for j in range(i, n):
            corr_sum[(i, j)] = 0.0
            counts[(i, j)] = 0

    for dt in common_dates:
        arr = np.empty((len(common_codes), n), dtype=float)
        arr[:] = np.nan
        for ni, nm in enumerate(names):
            arr[:, ni] = factor_values[nm].loc[dt, common_codes].values

        # 只有该日对所有因子都有效的股票才参与
        valid = np.all(np.isfinite(arr), axis=1)
        if valid.sum() < 3:
            continue

        sub = arr[valid, :]
        # 对每列排名
        ranked = np.empty_like(sub)
        for col in range(n):
            ranked[:, col] = pd.Series(sub[:, col]).rank().values

        # 相关矩阵
        corr_mat = np.corrcoef(ranked, rowvar=False)  # (n, n)

        for i in range(n):
            for j in range(i, n):
                c = corr_mat[i, j]
                if np.isfinite(c):
                    corr_sum[(i, j)] += c
                    counts[(i, j)] += 1

    # 构建输出
    out = pd.DataFrame(np.eye(n), index=names, columns=names)
    for i in range(n):
        for j in range(i, n):
            avg = corr_sum[(i, j)] / counts[(i, j)] if counts[(i, j)] > 0 else np.nan
            out.iloc[i, j] = avg
            if i != j:
                out.iloc[j, i] = avg

    return out


# ---------------------------------------------------------------------------
# Fama-MacBeth 两阶段回归
# ---------------------------------------------------------------------------

def fama_macbeth(
    factor_values: dict[str, pd.DataFrame],
    returns: pd.DataFrame,
) -> dict[str, dict[str, float]]:
    """Fama-MacBeth 两阶段回归。

    **Stage 1（时间序列）**：对每只股票，用其因子值时间序列回归其收益时间序列，
    得到该股票对每个因子的暴露 β。

    **Stage 2（横截面）**：对每个交易日，用收益横截面回归 β，得到每个因子的
    风险溢价 λ_t。最后对 λ_t 序列计算均值、t 统计量和 p 值。

    Parameters
    ----------
    factor_values : dict[str, pd.DataFrame]
        因子名 -> (date x code) 因子值。
    returns : pd.DataFrame
        (date x code) 收益矩阵（通常为下期收益）。

    Returns
    -------
    dict
        每个因子对应一个字典，包含 lambda_mean, lambda_std, t_stat, p_value。
    """
    names = list(factor_values.keys())
    if not names:
        return {}

    # ---- 对齐 ----
    common_dates = returns.index
    common_codes = returns.columns
    for nm in names:
        common_dates = common_dates.intersection(factor_values[nm].index)
        common_codes = common_codes.intersection(factor_values[nm].columns)

    if len(common_dates) < 10 or len(common_codes) < len(names) + 1:
        return {
            nm: {
                "lambda_mean": np.nan, "lambda_std": np.nan,
                "t_stat": np.nan, "p_value": np.nan,
            }
            for nm in names
        }

    # 对齐后的矩阵
    ret = returns.loc[common_dates, common_codes]
    fac_vals = {nm: factor_values[nm].loc[common_dates, common_codes] for nm in names}

    T, N = len(common_dates), len(common_codes)

    # ---- Stage 1: 时间序列回归（逐股） ----
    betas = pd.DataFrame(np.nan, index=common_codes, columns=names)

    for code in common_codes:
        y = ret[code].values.astype(float)              # (T,)
        X_cols = [fac_vals[nm][code].values for nm in names] + [np.ones(T)]
        X = np.column_stack(X_cols)                     # (T, K+1)

        valid = np.isfinite(y) & np.all(np.isfinite(X), axis=1)
        if valid.sum() < len(names) + 2:
            continue

        try:
            coeffs, *_ = np.linalg.lstsq(X[valid], y[valid], rcond=None)
        except np.linalg.LinAlgError:
            continue
        betas.loc[code, names] = coeffs[:-1]  # exclude intercept

    # ---- Stage 2: 横截面回归（逐日） ----
    lambdas: dict[str, list[float]] = {nm: [] for nm in names}

    for t_idx, dt in enumerate(common_dates):
        y = ret.loc[dt].values.astype(float)                              # (N,)
        X_cols = [betas[nm].values for nm in names] + [np.ones(N)]
        X = np.column_stack(X_cols)                                       # (N, K+1)

        valid = np.isfinite(y) & np.all(np.isfinite(X), axis=1)
        if valid.sum() < len(names) + 2:
            continue

        try:
            coeffs, *_ = np.linalg.lstsq(X[valid], y[valid], rcond=None)
        except np.linalg.LinAlgError:
            continue
        for i, nm in enumerate(names):
            lambdas[nm].append(float(coeffs[i]))

    # ---- 汇总 ----
    stats_mod = _try_import_scipy()
    result: dict[str, dict[str, float]] = {}

    for nm in names:
        lam = np.array(lambdas[nm])
        if len(lam) == 0:
            result[nm] = {
                "lambda_mean": np.nan, "lambda_std": np.nan,
                "t_stat": np.nan, "p_value": np.nan,
            }
            continue

        lam_mean = float(np.mean(lam))
        lam_std = float(np.std(lam, ddof=1))
        t_stat = lam_mean / (lam_std / np.sqrt(len(lam))) if lam_std > 0 else np.nan
        p_value = np.nan
        if stats_mod is not None and not np.isnan(t_stat):
            p_value = 2.0 * (1.0 - stats_mod.t.cdf(abs(t_stat), df=len(lam) - 1))

        result[nm] = {
            "lambda_mean": lam_mean,
            "lambda_std": lam_std,
            "t_stat": t_stat,
            "p_value": p_value,
        }

    return result
