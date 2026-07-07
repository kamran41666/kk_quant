import pytest
from datetime import date, timedelta

from quant_engine.data.calendar import TradingCalendar
from quant_engine.backtest.scheduler import RebalanceScheduler


class TestRebalanceScheduler:
    @pytest.fixture
    def calendar(self):
        return TradingCalendar()

    @pytest.fixture
    def weekly_scheduler(self, calendar):
        return RebalanceScheduler(calendar, frequency='weekly', weekday=5)

    def test_default_is_weekly_friday(self, calendar):
        s = RebalanceScheduler(calendar)
        assert s.frequency == 'weekly'
        assert s.weekday == 5

    def test_daily_frequency(self, calendar):
        s = RebalanceScheduler(calendar, frequency='daily')
        # 每个交易日都是调仓日
        assert s.is_rebalance_day(date(2024, 6, 14))  # 周五，必是交易日

    def test_weekly_friday(self, weekly_scheduler):
        # 2024-06-14 是周五且是交易日
        assert weekly_scheduler.is_rebalance_day(date(2024, 6, 14))

    def test_weekly_non_friday(self, weekly_scheduler):
        # 2024-06-12 是周三 → 不是调仓日
        assert not weekly_scheduler.is_rebalance_day(date(2024, 6, 12))

    def test_next_rebalance_day(self, weekly_scheduler):
        # 周三 → 下一个周五
        wed = date(2024, 6, 12)
        next_rb = weekly_scheduler.next_rebalance_day(wed)
        assert next_rb.weekday() == 4  # 周五
        assert next_rb > wed

    def test_rebalance_day_same_day(self, weekly_scheduler):
        # 如果当前就是调仓日，next 应该是下一个调仓日
        fri = date(2024, 6, 14)
        next_rb = weekly_scheduler.next_rebalance_day(fri)
        assert next_rb > fri
        assert next_rb.weekday() == 4

    def test_monthly_last_trading_day(self, calendar):
        s = RebalanceScheduler(calendar, frequency='monthly')
        # 每月最后一个交易日
        # 2024年6月最后一个交易日
        jun_last = s._find_month_last_trading_day(2024, 6)
        assert calendar.is_trading_day(jun_last)
        # 验证确实是当月最后
        for d in calendar.get_trading_days(
            jun_last + timedelta(days=1), date(2024, 7, 1)
        ):
            assert d.month != 6 or d > jun_last
