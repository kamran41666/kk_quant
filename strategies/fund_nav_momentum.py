"""Beginner-friendly domestic fund NAV strategy.

This is intentionally a small, deterministic strategy for the first fund
observation slice.  It ranks the supplied funds by trailing NAV return and
holds the strongest positive fund(s); it never invents an intraday price.
"""
from __future__ import annotations

from datetime import date
from quant_engine.backtest.strategy import Strategy
from quant_engine.backtest.protocol import (
    AnalysisOutputSpec, DataRequirement, ParameterSpec, ParameterType,
    StrategyOutput, StrategySpec,
)


class FundNavMomentumStrategy(Strategy):
    """Daily/weekly positive-momentum allocation over a supplied fund pool."""

    SPEC = StrategySpec(
        id="fund-nav-momentum",
        name="基金 NAV 动量",
        version="2.0.0",
        description="按历史净值收益排序，持有正动量排名靠前的基金。",
        markets=("cn-fund",),
        parameters=(
            ParameterSpec("lookback", "动量周期", ParameterType.INTEGER, 20, minimum=1, maximum=250),
            ParameterSpec("top_n", "持有数量", ParameterType.INTEGER, 1, minimum=1, maximum=50),
            ParameterSpec("target_weight", "总目标仓位", ParameterType.NUMBER, 1.0, minimum=0.0, maximum=1.0),
        ),
        data=(DataRequirement(
            "cn_fund_nav",
            ("nav",),
            2,
            adjustment="none",
            lookback_parameter="lookback",
            lookback_offset=1,
        ),),
        rebalance_frequency="daily",
        warmup_bars=2,
        tags=("基金", "动量"),
        analysis_outputs=(AnalysisOutputSpec("positive_candidates", "正动量候选", "integer"),),
    )

    def initialize(self):
        self.lookback = self.params["lookback"]
        self.top_n = self.params["top_n"]
        self.target_weight = self.params["target_weight"]

    def generate_signals(self, dt: date) -> StrategyOutput:
        history = self.ctx.history(lookback=self.lookback + 1, fields=["nav"])
        scores: list[tuple[str, float]] = []
        for code in self.ctx.universe:
            try:
                usable = history.xs(code, level="code").sort_index()
            except KeyError:
                continue
            if len(usable) < self.lookback + 1:
                continue
            try:
                latest = float(usable.iloc[-1]["nav"])
                base = float(usable.iloc[-1 - self.lookback]["nav"])
            except (TypeError, ValueError):
                continue
            if latest > 0 and base > 0:
                scores.append((str(code), latest / base - 1.0))
        positive = sorted((item for item in scores if item[1] > 0), key=lambda item: item[1], reverse=True)
        selected = positive[: self.top_n]
        if not selected or self.target_weight <= 0:
            return StrategyOutput({}, {"positive_candidates": len(positive)})
        weight = self.target_weight / len(selected)
        return StrategyOutput(
            {code: weight for code, _ in selected},
            {"positive_candidates": len(positive)},
        )
