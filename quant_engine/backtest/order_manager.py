"""订单管理器

负责订单的创建、取消、查询，以及 A 股交易单位约束。
"""
from datetime import date
from typing import Optional

from quant_engine.backtest.types import Order, OrderSide, OrderStatus
from quant_engine.backtest.cost_model import CostModel


class OrderManager:
    """订单管理器

    管理订单生命周期: 创建 → 挂单 → 撮合 → 完成/取消/拒绝。
    """

    # A股最小交易单位
    LOT_SIZE = 100

    def __init__(self, cost_model: CostModel):
        self._cost_model = cost_model
        self._orders: list[Order] = []
        self._order_counter = 0

    @property
    def cost_model(self) -> CostModel:
        return self._cost_model

    @property
    def all_orders(self) -> list[Order]:
        return list(self._orders)

    def submit(
        self,
        code: str,
        side: OrderSide,
        shares: int,
        dt: date,
        price_limit: Optional[float] = None,
    ) -> Order:
        """提交委托

        Args:
            code: 股票代码
            side: 买卖方向
            shares: 委托股数 (买入会自动取整)
            dt: 委托日期
            price_limit: 限价 (None=市价)

        Returns:
            创建的 Order 对象
        """
        # 买入时取整
        if side == OrderSide.BUY:
            shares = self.round_lot(shares, round_up=False)

        self._order_counter += 1
        order = Order(
            order_id=f"ord_{self._order_counter:06d}",
            code=code,
            date=dt,
            side=side,
            shares=shares,
            price_limit=price_limit,
        )
        self._orders.append(order)
        return order

    def cancel(self, order_id: str) -> bool:
        """取消订单"""
        for o in self._orders:
            if o.order_id == order_id:
                o.cancel()
                return True
        return False

    def get_pending_orders(self) -> list[Order]:
        """获取所有待处理订单"""
        return [
            o for o in self._orders
            if o.status in (OrderStatus.PENDING, OrderStatus.PARTIAL)
        ]

    def get_orders(self, code: str) -> list[Order]:
        """获取某只股票的所有订单"""
        return [o for o in self._orders if o.code == code]

    @staticmethod
    def round_lot(shares: int, round_up: bool = False) -> int:
        """将股数取整为手（100股）的倍数

        Args:
            shares: 原始股数
            round_up: True=向上取整, False=向下取整

        Returns:
            取整后的股数
        """
        if round_up:
            return ((shares + OrderManager.LOT_SIZE - 1)
                    // OrderManager.LOT_SIZE * OrderManager.LOT_SIZE)
        return (shares // OrderManager.LOT_SIZE) * OrderManager.LOT_SIZE
