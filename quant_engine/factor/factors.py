"""Point-in-time raw materials and nonlinear OHLCV alpha candidates.

All price changes use explicitly lagged prices.  Consequently a factor stamped
on session T never needs session T's close to form a return or price-derived
change.  Volume-only features may use T volume because this project generates
signals after the daily bar closes and executes them at T+1 open.
"""
from __future__ import annotations

import pandas as pd

from quant_engine.factor.catalog import register_factor
from quant_engine.factor.operators import (
    abs_value,
    add,
    decay_linear,
    delay,
    div,
    inverse,
    log,
    mul,
    rank,
    sub,
    ts_corr,
    ts_kurtosis,
    ts_max,
    ts_mean,
    ts_min,
    ts_quantile,
    ts_skewness,
    ts_std,
    winsorize_zscore,
)


def _column(df: pd.DataFrame, name: str) -> pd.Series:
    if not isinstance(df.index, pd.MultiIndex) or df.index.nlevels != 2:
        raise ValueError("factor data must use a two-level MultiIndex")
    if "date" not in df.index.names:
        raise ValueError("factor data MultiIndex must contain a 'date' level")
    if name not in df.columns:
        raise ValueError(f"factor input is missing required field: {name}")
    return pd.to_numeric(df[name], errors="coerce").astype(float)


def _finish(values: pd.Series, name: str) -> pd.Series:
    return winsorize_zscore(values).rename(name)


def _lagged_return(close: pd.Series, periods: int = 1) -> pd.Series:
    """Return ending at T-1, so the T row never consumes close[T]."""
    end = delay(close, 1)
    start = delay(close, periods + 1)
    return sub(div(end, start), 1.0)


@register_factor(
    name="raw_return_lagged_1_close", category="raw_price", inputs=("close",), window=3,
    formula="C[t-1] / C[t-2] - 1", logic="上一完整交易日收益。",
    operators=("delay", "div", "sub"),
)
def factor_raw_return_lagged_1_close(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：滞后一日收益原料
    金融逻辑：提供不含 T 日收盘的最短周期价格变化。
    计算公式：C[t-1] / C[t-2] - 1
    创新点：delay、div、sub；作为高级交互的基础原料。
    """
    return _finish(_lagged_return(_column(df, "close")), "raw_return_lagged_1_close")


@register_factor(
    name="raw_momentum_lagged_5_close", category="raw_price", inputs=("close",), window=7,
    formula="C[t-1] / C[t-6] - 1", logic="一周滞后动量。",
    operators=("delay", "div", "sub"),
)
def factor_raw_momentum_lagged_5_close(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：五日滞后动量原料
    金融逻辑：刻画不含当日收盘的一周价格持续性。
    计算公式：C[t-1] / C[t-6] - 1
    创新点：delay、div、sub。
    """
    return _finish(_lagged_return(_column(df, "close"), 5), "raw_momentum_lagged_5_close")


@register_factor(
    name="raw_momentum_lagged_20_close", category="raw_price", inputs=("close",), window=22,
    formula="C[t-1] / C[t-21] - 1", logic="一个月滞后动量。",
    operators=("delay", "div", "sub"),
)
def factor_raw_momentum_lagged_20_close(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：二十日滞后动量原料
    金融逻辑：刻画不含当日收盘的中短期趋势。
    计算公式：C[t-1] / C[t-21] - 1
    创新点：delay、div、sub。
    """
    return _finish(_lagged_return(_column(df, "close"), 20), "raw_momentum_lagged_20_close")


@register_factor(
    name="raw_intraday_lagged_1_ohlc", category="raw_price", inputs=("open", "close"), window=2,
    formula="(C[t-1]-O[t-1]) / O[t-1]", logic="上一交易日的日内买卖压力。",
    operators=("delay", "sub", "div"),
)
def factor_raw_intraday_lagged_1_ohlc(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：滞后一日日内收益原料
    金融逻辑：刻画上一完整交易日开盘到收盘的方向性压力。
    计算公式：(C[t-1]-O[t-1]) / O[t-1]
    创新点：delay、sub、div。
    """
    open_lag = delay(_column(df, "open"), 1)
    close_lag = delay(_column(df, "close"), 1)
    return _finish(div(sub(close_lag, open_lag), open_lag), "raw_intraday_lagged_1_ohlc")


@register_factor(
    name="raw_range_lagged_1_ohlc", category="raw_price", inputs=("high", "low", "close"), window=2,
    formula="(H[t-1]-L[t-1]) / C[t-1]", logic="上一交易日振幅。",
    operators=("delay", "sub", "div"),
)
def factor_raw_range_lagged_1_ohlc(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：滞后一日振幅原料
    金融逻辑：衡量上一完整交易日的价格分歧和交易风险。
    计算公式：(H[t-1]-L[t-1]) / C[t-1]
    创新点：delay、sub、div。
    """
    high_lag = delay(_column(df, "high"), 1)
    low_lag = delay(_column(df, "low"), 1)
    close_lag = delay(_column(df, "close"), 1)
    return _finish(div(sub(high_lag, low_lag), close_lag), "raw_range_lagged_1_ohlc")


@register_factor(
    name="raw_volume_change_2_volume", category="raw_volume", inputs=("volume",), window=3,
    formula="log(V[t]) - log(V[t-2])", logic="两日成交量变化。",
    operators=("log", "delay", "sub"),
)
def factor_raw_volume_change_2_volume(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：两日对数成交量变化原料
    金融逻辑：刻画交易关注度的短期扩张或收缩。
    计算公式：log(V[t]) - log(V[t-2])
    创新点：log、delay、sub。
    """
    volume = _column(df, "volume")
    return _finish(sub(log(volume), log(delay(volume, 2))), "raw_volume_change_2_volume")


@register_factor(
    name="raw_relative_volume_20_volume", category="raw_volume", inputs=("volume",), window=20,
    formula="V[t] / mean_20(V)", logic="当前成交量相对常态的异常程度。",
    operators=("ts_mean", "div", "rank"),
)
def factor_raw_relative_volume_20_volume(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：二十日相对成交量原料
    金融逻辑：识别放量或缩量状态。
    计算公式：V[t] / mean_20(V)
    创新点：ts_mean、div、rank。
    """
    volume = _column(df, "volume")
    return _finish(rank(div(volume, ts_mean(volume, 20, 12))), "raw_relative_volume_20_volume")


@register_factor(
    name="raw_turnover_mean_20_turnover", category="raw_liquidity", inputs=("turnover_rate",), window=20,
    formula="rank(mean_20(turnover_rate))", logic="中短期换手活跃度。",
    operators=("ts_mean", "rank", "winsorize_zscore"),
)
def factor_raw_turnover_mean_20_turnover(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：二十日平均换手原料
    金融逻辑：衡量筹码交换速度与流动性活跃程度。
    计算公式：mean_20(turnover_rate)
    创新点：ts_mean 后进行截面 rank，并统一稳健标准化。
    """
    return _finish(rank(ts_mean(_column(df, "turnover_rate"), 20, 12)), "raw_turnover_mean_20_turnover")


@register_factor(
    name="raw_amihud_illiquidity_20_amount", category="raw_liquidity", inputs=("close", "amount"), window=22,
    formula="mean_20(|R_lag1| / Amount[t-1])", logic="单位成交额引发的价格冲击。",
    operators=("delay", "abs", "div", "ts_mean"),
)
def factor_raw_amihud_illiquidity_20_amount(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：二十日 Amihud 非流动性原料
    金融逻辑：单位成交额对应的绝对收益越高，市场深度越弱。
    计算公式：mean_20(|R[t-1]| / Amount[t-1])
    创新点：delay、abs、div、ts_mean。
    """
    returns = abs_value(_lagged_return(_column(df, "close")))
    lagged_amount = delay(_column(df, "amount"), 1)
    raw = ts_mean(div(returns, lagged_amount), 20, 12)
    return _finish(raw, "raw_amihud_illiquidity_20_amount")


@register_factor(
    name="raw_realized_volatility_20_close", category="raw_risk", inputs=("close",), window=22,
    formula="std_20(R_lag1)", logic="已实现波动风险。",
    operators=("delay", "div", "ts_std"),
)
def factor_raw_realized_volatility_20_close(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：二十日已实现波动率原料
    金融逻辑：衡量过去完整日收益的不确定性。
    计算公式：std_20(R[t-1])
    创新点：delay、div、ts_std。
    """
    raw = ts_std(_lagged_return(_column(df, "close")), 20, 12)
    return _finish(raw, "raw_realized_volatility_20_close")


@register_factor(
    name="raw_return_skewness_20_close", category="raw_risk", inputs=("close",), window=22,
    formula="skew_20(R_lag1)", logic="收益分布方向性尾部。",
    operators=("delay", "div", "ts_skewness"),
)
def factor_raw_return_skewness_20_close(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：二十日收益偏度原料
    金融逻辑：区分上行长尾与下行长尾风险。
    计算公式：skew_20(R[t-1])
    创新点：delay、div、ts_skewness。
    """
    raw = ts_skewness(_lagged_return(_column(df, "close")), 20, 12)
    return _finish(raw, "raw_return_skewness_20_close")


@register_factor(
    name="raw_return_kurtosis_20_close", category="raw_risk", inputs=("close",), window=22,
    formula="kurt_20(R_lag1)", logic="收益分布肥尾程度。",
    operators=("delay", "div", "ts_kurtosis"),
)
def factor_raw_return_kurtosis_20_close(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：二十日收益峰度原料
    金融逻辑：识别跳跃和极端收益更常见的证券。
    计算公式：kurt_20(R[t-1])
    创新点：delay、div、ts_kurtosis。
    """
    raw = ts_kurtosis(_lagged_return(_column(df, "close")), 20, 12)
    return _finish(raw, "raw_return_kurtosis_20_close")


@register_factor(
    name="raw_earnings_yield_1_fundamental", category="raw_fundamental", inputs=("pe",), window=1,
    formula="rank(1 / PE)", logic="每单位价格对应的盈利。",
    operators=("inverse", "rank", "winsorize_zscore"),
)
def factor_raw_earnings_yield_1_fundamental(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：盈利收益率原料
    金融逻辑：PE 的倒数，高值代表更高的账面盈利回报。
    计算公式：1 / PE
    创新点：inverse、rank，并统一缩尾和标准化。
    """
    pe = _column(df, "pe").where(_column(df, "pe") > 0)
    return _finish(rank(inverse(pe)), "raw_earnings_yield_1_fundamental")


@register_factor(
    name="raw_book_to_price_1_fundamental", category="raw_fundamental", inputs=("pb",), window=1,
    formula="rank(1 / PB)", logic="每单位价格对应的账面净资产。",
    operators=("inverse", "rank", "winsorize_zscore"),
)
def factor_raw_book_to_price_1_fundamental(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：账面市值比原料
    金融逻辑：PB 的倒数，描述价值风格暴露。
    计算公式：1 / PB
    创新点：inverse、rank，并统一缩尾和标准化。
    """
    pb = _column(df, "pb").where(_column(df, "pb") > 0)
    return _finish(rank(inverse(pb)), "raw_book_to_price_1_fundamental")


@register_factor(
    name="raw_roe_rank_1_fundamental", category="raw_fundamental", inputs=("roe",), window=1,
    formula="rank(ROE)", logic="净资产盈利质量。",
    operators=("rank", "winsorize", "zscore"),
)
def factor_raw_roe_rank_1_fundamental(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：ROE 截面质量原料
    金融逻辑：在同一信息日识别净资产回报更高的公司。
    计算公式：rank(ROE)
    创新点：rank 与稳健缩尾、标准化组合。
    """
    return _finish(rank(_column(df, "roe")), "raw_roe_rank_1_fundamental")


@register_factor(
    name="alpha002_lag_safe_6_ohlcv", category="alpha101_material", inputs=("open", "close", "volume"), window=9,
    formula="-corr_6(rank(log(V[t])-log(V[t-2])), rank((C[t-1]-O[t-1])/O[t-1]))",
    logic="量价背离：偏好成交量变化与滞后日内收益方向相反的证券。",
    operators=("sub", "div", "rank", "ts_corr"),
)
def factor_alpha002_lag_safe_6_ohlcv(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：Alpha#2 防未来版本
    金融逻辑：做多量价背离，做空量价同步；价格腿整体滞后一日。
    计算公式：-corr_6(rank(log(V[t])-log(V[t-2])), rank((C[t-1]-O[t-1])/O[t-1]))
    创新点：sub、div、rank、ts_corr 四类算子嵌套。
    """
    volume = _column(df, "volume")
    open_lag = delay(_column(df, "open"), 1)
    close_lag = delay(_column(df, "close"), 1)
    volume_change = rank(sub(log(volume), log(delay(volume, 2))))
    intraday_return = rank(div(sub(close_lag, open_lag), open_lag))
    raw = mul(ts_corr(volume_change, intraday_return, 6, 4), -1.0)
    return _finish(raw, "alpha002_lag_safe_6_ohlcv")


@register_factor(
    name="volume_price_divergence_corr_10_ohlcv", category="advanced", inputs=("close", "volume"), window=22,
    formula="-corr_10(rank(R_lag1),rank(V/mean_20(V))) / (1+std_20(R_lag1))",
    logic="在波动惩罚后捕捉量价背离。",
    operators=("div", "rank", "ts_corr", "ts_std", "inverse", "mul"),
)
def factor_volume_price_divergence_corr_10_ohlcv(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：波动约束的十日量价背离
    金融逻辑：量价持续反向且历史波动较低时给出更强信号。
    计算公式：-corr_10(rank(R_lag1), rank(V/mean_20(V))) × 1/(1+std_20(R_lag1))
    创新点：div、rank、ts_corr、ts_std、inverse、mul 的非线性交互。
    """
    returns = _lagged_return(_column(df, "close"))
    volume = _column(df, "volume")
    relative_volume = div(volume, ts_mean(volume, 20, 12))
    divergence = mul(ts_corr(rank(returns), rank(relative_volume), 10, 6), -1.0)
    stability = inverse(add(ts_std(returns, 20, 12), 1.0))
    return _finish(mul(divergence, stability), "volume_price_divergence_corr_10_ohlcv")


@register_factor(
    name="breakout_dryup_quantile_20_ohlcv", category="advanced", inputs=("high", "low", "close", "volume"), window=22,
    formula="rank(pos_20) * rank(1/(V/mean_20(V))) * rank(q20(range,0.25)/range)",
    logic="识别价格靠近区间高位、成交缩量且振幅压缩的潜在供给枯竭。",
    operators=("sub", "div", "inverse", "ts_min", "ts_max", "ts_quantile", "rank", "mul"),
)
def factor_breakout_dryup_quantile_20_ohlcv(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：二十日缩量压缩突破
    金融逻辑：价格处于区间高位但成交量和振幅同步收缩，刻画卖压枯竭而非简单动量。
    计算公式：rank(pos_20) × rank(1/rel_volume_20) × rank(q25(range_20)/range)
    创新点：ts_min、ts_max、ts_quantile、inverse、div、rank、mul 多层交互。
    """
    close_lag = delay(_column(df, "close"), 1)
    high_lag = delay(_column(df, "high"), 1)
    low_lag = delay(_column(df, "low"), 1)
    volume = _column(df, "volume")
    floor = ts_min(close_lag, 20, 12)
    ceiling = ts_max(close_lag, 20, 12)
    position = div(sub(close_lag, floor), sub(ceiling, floor))
    relative_volume = div(volume, ts_mean(volume, 20, 12))
    daily_range = div(sub(high_lag, low_lag), close_lag)
    compression = div(ts_quantile(daily_range, 20, 0.25, 12), daily_range)
    raw = mul(mul(rank(position), rank(inverse(relative_volume))), rank(compression))
    return _finish(raw, "breakout_dryup_quantile_20_ohlcv")


@register_factor(
    name="liquidity_shock_reversal_decay_20_ohlcv", category="advanced", inputs=("close", "amount"), window=22,
    formula="decay_5(rank(-R_lag1)*rank(|R_lag1|/mean_20(Amount[t-1])))",
    logic="低流动性价格冲击后的短期反转。",
    operators=("delay", "abs", "div", "ts_mean", "rank", "mul", "decay_linear"),
)
def factor_liquidity_shock_reversal_decay_20_ohlcv(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：流动性冲击衰减反转
    金融逻辑：成交承载能力弱时的大幅下跌更可能包含暂时性价格压力。
    计算公式：decay_5(rank(-R_lag1) × rank(|R_lag1|/mean_20(Amount[t-1])))
    创新点：delay、abs、div、ts_mean、rank、mul、decay_linear 嵌套。
    """
    returns = _lagged_return(_column(df, "close"))
    lagged_amount = delay(_column(df, "amount"), 1)
    price_impact = div(abs_value(returns), ts_mean(lagged_amount, 20, 12))
    interaction = mul(rank(mul(returns, -1.0)), rank(price_impact))
    return _finish(decay_linear(interaction, 5, 3), "liquidity_shock_reversal_decay_20_ohlcv")


@register_factor(
    name="tail_asymmetry_rank_20_close", category="advanced", inputs=("close",), window=22,
    formula="rank(-skew_20(R_lag1))*rank(kurt_20(R_lag1)/(1+std_20(R_lag1)))",
    logic="区分负偏肥尾与普通高波动。",
    operators=("ts_skewness", "ts_kurtosis", "ts_std", "div", "rank", "mul"),
)
def factor_tail_asymmetry_rank_20_close(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：二十日尾部不对称风险
    金融逻辑：负偏度和高峰度共同出现但常规波动不足以解释时，识别尾部风险溢价。
    计算公式：rank(-skew_20(R_lag1)) × rank(kurt_20(R_lag1)/(1+std_20(R_lag1)))
    创新点：ts_skewness、ts_kurtosis、ts_std、div、rank、mul 高阶矩交互。
    """
    returns = _lagged_return(_column(df, "close"))
    negative_skew = mul(ts_skewness(returns, 20, 12), -1.0)
    standardized_tail = div(ts_kurtosis(returns, 20, 12), add(ts_std(returns, 20, 12), 1.0))
    return _finish(mul(rank(negative_skew), rank(standardized_tail)), "tail_asymmetry_rank_20_close")


@register_factor(
    name="range_compression_volume_release_20_ohlcv", category="advanced", inputs=("open", "high", "low", "close", "volume"), window=22,
    formula="rank(q25(range_20)/range)*rank(|C-O|/(H-L))*rank(V/mean_20(V))",
    logic="长期压缩后伴随实体与成交释放的状态跃迁。",
    operators=("sub", "abs", "div", "ts_quantile", "ts_mean", "rank", "mul"),
)
def factor_range_compression_volume_release_20_ohlcv(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：振幅压缩后的量能释放
    金融逻辑：过去完整日处于低振幅区且实体占比上升，同时当前成交量释放，刻画状态切换。
    计算公式：rank(q25(range_20)/range) × rank(|C-O|/(H-L)) × rank(V/mean_20(V))
    创新点：sub、abs、div、ts_quantile、ts_mean、rank、mul 三路非线性交互。
    """
    open_lag = delay(_column(df, "open"), 1)
    high_lag = delay(_column(df, "high"), 1)
    low_lag = delay(_column(df, "low"), 1)
    close_lag = delay(_column(df, "close"), 1)
    volume = _column(df, "volume")
    price_range = div(sub(high_lag, low_lag), close_lag)
    compression = div(ts_quantile(price_range, 20, 0.25, 12), price_range)
    body_share = div(abs_value(sub(close_lag, open_lag)), sub(high_lag, low_lag))
    volume_release = div(volume, ts_mean(volume, 20, 12))
    raw = mul(mul(rank(compression), rank(body_share)), rank(volume_release))
    return _finish(raw, "range_compression_volume_release_20_ohlcv")
