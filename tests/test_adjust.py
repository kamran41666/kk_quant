import pytest
from datetime import date
import pandas as pd
from quant_engine.data.calendar import TradingCalendar
from quant_engine.data.adjust import AdjustHandler


class TestAdjustHandler:
    @pytest.fixture
    def handler(self):
        cal = TradingCalendar()
        return AdjustHandler(cal)

    def test_no_adjust_returns_raw_price(self, handler, monkeypatch):
        """无复权事件时返回原始价格"""
        raw_prices = {
            date(2024, 6, 10): 10.0,
            date(2024, 6, 11): 10.5,
            date(2024, 6, 12): 10.2,
        }

        def mock_read_raw(code, d, field):
            return raw_prices[d]

        def mock_read_factor(code):
            return pd.DataFrame({"date": [], "factor": []}).astype({"factor": "float64"})

        monkeypatch.setattr(handler, "_read_raw_price", mock_read_raw)
        monkeypatch.setattr(handler, "_read_adjust_table", mock_read_factor)

        price = handler.get_adjusted_price("000001.SZ", date(2024, 6, 11))
        assert price == 10.5

    def test_split_adjustment(self, handler, monkeypatch):
        """10送10 = factor 2.0"""
        raw_prices = {
            date(2024, 6, 10): 20.0,
            date(2024, 6, 11): 10.0,
        }

        def mock_read_raw(code, d, field):
            return raw_prices[d]

        def mock_read_factor(code):
            return pd.DataFrame({
                "date": [date(2024, 6, 11)],
                "factor": [2.0],
            }).astype({"factor": "float64"})

        monkeypatch.setattr(handler, "_read_raw_price", mock_read_raw)
        monkeypatch.setattr(handler, "_read_adjust_table", mock_read_factor)

        # 除权前: 不应看到除权因子
        price_before = handler.get_adjusted_price("000001.SZ", date(2024, 6, 10))
        assert price_before == 20.0, "Before ex-date: should see original price"

        # 除权后: 应应用 factor
        price_after = handler.get_adjusted_price("000001.SZ", date(2024, 6, 11))
        assert price_after == pytest.approx(20.0, rel=0.01), \
            "After ex-date: 10 * 2.0 = 20.0"

    def test_dividend_adjustment(self, handler, monkeypatch):
        """分红调整: 每股派1元"""
        raw_prices = {
            date(2024, 6, 10): 10.0,
            date(2024, 6, 11): 9.2,
        }

        def mock_read_raw(code, d, field):
            return raw_prices[d]

        def mock_read_factor(code):
            return pd.DataFrame({
                "date": [date(2024, 6, 11)],
                "factor": [1.1],  # 派1元/10元 ≈ 1.1
            }).astype({"factor": "float64"})

        monkeypatch.setattr(handler, "_read_raw_price", mock_read_raw)
        monkeypatch.setattr(handler, "_read_adjust_table", mock_read_factor)

        price = handler.get_adjusted_price("000001.SZ", date(2024, 6, 11))
        assert price == pytest.approx(9.2 * 1.1, rel=0.01)

    def test_multiple_events(self, handler, monkeypatch):
        """多次除权的累积"""
        raw_prices = {
            date(2024, 1, 5): 10.0,
            date(2024, 6, 3): 5.0,
            date(2024, 12, 2): 4.5,
            date(2024, 12, 3): 4.5,
        }

        def mock_read_raw(code, d, field):
            return raw_prices[d]

        def mock_read_factor(code):
            return pd.DataFrame({
                "date": [date(2024, 6, 3), date(2024, 12, 2)],
                "factor": [2.0, 1.1],
            }).astype({"factor": "float64"})

        monkeypatch.setattr(handler, "_read_raw_price", mock_read_raw)
        monkeypatch.setattr(handler, "_read_adjust_table", mock_read_factor)

        # 年底: factor = 2.0 * 1.1 = 2.2
        price = handler.get_adjusted_price("000001.SZ", date(2024, 12, 3))
        assert price == pytest.approx(4.5 * 2.2, rel=0.01)

    def test_partial_events(self, handler, monkeypatch):
        """中间日期只应用已发生的事件"""
        raw_prices = {
            date(2024, 1, 5): 10.0,
            date(2024, 6, 3): 5.0,
            date(2024, 8, 1): 5.0,
            date(2024, 12, 2): 4.5,
        }

        def mock_read_raw(code, d, field):
            return raw_prices[d]

        def mock_read_factor(code):
            return pd.DataFrame({
                "date": [date(2024, 6, 3), date(2024, 12, 2)],
                "factor": [2.0, 1.1],
            }).astype({"factor": "float64"})

        monkeypatch.setattr(handler, "_read_raw_price", mock_read_raw)
        monkeypatch.setattr(handler, "_read_adjust_table", mock_read_factor)

        # 年中: 应该只看到第一次除权 factor=2.0
        price_mid = handler.get_adjusted_price("000001.SZ", date(2024, 8, 1))
        assert price_mid == pytest.approx(5.0 * 2.0, rel=0.01)

    def test_is_ex_date(self, handler, monkeypatch):
        """验证 is_ex_date 方法"""
        def mock_read_factor(code):
            return pd.DataFrame({
                "date": [date(2024, 6, 3)],
                "factor": [2.0],
            }).astype({"factor": "float64"})

        monkeypatch.setattr(handler, "_read_adjust_table", mock_read_factor)
        assert handler.is_ex_date("000001.SZ", date(2024, 6, 3))
        assert not handler.is_ex_date("000001.SZ", date(2024, 6, 4))
