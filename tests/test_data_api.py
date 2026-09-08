import pytest
from datetime import date
import pandas as pd
from quant_engine.data.api import DataAPI
from quant_engine.data.store import MetaDB, PriceStore
from quant_engine.data.fetcher.akshare_adapter import HistoricalConstituentsUnavailableError


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

    def test_index_components_only_use_archived_snapshot(self, api, tmp_path):
        previous = api._meta
        api._meta = MetaDB(db_path=str(tmp_path / "meta.db"))
        try:
            api.archive_index_components(
                "999999.SH", date(2024, 1, 31),
                [{"code": "000001.SZ", "name": "平安银行", "weight": 1.0}],
                source="test:snapshot",
            )
            assert api.index_components("999999.SH", date(2024, 2, 1)) == ["000001.SZ"]
            with pytest.raises(HistoricalConstituentsUnavailableError):
                api.index_components("999999.SH", date(2023, 12, 31))
        finally:
            api._meta = previous

    def test_index_snapshot_rejects_future_date(self, api):
        with pytest.raises(ValueError, match="future"):
            api.index_snapshot("000300.SH", date.today().replace(year=date.today().year + 1))

    def test_daily_coverage_reports_missing_dates_and_hash(self, tmp_path):
        api = DataAPI()
        previous = api._price_store
        store = PriceStore(base_dir=str(tmp_path))
        api._price_store = store
        try:
            store.write("000001.SZ", pd.DataFrame([{
                "date": "2024-01-02", "open": 10.0, "high": 10.5,
                "low": 9.8, "close": 10.2, "volume": 1000,
            }]))
            report = api.daily_coverage(["000001.SZ"], date(2024, 1, 2), date(2024, 1, 4))
            item = report["items"][0]
            assert report["coverage_version"] == "daily-coverage-v1"
            assert report["complete"] is False
            assert report["expected_trading_days"] == 3
            assert item["status"] == "partial"
            assert item["observed_trading_days"] == 1
            assert item["missing_count"] == 2
            assert item["last_observed"] == "2024-01-02"
            assert item["content_hash"] and len(item["content_hash"]) == 64
            assert report["dataset_hash"] and len(report["dataset_hash"]) == 64
            assert report["adjust"] == "none"
            assert len(report["coverage_hash"]) == 64
            assert DataAPI.daily_dataset_hash(report) == report["dataset_hash"]
            assert DataAPI.daily_coverage_hash(report) == report["coverage_hash"]
        finally:
            api._price_store = previous

    def test_daily_coverage_content_hash_changes_when_values_change(self, tmp_path):
        api = DataAPI()
        previous = api._price_store
        store = PriceStore(base_dir=str(tmp_path))
        api._price_store = store
        try:
            frame = pd.DataFrame([
                {"date": "2024-01-02", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 1000},
                {"date": "2024-01-03", "open": 10.2, "high": 10.7, "low": 10.0, "close": 10.6, "volume": 1200},
            ])
            store.write("000001.SZ", frame)
            first = api.daily_coverage(["000001.SZ"], date(2024, 1, 2), date(2024, 1, 3))
            store.write("000001.SZ", frame.assign(close=[10.3, 10.6]))
            second = api.daily_coverage(["000001.SZ"], date(2024, 1, 2), date(2024, 1, 3))
            assert first["complete"] is True and second["complete"] is True
            assert first["dataset_hash"] != second["dataset_hash"]
            assert first["items"][0]["content_hash"] != second["items"][0]["content_hash"]
        finally:
            api._price_store = previous

    def test_daily_dataset_hash_is_independent_of_requested_code_order(self):
        first = {
            "coverage_version": "daily-coverage-v1",
            "market": "a-share",
            "start_date": "2024-01-02",
            "end_date": "2024-01-03",
            "adjust": "event_driven",
            "items": [
                {"code": "600000.SH", "content_hash": "a" * 64},
                {"code": "000001.SZ", "content_hash": "b" * 64},
            ],
        }
        second = {**first, "items": list(reversed(first["items"]))}
        assert DataAPI.daily_dataset_hash(first) == DataAPI.daily_dataset_hash(second)

    def test_daily_coverage_rejects_non_trading_day_rows(self, tmp_path):
        api = DataAPI()
        previous = api._price_store
        store = PriceStore(base_dir=str(tmp_path))
        api._price_store = store
        try:
            store.write("000001.SZ", pd.DataFrame([
                {"date": "2024-01-02", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100},
                {"date": "2024-01-03", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100},
                {"date": "2024-01-06", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100},
            ]))
            report = api.daily_coverage(
                ["000001.SZ"], date(2024, 1, 2), date(2024, 1, 6)
            )
            assert report["complete"] is False
            assert report["items"][0]["unexpected_dates"] == ["2024-01-06"]
        finally:
            api._price_store = previous
