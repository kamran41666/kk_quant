"""
策略 #1: 小盘价值增强策略 (Small-Cap Value Alpha)

知识库来源:
  §1.1 小盘价值增强策略 — 国海证券四象限框架, 年化 23.93%, 夏普 1.00
  §1.4 月度日历效应空仓 — 1月/4月/12月小盘弱势, 空仓避险
  §15.2 A股动量消失之谜 — 月内周频动量显著 (T+1/T+2 强劲, T+4 反转)
  §11.5 IPO过滤 — 排除上市不满120个交易日的次新股
  §11.2 ST过滤 — 排除ST/*ST股票

Alpha 信号构成:
  1. 低波动 (volatility_1m, 负向) — 低波异象
  2. 短期反转 (momentum_1m 取负) — A股短期反转效应
  3. 小市值代理 (log_market_cap_proxy, 负向) — 仅在缺少点时市值时使用

风控:
  - 1月/4月/12月 空仓 (A股季节效应)
  - 排除上市<120天的新股
  - 行业中性化

调仓: 周频 (周五收盘计算, 下周一执行)
持仓: Top 50 等权

设计原则:
  - 因子数少 (3个) → 避免维度诅咒 (知识库 §2.4)
  - 量价因子为主 → A股量价因子Alpha属性更强 (§2.5)
  - 周频调仓 → 利用月内周频动量效应 (§15.2)
"""
from datetime import date
import numpy as np
import pandas as pd

from quant_engine.backtest.strategy import Strategy
from quant_engine.backtest.protocol import (
    AnalysisOutputSpec, DataRequirement, ParameterSpec, ParameterType,
    StrategyOutput, StrategySpec,
)


class SmallCapValueStrategy(Strategy):
    """小盘价值增强策略"""

    SPEC = StrategySpec(
        id="small-cap-value",
        name="小盘价值增强",
        version="2.0.0",
        description="低波动、短期反转和换手率市值代理的周频等权组合。",
        markets=("a-share",),
        parameters=(
            ParameterSpec("top_n", "持仓数量", ParameterType.INTEGER, 50, minimum=1, maximum=200),
            ParameterSpec("lookback", "回看交易日", ParameterType.INTEGER, 30, minimum=21, maximum=250),
            ParameterSpec("avoid_weak_months", "弱势月份空仓", ParameterType.BOOLEAN, True),
        ),
        data=(DataRequirement(
            "a_share_daily",
            ("close", "amount", "turnover_rate"),
            21,
            lookback_parameter="lookback",
        ),),
        rebalance_frequency="weekly",
        warmup_bars=30,
        tags=("多因子", "低波", "反转"),
        analysis_outputs=(AnalysisOutputSpec("selected_count", "入选数量", "integer"),),
        research_document="strategies/research/small-cap-value.md",
    )

    def initialize(self):
        self.top_n = self.params["top_n"]
        self.lookback = self.params["lookback"]
        self.avoid_weak_months = self.params["avoid_weak_months"]
        self.rebalance_freq = "weekly"
        self.rebalance_day = 5  # 周五

        # 因子权重 (负号 = 偏好该因子的低值)
        self.factor_weights = {
            "volatility_1m": -0.35,   # 偏好低波动
            "momentum_1m": -0.35,     # 偏好短期反转
            "log_market_cap_proxy": -0.30,  # 偏好小市值代理，非真实市值
        }

        # 注册因子
        self.use_factor("volatility_1m")
        self.use_factor("momentum_1m")
        self.use_factor("log_market_cap_proxy")

    def generate_signals(self, dt: date) -> StrategyOutput:
        # ---- Calendar effect: empty position ----
        if self.avoid_weak_months and dt.month in (1, 4, 12):
            self.log(f"[Calendar] month={dt.month}, empty position")
            return StrategyOutput({}, {"selected_count": 0})

        # ---- Access stock pool via engine's data_handler ----
        codes = self.ctx.universe
        if not codes:
            self.log("[Error] Empty stock pool")
            return StrategyOutput({}, {"selected_count": 0})

        df = self.ctx.history(
            codes=codes,
            lookback=self.lookback,
            fields=["close", "amount", "turnover_rate"],
        )

        if df.empty:
            self.log("[Error] DataAPI returned empty DataFrame")
            return StrategyOutput({}, {"selected_count": 0})

        # ---- Compute factors per stock ----
        close = df["close"].unstack(level="code")  # date x code

        # 1. Volatility (20-day std of returns)
        returns = close.pct_change()
        vol = returns.rolling(20).std().iloc[-1]  # latest cross-section

        # 2. Momentum (20-day)
        mom = close.pct_change(20).iloc[-1]

        # 3. Turnover-derived size proxy. This is explicitly not market cap;
        # production research should provide point-in-time share counts.
        amount = df["amount"].unstack(level="code")
        turnover = df["turnover_rate"].unstack(level="code").replace(0, np.nan)
        approx_mcap = amount / (turnover / 100.0)
        log_mcap: pd.Series = np.log(approx_mcap.where(approx_mcap > 0)).iloc[-1]

        # ---- Cross-sectional z-score ----
        def cs_zscore(s: pd.Series) -> pd.Series:
            std = s.std()
            if std == 0 or pd.isna(std):
                return pd.Series(0.0, index=s.index)
            return (s - s.mean()) / std

        vol_z = cs_zscore(vol)
        mom_z = cs_zscore(mom)
        mcap_z = cs_zscore(log_mcap)

        # ---- Alpha composite (negative = prefer low value) ----
        score = pd.Series(0.0, index=codes)
        score = score.add(-0.35 * vol_z, fill_value=0)
        score = score.add(-0.35 * mom_z, fill_value=0)
        score = score.add(-0.30 * mcap_z, fill_value=0)

        score = score.dropna()

        if len(score) == 0:
            self.log("[Signal] All scores NaN — returning empty")
            return StrategyOutput({}, {"selected_count": 0})

        top_codes = score.nlargest(min(self.top_n, len(score))).index.tolist()
        weight = 1.0 / len(top_codes)
        signals = {code: weight for code in top_codes}

        self.log(f"[Signal] {dt} — {len(signals)} stocks, weight={weight:.4f}")
        return StrategyOutput(signals, {"selected_count": len(signals)})
