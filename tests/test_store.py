import pytest
import tempfile
import os
from datetime import date
from pathlib import Path
import pandas as pd
import numpy as np

from quant_engine.data.store import PriceStore, MetaDB, StockInfo


class TestPriceStore:
    @pytest.fixture
    def store(self, tmp_path):
        return PriceStore(base_dir=str(tmp_path / "data"))

    @pytest.fixture
    def sample_df(self):
        dates = pd.date_range("2024-01-02", "2024-01-10", freq="B")
        n = len(dates)
        np.random.seed(42)
        return pd.DataFrame({
            "date": dates,
            "open": np.random.randn(n) + 10,
            "high": np.random.randn(n) + 11,
            "low": np.random.randn(n) + 9,
            "close": np.random.randn(n) + 10,
            "volume": np.random.randint(10000, 1000000, n),
            "amount": np.random.randint(100000, 10000000, n),
            "pre_close": np.random.randn(n) + 10,
            "up_limit": [12.0] * n,
            "down_limit": [8.0] * n,
            "turnover_rate": np.random.rand(n) * 5,
            "is_suspended": [False] * n,
        })

    def test_write_and_read(self, store, sample_df):
        store.write("000001.SZ", sample_df)
        price = store.read("000001.SZ", date(2024, 1, 3), "close")
        expected = sample_df.loc[
            sample_df["date"] == pd.Timestamp("2024-01-03"), "close"
        ].iloc[0]
        assert price == pytest.approx(expected)

    def test_read_range(self, store, sample_df):
        store.write("000001.SZ", sample_df)
        store.write("000002.SZ", sample_df)

        df = store.read_range(
            codes=["000001.SZ", "000002.SZ"],
            start=date(2024, 1, 2),
            end=date(2024, 1, 10),
            fields=["close", "volume"],
        )
        assert "000001.SZ" in df.index.get_level_values("code")
        assert "000002.SZ" in df.index.get_level_values("code")
        assert "close" in df.columns
        assert "volume" in df.columns

    def test_missing_code_raises(self, store):
        with pytest.raises(KeyError):
            store.read("NONEXIST.SZ", date(2024, 1, 3), "close")


class TestMetaDB:
    @pytest.fixture
    def db(self, tmp_path):
        return MetaDB(db_path=str(tmp_path / "meta.db"))

    def test_upsert_and_get_stock(self, db):
        info = StockInfo(
            code="000001.SZ", name="平安银行",
            exchange="SZSE", board="主板",
            listed_date=date(1991, 4, 3), delisted_date=None
        )
        db.upsert_stock(info)
        got = db.get_stock("000001.SZ")
        assert got.name == "平安银行"
        assert got.exchange == "SZSE"

    def test_get_all_stocks(self, db):
        db.upsert_stock(StockInfo(
            code="000001.SZ", name="平安银行",
            exchange="SZSE", board="主板",
            listed_date=date(1991, 4, 3), delisted_date=None
        ))
        db.upsert_stock(StockInfo(
            code="600000.SH", name="浦发银行",
            exchange="SSE", board="主板",
            listed_date=date(1999, 11, 10), delisted_date=None
        ))
        all_stocks = db.get_all_stocks()
        assert len(all_stocks) >= 2

    def test_data_version(self, db):
        db.set_data_version("daily", "2024-07-07")
        assert db.get_data_version("daily") == "2024-07-07"

    def test_update_log(self, db):
        db.log_update("akshare_daily", 5000, "success")
        log = db.get_last_update("akshare_daily")
        assert log["record_count"] == 5000
        assert log["status"] == "success"
