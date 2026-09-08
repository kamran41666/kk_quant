"""A-share low-volatility research candidate."""
from __future__ import annotations

from datetime import date

from quant_engine.backtest.protocol import (
    AnalysisOutputSpec,
    DataRequirement,
    ParameterSpec,
    ParameterType,
    StrategyOutput,
    StrategySpec,
)
from quant_engine.backtest.strategy import Strategy
from strategies._research_selection import (
    build_selection_output,
    eligible_histories,
    requested_history_bars,
    score_low_volatility,
)


class ResearchLowVolatilityStrategy(Strategy):
    """Monthly adaptation of Blitz et al., The Volatility Effect in China (2021).

    The paper's China test uses 36 months of monthly returns.  This candidate
    instead ranks 252 trading days of daily-return volatility (60 is a planned
    robustness setting), so it is an adaptation rather than an exact replica.
    Eligibility requires 120 tradable observations within the latest 252
    exchange sessions, independently of the signal window.
    """

    SPEC = StrategySpec(
        id="research-low-volatility",
        name="A股低波动研究候选",
        version="0.1.1",
        description="按过去日收益波动率从低到高排序的月频等权研究候选。",
        markets=("a-share",),
        parameters=(
            ParameterSpec("lookback", "波动率窗口", ParameterType.INTEGER, 252, minimum=60, maximum=504),
            ParameterSpec("top_n", "持仓数量", ParameterType.INTEGER, 30, minimum=1, maximum=200),
            ParameterSpec("min_amount", "最低20日均成交额", ParameterType.NUMBER, 20_000_000.0, minimum=0.0, maximum=10_000_000_000.0),
            ParameterSpec("gross_exposure", "目标总仓位", ParameterType.NUMBER, 0.90, minimum=0.0, maximum=0.90),
        ),
        data=(DataRequirement(
            "a_share_daily", ("close", "volume", "amount"), 252,
            lookback_parameter="lookback", lookback_offset=1,
        ),),
        rebalance_frequency="monthly",
        warmup_bars=120,
        max_gross_exposure=0.90,
        tags=("研究候选", "低波动", "A股"),
        analysis_outputs=(
            AnalysisOutputSpec("eligible_count", "有效候选数", "integer"),
            AnalysisOutputSpec("selected_count", "入选数量", "integer"),
            AnalysisOutputSpec("gross_exposure", "目标总仓位", "number"),
        ),
        research_document="docs/research-runs/ashare-multi-strategy-research.md",
        extensions={
            "research_gate_required": True,
            "research_engine_required": True,
            "research_status": "blocked_by_research_gate",
            "scope": "ASHARE_DAILY_LOW_VOLATILITY_ADAPTATION",
        },
    )

    def initialize(self) -> None:
        self.lookback = self.params["lookback"]
        self.top_n = self.params["top_n"]
        self.min_amount = self.params["min_amount"]
        self.gross_exposure = self.params["gross_exposure"]

    def generate_signals(self, dt: date) -> StrategyOutput:
        universe = self.ctx.universe
        if not universe:
            return build_selection_output(
                {}, scorer=lambda item: None, top_n=self.top_n,
                gross_exposure=self.gross_exposure, ascending=True,
            )
        history = self.ctx.history(
            codes=universe,
            lookback=requested_history_bars(self.lookback),
            fields=["close", "volume", "amount"],
        )
        eligible = eligible_histories(history, as_of=dt, min_amount=self.min_amount)
        return build_selection_output(
            eligible,
            scorer=lambda item: score_low_volatility(item, self.lookback),
            top_n=self.top_n,
            gross_exposure=self.gross_exposure,
            ascending=True,
        )
