"""A-share short-term reversal research candidate."""
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
    score_trailing_return,
)


class ResearchShortReversalStrategy(Strategy):
    """Weekly adaptation of Liu, Stambaugh and Yuan, Size and Value in China (2019).

    The paper forms five overlapping daily sub-portfolios, each held for five
    days after a 20-day ranking period.  This candidate uses one weekly target
    portfolio (with 10 days as a robustness setting), so it is not an exact
    reproduction of the paper's implementation. Eligibility requires 120
    tradable observations within the latest 252 exchange sessions,
    independently of the signal window.
    """

    SPEC = StrategySpec(
        id="research-short-reversal",
        name="A股短期反转研究候选",
        version="0.1.1",
        description="按过去短期收益从低到高排序的周频等权研究候选。",
        markets=("a-share",),
        parameters=(
            ParameterSpec("lookback", "反转窗口", ParameterType.INTEGER, 20, minimum=10, maximum=60),
            ParameterSpec("top_n", "持仓数量", ParameterType.INTEGER, 30, minimum=1, maximum=200),
            ParameterSpec("min_amount", "最低20日均成交额", ParameterType.NUMBER, 20_000_000.0, minimum=0.0, maximum=10_000_000_000.0),
            ParameterSpec("gross_exposure", "目标总仓位", ParameterType.NUMBER, 0.90, minimum=0.0, maximum=0.90),
        ),
        data=(DataRequirement(
            "a_share_daily", ("close", "volume", "amount"), 252,
            lookback_parameter="lookback", lookback_offset=1,
        ),),
        rebalance_frequency="weekly",
        warmup_bars=120,
        max_gross_exposure=0.90,
        tags=("研究候选", "短期反转", "A股"),
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
            "scope": "ASHARE_WEEKLY_SHORT_REVERSAL_ADAPTATION",
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
            scorer=lambda item: score_trailing_return(item, self.lookback),
            top_n=self.top_n,
            gross_exposure=self.gross_exposure,
            ascending=True,
        )
