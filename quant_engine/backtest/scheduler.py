"""调仓日调度器

策略的调仓频率由 RebalanceScheduler 决定。
引擎每日迭代，但仅调仓日触发信号生成和订单执行。
"""
from datetime import date, timedelta

from quant_engine.data.calendar import TradingCalendar


class RebalanceScheduler:
    """调仓日调度器

    支持 daily / weekly / monthly 三种频率。
    """

    def __init__(
        self,
        calendar: TradingCalendar,
        frequency: str = 'weekly',
        weekday: int = 5,
    ):
        if frequency not in ('daily', 'weekly', 'monthly'):
            raise ValueError(
                f"Unknown frequency: {frequency}. "
                f"Use 'daily', 'weekly', or 'monthly'."
            )
        if weekday < 1 or weekday > 5:
            raise ValueError(f"weekday must be 1-5 (Mon-Fri), got {weekday}")

        self._calendar = calendar
        self.frequency = frequency
        self.weekday = weekday  # 1=Mon ... 5=Fri

    def is_rebalance_day(self, dt: date) -> bool:
        """判断 dt 是否为调仓日"""
        if not self._calendar.is_trading_day(dt):
            return False

        if self.frequency == 'daily':
            return True

        if self.frequency == 'weekly':
            return dt.weekday() == (self.weekday - 1)  # Mon=0 in Python

        if self.frequency == 'monthly':
            return self._is_month_last_trading_day(dt)

        return False

    def next_rebalance_day(self, dt: date) -> date:
        """返回 dt 之后（不含 dt）的下一个调仓日"""
        next_day = dt + timedelta(days=1)
        while True:
            if self.is_rebalance_day(next_day):
                return next_day
            next_day += timedelta(days=1)

    def _is_month_last_trading_day(self, dt: date) -> bool:
        """判断 dt 是否为当月最后一个交易日"""
        # 获取 dt 所在月份的所有交易日，取最后一个
        year, month = dt.year, dt.month
        # 找下个月第一个交易日，然后回退一个交易日
        next_month = month + 1
        next_year = year
        if next_month > 12:
            next_month = 1
            next_year += 1
        first_of_next = date(next_year, next_month, 1)
        prev_day = self._calendar.prev_trading_day(first_of_next)
        return dt == prev_day

    def _find_month_last_trading_day(self, year: int, month: int) -> date:
        """返回指定年月的最后一个交易日（公开辅助方法）"""
        return self._calendar.nth_trading_day_of_month(year, month, -1)
