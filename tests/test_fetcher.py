import pytest
import pandas as pd
import time
import threading
from datetime import date
from quant_engine.data.fetcher.base import DataSource
from quant_engine.data.fetcher.akshare_adapter import AKShareAdapter
from quant_engine.data.fetcher.akshare_adapter import HistoricalConstituentsUnavailableError
from quant_engine.data.fetcher.pipeline import DataPipeline
from quant_engine.data.store import MetaDB, PriceStore


class TestDataSourceInterface:
    def test_abstract_class_cannot_instantiate(self):
        with pytest.raises(TypeError):
            DataSource()

    def test_akshare_adapter_is_datasource(self):
        adapter = AKShareAdapter()
        assert isinstance(adapter, DataSource)

    def test_akshare_source_name(self):
        adapter = AKShareAdapter()
        assert "akshare" in adapter.source_name.lower()


def test_stock_list_is_validated_and_labels_bse(monkeypatch):
    monkeypatch.setattr(
        "quant_engine.data.fetcher.akshare_adapter.ak.stock_info_a_code_name",
        lambda: pd.DataFrame({
            "code": ["000001", "920002", "600000"],
            "name": ["平安银行", "示例北交所", "浦发银行"],
        }),
    )

    adapter = AKShareAdapter(timeout_seconds=0.2)
    frame = adapter.fetch_stock_list()

    assert frame["full_code"].tolist() == ["000001.SZ", "920002.BJ", "600000.SH"]
    assert adapter.health()["status"] == "ok"
    assert adapter.last_attempts == []


def test_daily_adds_auditable_source_and_received_at(monkeypatch):
    monkeypatch.setattr(
        "quant_engine.data.fetcher.akshare_adapter.ak.stock_zh_a_hist",
        lambda **_: pd.DataFrame({
            "日期": ["2026-09-04"], "开盘": [10], "收盘": [11],
            "最高": [12], "最低": [9], "成交量": [100],
        }),
    )

    adapter = AKShareAdapter(timeout_seconds=0.2)
    frame = adapter.fetch_daily(["000001.SZ"], date(2026, 9, 1), date(2026, 9, 5))

    assert frame["code"].tolist() == ["000001.SZ"]
    assert frame["source"].tolist() == ["akshare:daily"]
    assert frame["received_at"].notna().all()
    assert adapter.health()["status"] == "ok"
    assert adapter.last_attempts[0]["status"] == "ok"


def test_daily_timeout_returns_empty_without_blocking(monkeypatch):
    def blocked(**_):
        time.sleep(0.2)
        return pd.DataFrame()

    monkeypatch.setattr(
        "quant_engine.data.fetcher.akshare_adapter.ak.stock_zh_a_hist", blocked
    )
    adapter = AKShareAdapter(timeout_seconds=0.01)

    started = time.monotonic()
    frame = adapter.fetch_daily(["000001.SZ"], date(2026, 9, 1), date(2026, 9, 5))
    elapsed = time.monotonic() - started

    assert frame.empty
    assert elapsed < 0.15
    assert adapter.health()["status"] == "unavailable"
    assert "TimeoutError" in adapter.last_attempts[0]["error"]


def test_daily_rejects_malformed_provider_rows(monkeypatch):
    monkeypatch.setattr(
        "quant_engine.data.fetcher.akshare_adapter.ak.stock_zh_a_hist",
        lambda **_: pd.DataFrame({"日期": ["2026-09-04"], "收盘": [11]}),
    )
    adapter = AKShareAdapter(timeout_seconds=0.2)

    frame = adapter.fetch_daily(["000001.SZ"], date(2026, 9, 1), date(2026, 9, 5))

    assert frame.empty
    assert adapter.last_attempts[0]["status"] == "error"
    assert "required OHLCV" in adapter.last_attempts[0]["error"]


def test_daily_duplicate_dates_degrade_batch_status(monkeypatch):
    monkeypatch.setattr(
        "quant_engine.data.fetcher.akshare_adapter.ak.stock_zh_a_hist",
        lambda **_: pd.DataFrame({
            "日期": ["2026-09-04", "2026-09-04"],
            "开盘": [10, 10], "收盘": [11, 11], "最高": [12, 12],
            "最低": [9, 9], "成交量": [100, 100],
        }),
    )
    adapter = AKShareAdapter(timeout_seconds=0.2)

    frame = adapter.fetch_daily(["000001.SZ"], date(2026, 9, 1), date(2026, 9, 5))

    assert frame.attrs["source_meta"]["duplicate_count"] == 1
    assert frame.attrs["source_meta"]["status"] == "partial"
    assert adapter.health()["status"] == "degraded"


def test_daily_rejects_nonfinite_prices(monkeypatch):
    monkeypatch.setattr(
        "quant_engine.data.fetcher.akshare_adapter.ak.stock_zh_a_hist",
        lambda **_: pd.DataFrame({
            "日期": ["2026-09-04"], "开盘": [10], "收盘": [float("inf")],
            "最高": [12], "最低": [9], "成交量": [100],
        }),
    )
    adapter = AKShareAdapter(timeout_seconds=0.2)

    frame = adapter.fetch_daily(["000001.SZ"], date(2026, 9, 1), date(2026, 9, 5))

    assert frame.empty
    assert frame.attrs["source_meta"]["status"] == "failed"
    assert frame.attrs["source_meta"]["invalid_row_count"] == 1


def test_daily_accepts_single_code_and_rejects_conflicting_suffix(monkeypatch):
    calls = []

    def fetch(**kwargs):
        calls.append(kwargs["symbol"])
        return pd.DataFrame({
            "日期": ["2026-09-04"], "开盘": [10], "收盘": [11],
            "最高": [12], "最低": [9], "成交量": [100],
        })

    monkeypatch.setattr(
        "quant_engine.data.fetcher.akshare_adapter.ak.stock_zh_a_hist", fetch
    )
    adapter = AKShareAdapter(timeout_seconds=0.2)
    frame = adapter.fetch_daily("000001.SZ", date(2026, 9, 1), date(2026, 9, 5))

    assert not frame.empty
    assert calls == ["000001"]
    with pytest.raises(ValueError, match="conflicts"):
        adapter.fetch_daily("600519.BJ", date(2026, 9, 1), date(2026, 9, 5))
    assert calls == ["000001"]


def test_index_components_fails_closed_for_historical_date():
    adapter = AKShareAdapter(timeout_seconds=0.2)

    with pytest.raises(HistoricalConstituentsUnavailableError, match="historical"):
        adapter.fetch_index_components("000300", date(2020, 1, 2))
    assert adapter.health()["status"] == "unavailable"


def test_daily_timeout_is_single_flight(monkeypatch):
    release = threading.Event()
    calls = 0

    def blocked(**_):
        nonlocal calls
        calls += 1
        release.wait(1)
        return pd.DataFrame()

    monkeypatch.setattr(
        "quant_engine.data.fetcher.akshare_adapter.ak.stock_zh_a_hist", blocked
    )
    adapter = AKShareAdapter(timeout_seconds=0.01)
    try:
        adapter.fetch_daily(["000001.SZ", "000002.SZ"], date(2026, 9, 1), date(2026, 9, 5))
    finally:
        release.set()

    assert calls == 1
    meta = adapter.last_attempts
    assert any("already in flight" in item.get("error", "") for item in meta)


def test_pipeline_records_partial_batch_without_calling_it_success(tmp_path):
    class PartialSource(DataSource):
        @property
        def source_name(self):
            return "test:daily"

        def fetch_stock_list(self):
            return pd.DataFrame()

        def fetch_index_components(self, index_code, dt):
            return pd.DataFrame()

        def fetch_daily(self, codes, start, end):
            frame = pd.DataFrame({
                "code": ["000001.SZ"], "date": pd.to_datetime(["2026-09-04"]),
                "open": [10.0], "high": [11.0], "low": [9.0],
                "close": [10.5], "volume": [100.0],
            })
            frame.attrs["source_meta"] = {
                "source": "test:daily", "status": "partial",
                "requested_codes": ["000001.SZ", "000002.SZ"],
                "returned_codes": ["000001.SZ"], "failed_codes": ["000002.SZ"],
            }
            return frame

    db = MetaDB(db_path=str(tmp_path / "meta.db"))
    pipeline = DataPipeline(PartialSource(), PriceStore(base_dir=str(tmp_path / "prices")), db)
    pipeline.sync_daily(["000001.SZ", "000002.SZ"], date(2026, 9, 1), date(2026, 9, 5))

    log = db.get_last_update("test:daily")
    assert log["status"] == "partial"


def test_pipeline_refuses_nonempty_failed_batch(tmp_path):
    class FailedSource(DataSource):
        @property
        def source_name(self):
            return "test:failed"

        def fetch_stock_list(self):
            return pd.DataFrame()

        def fetch_index_components(self, index_code, dt):
            return pd.DataFrame()

        def fetch_daily(self, codes, start, end):
            frame = pd.DataFrame({
                "code": ["000001.SZ"], "date": pd.to_datetime(["2026-09-04"]),
                "open": [10.0], "high": [11.0], "low": [9.0],
                "close": [10.5], "volume": [100.0],
            })
            frame.attrs["source_meta"] = {"source": "test:failed", "status": "failed"}
            return frame

    db = MetaDB(db_path=str(tmp_path / "meta.db"))
    store = PriceStore(base_dir=str(tmp_path / "prices"))
    pipeline = DataPipeline(FailedSource(), store, db)
    pipeline.sync_daily(["000001.SZ"], date(2026, 9, 1), date(2026, 9, 5))

    assert db.get_last_update("test:failed")["status"] == "failed"
    assert not (tmp_path / "prices" / "000001.SZ.parquet").exists()


def test_pipeline_logs_upstream_exception(tmp_path):
    class BrokenSource(DataSource):
        @property
        def source_name(self):
            return "test:broken"

        def fetch_stock_list(self):
            return pd.DataFrame()

        def fetch_index_components(self, index_code, dt):
            return pd.DataFrame()

        def fetch_daily(self, codes, start, end):
            raise TimeoutError("upstream timeout")

    db = MetaDB(db_path=str(tmp_path / "meta.db"))
    pipeline = DataPipeline(BrokenSource(), PriceStore(base_dir=str(tmp_path / "prices")), db)
    with pytest.raises(TimeoutError):
        pipeline.sync_daily(["000001.SZ"], date(2026, 9, 1), date(2026, 9, 5))

    assert db.get_last_update("test:broken")["status"] == "failed"


def test_pipeline_rejects_missing_ohlcv_columns(tmp_path):
    class IncompleteSource(DataSource):
        @property
        def source_name(self):
            return "test:incomplete"

        def fetch_stock_list(self):
            return pd.DataFrame()

        def fetch_index_components(self, index_code, dt):
            return pd.DataFrame()

        def fetch_daily(self, codes, start, end):
            return pd.DataFrame({
                "code": ["000001.SZ"], "date": pd.to_datetime(["2026-09-04"]),
            })

    db = MetaDB(db_path=str(tmp_path / "meta.db"))
    pipeline = DataPipeline(IncompleteSource(), PriceStore(base_dir=str(tmp_path / "prices")), db)
    with pytest.raises(ValueError, match="required columns"):
        pipeline.sync_daily(["000001.SZ"], date(2026, 9, 1), date(2026, 9, 5))

    assert db.get_last_update("test:incomplete")["status"] == "failed"
