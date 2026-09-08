"""撮合引擎

负责将订单撮合成交，包含 A 股涨跌停/停牌约束检查和滑点计算。
"""
import uuid
from typing import Optional

from quant_engine.backtest.types import Order, OrderSide, Trade
from quant_engine.backtest.cost_model import CostModel


class Matcher:
    """A股订单撮合引擎"""

    def __init__(self, cost_model: CostModel):
        self._cost_model = cost_model

    @staticmethod
    def est_vwap(
        open_price: float, high: float, low: float, close: float
    ) -> float:
        """估算 VWAP (成交量加权平均价)

        使用日 OHLC 的近似公式: (O + H + L + 2*C) / 5
        """
        return (open_price + high + low + 2 * close) / 5.0

    def match(self, order: Order, data_handler) -> Optional[Trade]:
        """尝试撮合订单

        Args:
            order: 待撮合订单
            data_handler: 数据供给器 (提供当前行情和状态)

        Returns:
            Trade 对象 (成交) 或 None (无法成交)
        """
        code = order.code
        dh = data_handler

        # 1. 停牌检查
        if dh.is_suspended(code):
            order.reject("instrument suspended or market data missing")
            return None

        # 2. 获取执行参考价和涨跌停。订单在当前交易日开盘后执行，
        # 因此不能使用收盘后才知道的日内 VWAP（会引入前视偏差）。
        execution_price = self._get_execution_price(code, dh)
        down_limit, up_limit = dh.get_limit_prices(code)
        if not isinstance(execution_price, (int, float)) or not (execution_price > 0):
            order.reject("invalid market price")
            return None

        # 3. 涨停 — 买入无法成交
        if order.side == OrderSide.BUY and execution_price >= up_limit:
            order.reject("buy blocked at upper price limit")
            return None

        # 4. 跌停 — 卖出无法成交
        if order.side == OrderSide.SELL and execution_price <= down_limit:
            order.reject("sell blocked at lower price limit")
            return None

        # 5. 计算含滑点的成交价
        fill_price = self._cost_model.est_fill_price(execution_price, order.side)

        # 6. 确定成交股数 (当前简单: 全额成交剩余未成交部分)
        fill_shares = order.remaining
        order.fill(order.fill_shares + fill_shares)

        # 7. 生成成交记录 (只记录本次撮合的股数)
        trade = self._make_trade(order, fill_price, fill_shares)
        return trade

    def _make_trade(
        self, order: Order, price: float, shares: int
    ) -> Trade:
        """构造本次撮合的 Trade 记录"""
        amount = shares * price
        commission = self._compute_commission(amount, order.side)
        stamp_duty = self._compute_stamp_duty(amount, order.side)
        # 滑点已反映在 fill_price 中，不再单独计算金额字段
        # 以免在 portfolio.apply_trade() 中重复扣除
        slippage = 0.0
        return Trade(
            trade_id=f"t_{uuid.uuid4().hex[:12]}",
            order_id=order.order_id,
            code=order.code,
            date=order.date,
            side=order.side,
            shares=shares,
            price=price,
            amount=amount,
            commission=commission,
            stamp_duty=stamp_duty,
            slippage=slippage,
        )

    def _get_execution_price(self, code: str, dh) -> float:
        """获取下一交易日开盘执行价；缺少开盘价则拒绝撮合。"""
        try:
            opening = float(dh.get_price(code, 'open'))
            if opening > 0:
                return opening
        except (KeyError, TypeError, ValueError):
            return float('nan')
        return float('nan')

    # Backward-compatible helper for research code that uses the OHLC VWAP
    # estimate explicitly. It is not used for event-driven order execution.
    def _get_vwap(self, code: str, dh) -> float:
        try:
            o = float(dh.get_price(code, 'open'))
            h = float(dh.get_price(code, 'high'))
            l = float(dh.get_price(code, 'low'))
            c = float(dh.get_price(code, 'close'))
            return self.est_vwap(o, h, l, c)
        except (KeyError, TypeError, ValueError):
            return float(dh.get_price(code, 'close'))

    def _compute_commission(
        self, amount: float, side: OrderSide
    ) -> float:
        return max(amount * self._cost_model.commission_rate,
                   self._cost_model.min_commission)

    def _compute_stamp_duty(
        self, amount: float, side: OrderSide
    ) -> float:
        if side == OrderSide.SELL:
            return amount * self._cost_model.stamp_duty_rate
        return 0.0
