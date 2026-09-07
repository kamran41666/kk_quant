import pytest
from datetime import date
import pandas as pd
import pytest
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
        assert 238 <= count <= 270, f"Expected ~242-262, got {count}"

    def test_cache_consistency(self):
        cal = TradingCalendar()
        r1 = cal.is_trading_day(date(2024, 6, 15))
        r2 = cal.is_trading_day(date(2024, 6, 15))
        assert r1 == r2

    def test_coverage_report_contains_provenance_and_hash(self):
        cal = TradingCalendar()
        report = cal.coverage_report(date(2024, 1, 2), date(2024, 1, 10))
        assert report["calendar_version"] == "trading-calendar-v1"
        assert report["content_hash"] and len(report["content_hash"]) == 64
        assert report["coverage_start"] <= report["requested_start"]
        assert report["coverage_end"] >= report["requested_end"]
        assert isinstance(report["trading_days"], list)

    def test_unverified_calendar_is_not_complete(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUANT_DATA_DIR", str(tmp_path))
        path = tmp_path / "raw" / "calendar" / "trading_dates.parquet"
        path.parent.mkdir(parents=True)
        pd.DataFrame({
            "date": ["2024-01-02", "2024-01-03"],
            "is_trading_day": [True, True],
            "exchange": ["SSE", "SSE"],
        }).to_parquet(path, index=False)
        cal = TradingCalendar(start_year=2024, end_year=2024)
        report = cal.coverage_report(date(2024, 1, 2), date(2024, 1, 3))
        assert report["source"] == "unknown:local-cache"
        assert report["complete"] is False

    def test_mixed_calendar_sources_are_not_verified(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUANT_DATA_DIR", str(tmp_path))
        path = tmp_path / "raw" / "calendar" / "trading_dates.parquet"
        path.parent.mkdir(parents=True)
        pd.DataFrame({
            "date": ["2024-01-02", "2024-01-03"],
            "is_trading_day": [True, True],
            "exchange": ["SSE", "SSE"],
            "source": ["akshare:test", "fallback:business-days"],
        }).to_parquet(path, index=False)
        cal = TradingCalendar(start_year=2024, end_year=2024)
        report = cal.coverage_report(date(2024, 1, 2), date(2024, 1, 3))
        assert report["source"].startswith("mixed:")
        assert report["verified"] is False
        assert report["complete"] is False

    def test_ensure_coverage_refreshes_only_verified_source(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUANT_DATA_DIR", str(tmp_path))
        path = tmp_path / "raw" / "calendar" / "trading_dates.parquet"
        path.parent.mkdir(parents=True)
        pd.DataFrame({
            "date": ["2024-01-02"],
            "is_trading_day": [True],
            "exchange": ["SSE"],
        }).to_parquet(path, index=False)
        cal = TradingCalendar(start_year=2024, end_year=2024)
        monkeypatch.setattr(cal, "_fetch_from_akshare", lambda: pd.DataFrame({
            "date": ["2024-01-02", "2024-01-03"],
            "is_trading_day": [True, True],
            "exchange": ["SSE", "SSE"],
            "source": ["akshare:test", "akshare:test"],
        }))
        report = cal.ensure_coverage(date(2024, 1, 2), date(2024, 1, 3))
        assert report["complete"] is True
        assert report["source"] == "akshare:test"

    def test_ensure_coverage_keeps_fallback_fail_closed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUANT_DATA_DIR", str(tmp_path))
        path = tmp_path / "raw" / "calendar" / "trading_dates.parquet"
        path.parent.mkdir(parents=True)
        pd.DataFrame({
            "date": ["2024-01-02"],
            "is_trading_day": [True],
            "exchange": ["SSE"],
        }).to_parquet(path, index=False)
        cal = TradingCalendar(start_year=2024, end_year=2024)
        monkeypatch.setattr(cal, "_fetch_from_akshare", lambda: pd.DataFrame({
            "date": ["2024-01-02", "2024-01-03"],
            "is_trading_day": [True, True],
            "exchange": ["SSE", "SSE"],
            "source": ["fallback:business-days", "fallback:business-days"],
        }))
        report = cal.ensure_coverage(date(2024, 1, 2), date(2024, 1, 3))
        assert report["complete"] is False

    def test_fallback_fetch_is_not_persisted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUANT_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(TradingCalendar, "_fetch_from_akshare", lambda _self: pd.DataFrame({
            "date": ["2024-01-02", "2024-01-03"],
            "is_trading_day": [True, True],
            "exchange": ["SSE", "SSE"],
            "source": ["fallback:business-days", "fallback:business-days"],
        }))
        TradingCalendar(start_year=2024, end_year=2024)
        assert not (tmp_path / "raw" / "calendar" / "trading_dates.parquet").exists()
