"""组合估值

管理回测中的虚拟持仓、现金和总权益。支持 T+1 锁仓规则。
"""
from datetime import date, timedelta
from quant_engine.backtest.types import Trade, OrderSide, Position


class Portfolio:
    """回测组合管理器"""

    def __init__(self, initial_capital: float = 1_000_000.0):
        self._initial_capital = initial_capital
        self._cash = initial_capital
        self._positions: dict[str, Position] = {}
        self._locked_lots: dict[str, list[tuple[date, int]]] = {}
        self._previous_total: float = initial_capital
        self._current_total: float = initial_capital
        self._daily_return: float = 0.0

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def positions(self) -> dict[str, Position]:
        return dict(self._positions)

    @property
    def market_value(self) -> float:
        return sum(p.market_value for p in self._positions.values())

    @property
    def total_value(self) -> float:
        return self._cash + self.market_value

    @property
    def previous_total_value(self) -> float:
        return self._previous_total

    @property
    def daily_return(self) -> float:
        return self._daily_return

    def apply_trade(self, trade: Trade):
        """应用一笔成交到组合"""
        cost = trade.commission + trade.stamp_duty + trade.slippage

        if trade.side == OrderSide.BUY:
            self._apply_buy(trade, cost)
        else:
            self._apply_sell(trade, cost)

    def _apply_buy(self, trade: Trade, cost: float):
        """处理买入成交"""
        total_cost = trade.amount + cost
        if total_cost > self._cash + 1e-9:
            raise ValueError(
                f"Insufficient cash: required={total_cost:.2f}, available={self._cash:.2f}"
            )
        self._cash -= total_cost

        existing = self._positions.get(trade.code)
        if trade.code not in self._locked_lots:
            # Loaded legacy positions carry only a position-level unlock date.
            self._locked_lots[trade.code] = (
                [(existing.unlock_date, existing.shares)] if existing else []
            )
        lots = [
            (unlock, shares) for unlock, shares in self._locked_lots[trade.code]
            if unlock > trade.date
        ]
        lots.append((trade.date + timedelta(days=1), trade.shares))
        self._locked_lots[trade.code] = lots
        if existing:
            # 加仓: 更新平均成本
            total_shares = existing.shares + trade.shares
            total_cost_basis = (
                existing.shares * existing.avg_cost + trade.amount + cost
            )
            new_avg_cost = total_cost_basis / total_shares if total_shares > 0 else 0
            existing.shares = total_shares
            existing.avg_cost = new_avg_cost
            existing.market_value += trade.amount
            # T+1: 新买入的股份锁定到下一个交易日
            existing.unlock_date = max(
                existing.unlock_date, trade.date + timedelta(days=1)
            )
        else:
            self._positions[trade.code] = Position(
                code=trade.code,
                shares=trade.shares,
                avg_cost=(trade.amount + cost) / trade.shares,
                market_value=trade.amount,
                unlock_date=trade.date + timedelta(days=1),
            )

    def _apply_sell(self, trade: Trade, cost: float):
        """处理卖出成交"""
        existing = self._positions.get(trade.code)
        available = existing.shares if existing else 0
        if trade.shares > available or existing is None:
            raise ValueError(
                f"Insufficient shares: required={trade.shares}, available={available}"
            )
        sellable = self.get_sellable_shares(trade.code, trade.date)
        if trade.shares > sellable:
            raise ValueError(
                f"Insufficient sellable shares: required={trade.shares}, available={sellable}"
            )
        self._cash += trade.amount - cost
        marked_price = existing.market_value / existing.shares
        existing.shares -= trade.shares
        existing.market_value = existing.shares * marked_price
        if existing.shares <= 0:
            self._positions.pop(trade.code, None)
            self._locked_lots.pop(trade.code, None)

    def update_market_values(self, prices: dict[str, float], dt: date):
        """更新持仓市值（每日估值）

        Args:
            prices: {code: close_price} — 当日收盘价
            dt: 当前交易日
        """
        # Compare the marked close with the prior session's marked close.  Do
        # not reset the denominator after intraday trades: doing so would make
        # deposits/withdrawals caused by buys and sells look like P&L.
        previous_total = self._current_total

        for code, pos in self._positions.items():
            if code in prices:
                pos.market_value = pos.shares * prices[code]

        self._previous_total = previous_total
        self._current_total = self.total_value
        if previous_total:
            self._daily_return = (self._current_total - previous_total) / previous_total
        else:
            self._daily_return = 0.0

    def get_sellable_shares(self, code: str, dt: date) -> int:
        """获取某只股票在 dt 日可卖出的股数（考虑 T+1 锁仓）

        Args:
            code: 股票代码
            dt: 当前交易日

        Returns:
            可卖股数
        """
        pos = self._positions.get(code)
        if pos is None or pos.shares <= 0:
            return 0
        lots = self._locked_lots.get(code)
        if lots is None:
            return 0 if pos.is_locked(dt) else pos.shares
        locked_shares = sum(shares for unlock, shares in lots if dt < unlock)
        return max(0, pos.shares - locked_shares)
