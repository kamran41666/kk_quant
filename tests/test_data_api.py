import pytest
from datetime import date
from quant_engine.data.api import DataAPI


class TestDataAPI:
    @pytest.fixture
    def api(self):
        return DataAPI()

    def test_singleton(self):
        api1 = DataAPI()
        api2 = DataAPI()
        assert api1 is api2

    def test_get_trading_dates(self, api):
        cal = api.get_trading_dates(date(2024, 1, 1), date(2024, 1, 31))
        assert len(cal) > 0
        assert 15 <= len(cal) <= 23  # 1月约20个交易日

    def test_stock_list(self, api):
        stocks = api.stock_list()
        assert isinstance(stocks, list)
