"""收益/风险指标计算

所有指标基于 daily_portfolio DataFrame 或 trades DataFrame。
从 daily_portfolio 提取 daily_return 列作为 pd.Series 输入。
"""
from datetime import date
from typing import Optional, Tuple

import numpy as np
import pandas as pd


def annual_return(
    daily_returns: pd.Series, periods_per_year: int = 252
) -> float:
    """年化收益率

    Args:
        daily_returns: 日收益率序列 (非累计)
        periods_per_year: 年交易日数, A 股默认 252

    Returns:
        年化收益率 (小数形式, 如 0.15 表示 15%)
    """
    daily_returns = daily_returns.dropna()
    if len(daily_returns) == 0:
        return 0.0
    total_return = (1 + daily_returns).prod()
    n_days = len(daily_returns)
    return float(total_return ** (periods_per_year / n_days) - 1)


def annual_volatility(
    daily_returns: pd.Series, periods_per_year: int = 252
) -> float:
    """年化波动率

    Args:
        daily_returns: 日收益率序列
        periods_per_year: 年交易日数

    Returns:
        年化波动率 (标准差)
    """
    daily_returns = daily_returns.dropna()
    if len(daily_returns) < 2:
        return 0.0
    return float(daily_returns.std(ddof=1) * np.sqrt(periods_per_year))


def downside_volatility(
    daily_returns: pd.Series, periods_per_year: int = 252
) -> float:
    """下行波动率 (只计入负收益)

    Args:
        daily_returns: 日收益率序列
        periods_per_year: 年交易日数

    Returns:
        年化下行波动率
    """
    daily_returns = daily_returns.dropna()
    negative = daily_returns[daily_returns < 0]
    if len(negative) < 2:
        return 0.0
    return float(negative.std(ddof=1) * np.sqrt(periods_per_year))


def max_drawdown(
    daily_returns: pd.Series,
) -> Tuple[float, Optional[date], Optional[date], int]:
    """最大回撤

    从 daily_return 重建累计净值曲线, 精确计算:
    峰值 → 谷底 → 恢复至前峰值所需天数

    Args:
        daily_returns: 日收益率序列, index 为 date/timestamp

    Returns:
        (最大回撤率, 峰值日, 谷底日, 恢复天数)
        - 最大回撤率: 负值 (如 -0.25 表示 -25%)
        - 恢复天数: 从峰值到恢复的天数; 若始终未恢复则为区间末尾至今的天数; 若回撤从第一笔开始则为 0
    """
    daily_returns = daily_returns.dropna()
    if len(daily_returns) == 0:
        return (0.0, None, None, 0)

    # 累计净值
    cumulative = (1 + daily_returns).cumprod()
    running_max = cumulative.cummax()

    # 回撤序列: (当前/峰值 - 1), 始终 <= 0
    drawdown = cumulative / running_max - 1

    # 最大回撤位置
    idx_trough = drawdown.idxmin()  # 最深的点
    mdd = float(drawdown.min())

    # 峰值日: 在谷底之前的最高点
    trough_pos = drawdown.index.get_loc(idx_trough)
    cumul_before_trough = cumulative.iloc[: trough_pos + 1]
    idx_peak = cumul_before_trough.idxmax()

    peak_date = _to_date(idx_peak)
    trough_date = _to_date(idx_trough)

    # 恢复天数: 从峰值到第一次回到或超过峰值的交易日数
    if trough_pos < len(cumulative) - 1:
        after_trough = cumulative.iloc[trough_pos + 1:]
        recovered = after_trough[after_trough >= running_max.iloc[trough_pos]]
        if len(recovered) > 0:
            recovery_idx = recovered.index[0]
            recovery_days = (cumulative.index.get_loc(recovery_idx)
                             - cumulative.index.get_loc(idx_peak))
        else:
            # 从未恢复
            last_idx = cumulative.index[-1]
            recovery_days = (cumulative.index.get_loc(last_idx)
                             - cumulative.index.get_loc(idx_peak))
    else:
        # 谷底在最后一天, 无法恢复
        recovery_days = len(cumulative) - 1 - cumulative.index.get_loc(idx_peak)

    return (mdd, peak_date, trough_date, recovery_days)


def sharpe_ratio(daily_returns: pd.Series, rf: float = 0.02) -> float:
    """夏普比率

    Sharpe = (年化收益 - 无风险利率) / 年化波动率

    Args:
        daily_returns: 日收益率序列
        rf: 年化无风险利率 (默认 0.02)

    Returns:
        夏普比率
    """
    daily_returns = daily_returns.dropna()
    if len(daily_returns) < 2:
        return 0.0
    excess = daily_returns.mean() * 252 - rf
    vol = annual_volatility(daily_returns)
    if vol == 0:
        return 0.0
    return float(excess / vol)


def sortino_ratio(daily_returns: pd.Series, rf: float = 0.02) -> float:
    """索提诺比率

    Sortino = (年化收益 - 无风险利率) / 年化下行波动率

    Args:
        daily_returns: 日收益率序列
        rf: 年化无风险利率 (默认 0.02)

    Returns:
        索提诺比率
    """
    daily_returns = daily_returns.dropna()
    if len(daily_returns) < 2:
        return 0.0
    excess = daily_returns.mean() * 252 - rf
    down_vol = downside_volatility(daily_returns)
    if down_vol == 0:
        # 无下行波动 → 若收益为正则为 +inf, 否则为 0
        return float("inf") if excess > 0 else 0.0
    return float(excess / down_vol)


def calmar_ratio(daily_returns: pd.Series) -> float:
    """卡玛比率 = 年化收益 / |最大回撤|

    只关注下行风险, 回撤越小 (即 |mdd| 小) → Calmar 越大

    Args:
        daily_returns: 日收益率序列

    Returns:
        卡玛比率
    """
    daily_returns = daily_returns.dropna()
    if len(daily_returns) < 2:
        return 0.0
    ann_ret = annual_return(daily_returns)
    mdd, _, _, _ = max_drawdown(daily_returns)
    if mdd == 0:
        return 0.0
    return float(ann_ret / abs(mdd))


def win_rate(trades_df: pd.DataFrame) -> float:
    """胜率 — 盈利交易笔数 / 总交易笔数

    每笔交易的盈亏由 amount 字段决定: 卖出为正, 买入为负 (建仓成本)。
    简化处理: 以 amount 加佣金和印花税后的净额判断盈亏。

    Args:
        trades_df: trades.parquet DataFrame
                   columns: trade_id, code, side, shares, price,
                            amount, commission, stamp_duty, ...

    Returns:
        胜率 (0.0 ~ 1.0)
    """
    if trades_df.empty:
        return 0.0

    # 按 trade_id 合并多腿 (如果有)
    # 简单处理: 每笔成交的净现金流 = amount - commission - stamp_duty
    # 卖出amount为正 → 盈利; 买入amount为负 → 无法单独判断盈亏
    # 所以我们用 trade 的 side 来判断: 只看卖出交易
    sells = trades_df[trades_df["side"] == "sell"]
    if sells.empty:
        return 0.0

    # 卖出交易的净收益
    net_pnl = sells["amount"] - sells["commission"] - sells["stamp_duty"]
    wins = (net_pnl > 0).sum()
    return float(wins / len(sells))


def profit_loss_ratio(trades_df: pd.DataFrame) -> float:
    """盈亏比 — 平均盈利 / 平均亏损 (绝对值)

    Args:
        trades_df: trades.parquet DataFrame

    Returns:
        盈亏比; 若无亏损则返回 inf; 若无盈利则返回 0
    """
    if trades_df.empty:
        return 0.0

    sells = trades_df[trades_df["side"] == "sell"]
    if sells.empty:
        return 0.0

    net_pnl = sells["amount"] - sells["commission"] - sells["stamp_duty"]
    gains = net_pnl[net_pnl > 0]
    losses = net_pnl[net_pnl < 0]

    if losses.empty:
        return float("inf")
    if gains.empty:
        return 0.0

    avg_gain = gains.mean()
    avg_loss = abs(losses.mean())
    if avg_loss == 0:
        return float("inf")

    return float(avg_gain / avg_loss)


def _to_date(idx) -> Optional[date]:
    """将 pandas index 值转为 datetime.date"""
    if idx is None:
        return None
    val = idx
    if hasattr(val, "date"):
        return val.date()
    if isinstance(val, date):
        return val
    return pd.Timestamp(val).date()
