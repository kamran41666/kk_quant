"""Build a frozen Strategy Protocol class from a validated expression."""
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
from quant_engine.factor.expression import FactorExpressionSpec


def build_expression_rank_strategy(
    expression: FactorExpressionSpec,
    *,
    strategy_version: str = "0.1.0",
    rebalance_frequency: str = "weekly",
    bundle_hash: str | None = None,
) -> type[Strategy]:
    """Create a research-only equal-weight rank strategy with frozen metadata."""
    if expression.role != "rank":
        raise ValueError("only rank factor expressions can become standalone rank strategies")
    if rebalance_frequency not in {"weekly", "monthly"}:
        raise ValueError("factor rank strategy supports weekly or monthly rebalancing")
    factor_name = expression.name
    factor_hash = expression.expression_hash
    required_history = max(252, expression.lookback)

    class FrozenExpressionRankStrategy(Strategy):
        SPEC = StrategySpec(
            id=f"factor-{factor_hash[:16]}",
            name=f"因子组合：{factor_name}",
            version=strategy_version,
            description="冻结受限表达式的等权多头研究组合。",
            markets=("a-share",),
            parameters=(
                ParameterSpec(
                    "top_n", "持仓数量", ParameterType.INTEGER, 20,
                    minimum=9, maximum=100,
                ),
                ParameterSpec(
                    "gross_exposure", "目标总仓位", ParameterType.NUMBER, 0.90,
                    minimum=0.10, maximum=0.90,
                ),
            ),
            data=(DataRequirement(
                "a_share_daily",
                (),
                required_history,
                factors=(factor_name,),
            ),),
            rebalance_frequency=rebalance_frequency,
            warmup_bars=required_history,
            max_gross_exposure=0.90,
            tags=("研究候选", "自动因子", "A股"),
            analysis_outputs=(
                AnalysisOutputSpec("eligible_count", "有效候选数", "integer"),
                AnalysisOutputSpec("selected_count", "入选数量", "integer"),
                AnalysisOutputSpec("gross_exposure", "目标总仓位", "number"),
            ),
            research_document="docs/factor-research-workflow.md",
            extensions={
                "research_gate_required": True,
                "research_engine_required": True,
                "research_status": "factor_validation_passed_pending_portfolio_backtest",
                "factor_expression_hash": factor_hash,
                "factor_strategy_bundle_hash": bundle_hash,
                "factor_expression": expression.as_dict(),
                "scope": "FROZEN_EXPRESSION_RANK_PORTFOLIO",
            },
        )

        def initialize(self) -> None:
            self.top_n = int(self.params["top_n"])
            self.gross_exposure = float(self.params["gross_exposure"])

        def generate_signals(self, dt: date) -> StrategyOutput:
            universe = list(self.ctx.universe)
            scores = self.get_factor(factor_name, dt).reindex(universe)
            scores = (scores * expression.direction).dropna().sort_values(
                ascending=False, kind="stable"
            )
            selected = scores.head(self.top_n)
            if len(selected) < 9:
                selected = selected.iloc[0:0]
            weight = self.gross_exposure / len(selected) if len(selected) else 0.0
            targets = {str(code): weight for code in selected.index}
            return StrategyOutput(
                target_weights=targets,
                diagnostics={
                    "eligible_count": len(scores),
                    "selected_count": len(selected),
                    "gross_exposure": sum(targets.values()),
                },
            )

    FrozenExpressionRankStrategy.__name__ = (
        f"FrozenExpressionRankStrategy_{factor_hash[:12]}_{rebalance_frequency}"
    )
    return FrozenExpressionRankStrategy
