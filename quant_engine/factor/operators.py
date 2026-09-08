"""Vectorized operators for multi-asset, point-in-time factor expressions.

The public operators accept a ``Series`` indexed by either ``(date, ticker)``
or the project's native ``(code, date)`` convention.  Time-series work is
performed on a date-by-asset matrix and converted back to the original index,
which prevents rolling windows from crossing instruments.
"""
from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

EPSILON = 1e-12
_ASSET_LEVELS = ("ticker", "code", "symbol")


def _levels(index: pd.Index) -> tuple[str, str]:
    if not isinstance(index, pd.MultiIndex) or index.nlevels != 2:
        raise ValueError("factor input must use a two-level MultiIndex")
    names = list(index.names)
    if "date" not in names:
        raise ValueError("factor input MultiIndex must contain a 'date' level")
    date_level = "date"
    asset_level = next((name for name in _ASSET_LEVELS if name in names), None)
    if asset_level is None:
        asset_level = names[1 - names.index(date_level)]
    if asset_level is None:
        raise ValueError("factor asset index level must be named ticker, code or symbol")
    return date_level, asset_level


def numeric(values: pd.Series) -> pd.Series:
    """Coerce a factor input to finite floating-point observations."""
    result = pd.to_numeric(values, errors="coerce").astype(float)
    return result.replace([np.inf, -np.inf], np.nan)


def to_wide(values: pd.Series) -> pd.DataFrame:
    """Convert a panel Series to ``date x asset`` without aggregating rows."""
    date_level, asset_level = _levels(values.index)
    clean = numeric(values)
    if clean.index.duplicated().any():
        raise ValueError("factor input contains duplicate date/asset rows")
    canonical = clean.reorder_levels([date_level, asset_level]).sort_index()
    return canonical.unstack(asset_level).sort_index()


def from_wide(values: pd.DataFrame, like: pd.Series) -> pd.Series:
    """Restore a date-by-asset matrix to the exact index order of ``like``."""
    date_level, asset_level = _levels(like.index)
    stacked = values.stack(future_stack=True)
    stacked.index = stacked.index.set_names([date_level, asset_level])
    if list(like.index.names) != [date_level, asset_level]:
        stacked = stacked.reorder_levels(list(like.index.names))
    return stacked.reindex(like.index).astype(float)


def delay(values: pd.Series, periods: int = 1) -> pd.Series:
    """Lag within each instrument; negative periods are deliberately rejected."""
    if periods < 0:
        raise ValueError("negative delay would introduce look-ahead bias")
    wide = to_wide(values)
    return from_wide(wide.shift(periods), values)


def delta(values: pd.Series, periods: int = 1) -> pd.Series:
    if periods < 1:
        raise ValueError("delta periods must be >= 1")
    return sub(values, delay(values, periods))


def add(left: pd.Series, right: pd.Series | float) -> pd.Series:
    return numeric(left).add(right)


def sub(left: pd.Series, right: pd.Series | float) -> pd.Series:
    return numeric(left).sub(right)


def mul(left: pd.Series, right: pd.Series | float) -> pd.Series:
    return numeric(left).mul(right)


def div(left: pd.Series, right: pd.Series | float) -> pd.Series:
    denominator = numeric(right) if isinstance(right, pd.Series) else float(right)
    if isinstance(denominator, pd.Series):
        denominator = denominator.where(denominator.abs() > EPSILON)
    elif abs(denominator) <= EPSILON:
        denominator = np.nan
    return numeric(left).div(denominator).replace([np.inf, -np.inf], np.nan)


def inverse(values: pd.Series) -> pd.Series:
    return div(pd.Series(1.0, index=values.index), values)


def signed_power(values: pd.Series, exponent: float) -> pd.Series:
    clean = numeric(values)
    return np.sign(clean) * np.power(clean.abs(), exponent)


def log(values: pd.Series) -> pd.Series:
    clean = numeric(values)
    return np.log(clean.where(clean > 0))


def abs_value(values: pd.Series) -> pd.Series:
    return numeric(values).abs()


def rank(values: pd.Series) -> pd.Series:
    """Percentile rank within each date's cross-section."""
    date_level, _ = _levels(values.index)
    return numeric(values).groupby(level=date_level).rank(method="average", pct=True)


def demean(values: pd.Series) -> pd.Series:
    date_level, _ = _levels(values.index)
    clean = numeric(values)
    return clean - clean.groupby(level=date_level).transform("mean")


def scale(values: pd.Series) -> pd.Series:
    """Cross-sectionally scale absolute exposure to one on every date."""
    date_level, _ = _levels(values.index)
    clean = numeric(values)
    denominator = clean.abs().groupby(level=date_level).transform("sum")
    return div(clean, denominator)


def _rolling(
    values: pd.Series,
    window: int,
    min_periods: int | None,
    operation: Callable[[pd.core.window.rolling.Rolling], pd.DataFrame],
) -> pd.Series:
    if window < 1:
        raise ValueError("rolling window must be >= 1")
    minimum = min_periods if min_periods is not None else max(1, int(np.ceil(window * 0.6)))
    if not 1 <= minimum <= window:
        raise ValueError("min_periods must be between 1 and window")
    wide = to_wide(values)
    result = operation(wide.rolling(window=window, min_periods=minimum))
    return from_wide(result, values)


def ts_mean(values: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    return _rolling(values, window, min_periods, lambda rolling: rolling.mean())


def ts_std(values: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    return _rolling(values, window, min_periods, lambda rolling: rolling.std(ddof=1))


def ts_max(values: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    return _rolling(values, window, min_periods, lambda rolling: rolling.max())


def ts_min(values: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    return _rolling(values, window, min_periods, lambda rolling: rolling.min())


def ts_sum(values: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    return _rolling(values, window, min_periods, lambda rolling: rolling.sum())


def ts_skewness(values: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    return _rolling(values, window, min_periods, lambda rolling: rolling.skew())


def ts_kurtosis(values: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    return _rolling(values, window, min_periods, lambda rolling: rolling.kurt())


def ts_quantile(
    values: pd.Series,
    window: int,
    quantile: float,
    min_periods: int | None = None,
) -> pd.Series:
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be in [0, 1]")
    return _rolling(values, window, min_periods, lambda rolling: rolling.quantile(quantile))


def ts_corr(
    left: pd.Series,
    right: pd.Series,
    window: int,
    min_periods: int | None = None,
) -> pd.Series:
    if window < 2:
        raise ValueError("correlation window must be >= 2")
    minimum = min_periods if min_periods is not None else max(2, int(np.ceil(window * 0.6)))
    left_wide, right_wide = to_wide(left).align(to_wide(right), join="outer")
    result = left_wide.rolling(window, min_periods=minimum).corr(right_wide)
    return from_wide(result, left)


def ts_cov(
    left: pd.Series,
    right: pd.Series,
    window: int,
    min_periods: int | None = None,
) -> pd.Series:
    if window < 2:
        raise ValueError("covariance window must be >= 2")
    minimum = min_periods if min_periods is not None else max(2, int(np.ceil(window * 0.6)))
    left_wide, right_wide = to_wide(left).align(to_wide(right), join="outer")
    result = left_wide.rolling(window, min_periods=minimum).cov(right_wide)
    return from_wide(result, left)


def decay_linear(values: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    """Linearly weighted rolling mean with the newest observation heaviest."""
    minimum = min_periods if min_periods is not None else max(1, int(np.ceil(window * 0.6)))
    weights = np.arange(1.0, window + 1.0)

    def weighted(sample: np.ndarray) -> float:
        usable_weights = weights[-len(sample):]
        valid = np.isfinite(sample)
        if int(valid.sum()) < minimum:
            return np.nan
        return float(np.dot(sample[valid], usable_weights[valid]) / usable_weights[valid].sum())

    return _rolling(values, window, minimum, lambda rolling: rolling.apply(weighted, raw=True))


def ts_slope(values: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    """Least-squares time slope for each rolling instrument window."""
    minimum = min_periods if min_periods is not None else max(2, int(np.ceil(window * 0.6)))

    def slope(sample: np.ndarray) -> float:
        valid = np.isfinite(sample)
        if int(valid.sum()) < minimum:
            return np.nan
        x = np.arange(len(sample), dtype=float)[valid]
        y = sample[valid]
        centered = x - x.mean()
        denominator = np.dot(centered, centered)
        return float(np.dot(centered, y - y.mean()) / denominator) if denominator > 0 else np.nan

    return _rolling(values, window, minimum, lambda rolling: rolling.apply(slope, raw=True))


def winsorize_zscore(
    values: pd.Series,
    lower: float = 0.01,
    upper: float = 0.99,
) -> pd.Series:
    """Point-in-time cross-sectional winsorization followed by Z-score.

    A whole-sample normalization would make an old factor value depend on
    future observations.  Applying the requested 1%/99% treatment within each
    date keeps the transformation deployable and also makes every usable
    cross-section mean-zero and unit-variance.
    """
    if not 0 <= lower < upper <= 1:
        raise ValueError("winsorization bounds must satisfy 0 <= lower < upper <= 1")
    clean = numeric(values)
    if clean.dropna().empty:
        return clean
    date_level, _ = _levels(clean.index)
    grouped = clean.groupby(level=date_level)
    low = grouped.transform("quantile", q=lower)
    high = grouped.transform("quantile", q=upper)
    clipped = clean.clip(lower=low, upper=high)
    clipped_grouped = clipped.groupby(level=date_level)
    mean = clipped_grouped.transform("mean")
    standard_deviation = clipped_grouped.transform("std", ddof=0)
    result = div(sub(clipped, mean), standard_deviation)
    constant = clipped.notna() & standard_deviation.notna() & (standard_deviation <= EPSILON)
    return result.mask(constant, 0.0)
