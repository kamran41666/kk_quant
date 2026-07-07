import pytest
from datetime import date
from quant_engine.data.calendar import TradingCalendar


class TestTradingCalendar:
    def test_known_trading_days(self, sample_dates):
        cal = TradingCalendar()
        for d in sample_dates["trading"]:
            assert cal.is_trading_day(d), f"{d} should be a trading day"

    def test_weekends_are_not_trading_days(self, sample_dates):
        cal = TradingCalendar()
        for d in sample_dates["weekend"]:
            assert not cal.is_trading_day(d), f"{d} (weekend) should not trade"

    def test_get_trading_days_range(self):
        cal = TradingCalendar()
        days = cal.get_trading_days(date(2024, 1, 2), date(2024, 1, 10))
        assert len(days) > 0
        assert days[0] >= date(2024, 1, 2)
        assert days[-1] <= date(2024, 1, 10)

    def test_next_and_prev_trading_day(self):
        cal = TradingCalendar()
        wed = date(2024, 1, 3)
        next_day = cal.next_trading_day(wed)
        prev_day = cal.prev_trading_day(wed)
        assert next_day > wed
        assert prev_day < wed
        assert cal.is_trading_day(next_day)
        assert cal.is_trading_day(prev_day)

    def test_trading_days_count(self):
        cal = TradingCalendar()
        count = cal.count_trading_days(date(2024, 1, 1), date(2024, 12, 31))
        assert 238 <= count <= 248, f"Expected ~242, got {count}"

    def test_cache_consistency(self):
        cal = TradingCalendar()
        r1 = cal.is_trading_day(date(2024, 6, 15))
        r2 = cal.is_trading_day(date(2024, 6, 15))
        assert r1 == r2
