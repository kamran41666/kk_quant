from datetime import date

import pandas as pd
import pytest

from quant_engine.data.api import DataAPI
from quant_engine.data.store import PriceStore


class VerifiedCalendar:
    def coverage_report(self, start, end):
        return {"complete": True, "verified": True, "source": "test:calendar"}

    def get_trading_days(self, start, end):
        return [start]


@pytest.mark.parametrize("changed", [
    {"open": 0}, {"close": -1}, {"volume": -1},
    {"high": 8}, {"low": 12}, {"close": 12}, {"open": 8},
])
def test_coverage_rejects_impossible_ohlcv(tmp_path, monkeypatch, changed):
    store = PriceStore(base_dir=str(tmp_path))
    row = {"date": "2024-01-02", "open": 10, "high": 11, "low": 9,
           "close": 10, "volume": 100, **changed}
    store.write("000001.SZ", pd.DataFrame([row]))
    api = DataAPI()
    monkeypatch.setattr(api, "_price_store", store)
    report = api.daily_coverage(["000001.SZ"], date(2024, 1, 2), date(2024, 1, 2),
                                calendar=VerifiedCalendar())
    assert report["complete"] is False
    assert report["items"][0]["invalid_field_rows"] == 1
    assert report["items"][0]["status"] == "partial"
    assert report["coverage_hash"] == DataAPI.daily_coverage_hash(report)


def test_zero_volume_flat_suspension_bar_is_valid(tmp_path, monkeypatch):
    store = PriceStore(base_dir=str(tmp_path))
    store.write("000001.SZ", pd.DataFrame([{
        "date": "2024-01-02", "open": 10, "high": 10, "low": 10,
        "close": 10, "volume": 0,
    }]))
    api = DataAPI()
    monkeypatch.setattr(api, "_price_store", store)
    report = api.daily_coverage(["000001.SZ"], date(2024, 1, 2), date(2024, 1, 2),
                                calendar=VerifiedCalendar())
    assert report["complete"] is True


def test_price_limits_use_the_same_adjustment_as_ohlc(monkeypatch):
    api = DataAPI()
    index = pd.MultiIndex.from_tuples([("000001.SZ", date(2024, 1, 2))], names=["code", "date"])
    frame = pd.DataFrame({"close": [10.0], "up_limit": [11.0], "down_limit": [9.0]}, index=index)
    monkeypatch.setattr(api._adjust, "factors_for_dates", lambda code, dates: pd.Series([0.5]))
    adjusted = api._apply_event_driven_adjust(frame, list(frame.columns))
    assert adjusted.iloc[0].to_dict() == {"close": 5.0, "up_limit": 5.5, "down_limit": 4.5}


@pytest.mark.parametrize("field,value", [("up_limit", 11.0), ("down_limit", 9.0), ("is_suspended", True)])
def test_execution_constraints_change_dataset_identity(tmp_path, monkeypatch, field, value):
    store = PriceStore(base_dir=str(tmp_path))
    row = {"date": "2024-01-02", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100}
    store.write("000001.SZ", pd.DataFrame([row]))
    api = DataAPI()
    monkeypatch.setattr(api, "_price_store", store)
    before = api.daily_coverage(["000001.SZ"], date(2024, 1, 2), date(2024, 1, 2), calendar=VerifiedCalendar())
    store.write("000001.SZ", pd.DataFrame([{**row, field: value}]))
    after = api.daily_coverage(["000001.SZ"], date(2024, 1, 2), date(2024, 1, 2), calendar=VerifiedCalendar())
    assert before["complete"] and after["complete"]
    assert before["dataset_hash"] != after["dataset_hash"]
    assert before["items"][0]["execution_field_counts"][field] == 0
    assert after["items"][0]["execution_field_counts"][field] == 1


def test_suspension_hash_preserves_consumed_type(tmp_path, monkeypatch):
    store = PriceStore(base_dir=str(tmp_path))
    api = DataAPI()
    monkeypatch.setattr(api, "_price_store", store)
    hashes = []
    for value in (0, "0"):
        store.write("000001.SZ", pd.DataFrame([{
            "date": "2024-01-02", "open": 10, "high": 11, "low": 9,
            "close": 10, "volume": 100, "is_suspended": value,
        }]))
        report = api.daily_coverage(["000001.SZ"], date(2024, 1, 2), date(2024, 1, 2), calendar=VerifiedCalendar())
        hashes.append(report["dataset_hash"])
    assert hashes[0] != hashes[1]


def test_http_metadata_does_not_invalidate_exported_coverage_hash():
    report = {"coverage_version": "daily-coverage-v1", "items": [], "complete": False}
    report["coverage_hash"] = DataAPI.daily_coverage_hash(report)
    exported = {**report, "meta": {"research_only": True, "calendar": "TradingCalendar", "adjustment": "none"}}
    assert DataAPI.daily_coverage_hash(exported) == report["coverage_hash"]
    exported["items"] = [{"code": "600519.SH", "missing_count": 1}]
    assert DataAPI.daily_coverage_hash(exported) != report["coverage_hash"]
