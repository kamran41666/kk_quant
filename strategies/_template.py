"""Copyable Strategy Protocol v2 template.

Rename both this file and ``_StrategyTemplate``.  Leading-underscore strategy
classes are intentionally excluded from automatic discovery.
"""
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


class _StrategyTemplate(Strategy):
    SPEC = StrategySpec(
        id="replace-with-stable-id",
        name="替换为策略名称",
        version="0.1.0",
        description="说明信号、持仓逻辑和适用边界。",
        markets=("a-share",),
        parameters=(
            ParameterSpec(
                "lookback",
                "回看交易日",
                ParameterType.INTEGER,
                20,
                minimum=1,
                maximum=250,
            ),
        ),
        data=(DataRequirement(
            "a_share_daily", ("close",), 1, lookback_parameter="lookback",
        ),),
        rebalance_frequency="weekly",
        warmup_bars=20,
        analysis_outputs=(AnalysisOutputSpec("selected_count", "入选数量", "integer"),),
    )

    def initialize(self) -> None:
        self.lookback = self.params["lookback"]

    def generate_signals(self, dt: date) -> StrategyOutput:
        history = self.ctx.history(
            lookback=self.lookback,
            fields=["close"],
        )
        # Replace this example selection with deterministic signal logic.
        selected = (
            list(dict.fromkeys(history.index.get_level_values("code")))[:1]
            if not history.empty
            else []
        )
        target_weights = {code: 1.0 / len(selected) for code in selected}
        return StrategyOutput(
            target_weights=target_weights,
            diagnostics={"selected_count": len(selected)},
        )
