"""交易成本模型

A股实际费率:
  - 佣金: 万2.5 (双边), 最低5元
  - 印花税: 千1 (仅卖出)
  - 滑点: 模拟市场冲击成本, 默认千1
"""
import math
from quant_engine.backtest.types import Trade, OrderSide


# These are explicitly named research assumptions, not broker quotations.
# Keeping the registry in code makes the selected scenario reproducible and
# prevents a UI from silently inventing a fee schedule.
COST_SCENARIOS = {
    "paper_baseline_v1": {
        "label": "纸面基线（敏感性假设）",
        "commission_rate": 0.00025,
        "stamp_duty_rate": 0.001,
        "min_commission": 5.0,
        "slippage_rate": 0.001,
    },
    "paper_low_impact_v1": {
        "label": "纸面低冲击（敏感性假设）",
        "commission_rate": 0.00025,
        "stamp_duty_rate": 0.001,
        "min_commission": 5.0,
        "slippage_rate": 0.0005,
    },
    "paper_high_impact_v1": {
        "label": "纸面高冲击（敏感性假设）",
        "commission_rate": 0.00025,
        "stamp_duty_rate": 0.001,
        "min_commission": 5.0,
        "slippage_rate": 0.0025,
    },
}


def cost_scenario_catalog() -> list[dict]:
    """Return a JSON-safe copy of the registered research scenarios."""
    return [
        {"id": scenario_id, **dict(config)}
        for scenario_id, config in COST_SCENARIOS.items()
    ]


class CostModel:
    """A股交易成本计算器"""

    def __init__(
        self,
        commission_rate: float = 0.00025,
        stamp_duty_rate: float = 0.001,
        min_commission: float = 5.0,
        slippage_rate: float = 0.001,
        *,
        scenario: str = "paper_baseline_v1",
    ):
        if scenario not in COST_SCENARIOS:
            raise ValueError(f"unknown cost scenario: {scenario}")
        values = (commission_rate, stamp_duty_rate, min_commission, slippage_rate)
        if any(not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0 for value in values):
            raise ValueError("cost model rates must be finite and non-negative")
        self.scenario = scenario
        self.scenario_label = str(COST_SCENARIOS[scenario]["label"])
        self.commission_rate = float(commission_rate)
        self.stamp_duty_rate = float(stamp_duty_rate)
        self.min_commission = float(min_commission)
        self.slippage_rate = float(slippage_rate)

    @classmethod
    def from_scenario(cls, scenario: str = "paper_baseline_v1") -> "CostModel":
        """Create a model from the immutable registered scenario catalog."""
        config = COST_SCENARIOS.get(scenario)
        if config is None:
            raise ValueError(f"unknown cost scenario: {scenario}")
        return cls(
            commission_rate=config["commission_rate"],
            stamp_duty_rate=config["stamp_duty_rate"],
            min_commission=config["min_commission"],
            slippage_rate=config["slippage_rate"],
            scenario=scenario,
        )

    def as_manifest(self) -> dict:
        """Return the exact assumptions used by a run."""
        return {
            "scenario": self.scenario,
            "label": self.scenario_label,
            "commission_rate": self.commission_rate,
            "stamp_duty_rate": self.stamp_duty_rate,
            "min_commission": self.min_commission,
            "slippage_rate": self.slippage_rate,
            "research_only": True,
        }

    def calc_cost(self, trade: Trade) -> tuple[float, float, float]:
        """计算交易成本

        Returns:
            (commission, stamp_duty, slippage)
        """
        commission = max(
            trade.amount * self.commission_rate, self.min_commission
        )
        stamp_duty = (
            trade.amount * self.stamp_duty_rate
            if trade.side == OrderSide.SELL else 0.0
        )
        slippage = trade.amount * self.slippage_rate
        return commission, stamp_duty, slippage

    def est_fill_price(
        self, ref_price: float, side: OrderSide
    ) -> float:
        """估算含滑点的成交价

        Args:
            ref_price: 参考价 (如 VWAP 估算值)
            side: 买卖方向

        Returns:
            含滑点的估计成交价
        """
        if side == OrderSide.BUY:
            return ref_price * (1.0 + self.slippage_rate)
        else:
            return ref_price * (1.0 - self.slippage_rate)

    def total_cost(self, trade: Trade) -> float:
        """总交易成本 = 佣金 + 印花税 + 滑点"""
        comm, stamp, slip = self.calc_cost(trade)
        return comm + stamp + slip
