"""交易成本模型

A股实际费率:
  - 佣金: 万2.5 (双边), 最低5元
  - 印花税: 千1 (仅卖出)
  - 滑点: 模拟市场冲击成本, 默认千1
"""
import warnings
from quant_engine.backtest.types import Trade, OrderSide


class CostModel:
    """A股交易成本计算器"""

    def __init__(
        self,
        commission_rate: float = 0.00025,
        stamp_duty_rate: float = 0.001,
        min_commission: float = 5.0,
        slippage_rate: float = 0.001,
    ):
        self.commission_rate = commission_rate
        self.stamp_duty_rate = stamp_duty_rate
        self.min_commission = min_commission
        self.slippage_rate = slippage_rate

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
