import pytest
import tempfile
import os
import sqlite3
from datetime import date, timedelta
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

    def test_write_batch_rolls_back_when_replacement_fails(self, store, sample_df, monkeypatch):
        store.write("000001.SZ", sample_df)
        store.write("000002.SZ", sample_df)
        replacement = sample_df.copy()
        replacement["close"] = replacement["close"] + 100

        import quant_engine.data.store as store_module
        original_replace = store_module.os.replace

        def fail_second(source, target):
            if str(target).endswith("000002.SZ.parquet"):
                raise OSError("simulated replacement failure")
            return original_replace(source, target)

        monkeypatch.setattr(store_module.os, "replace", fail_second)
        with pytest.raises(OSError, match="replacement failure"):
            store.write_batch({"000001.SZ": replacement, "000002.SZ": replacement})

        assert store.read("000001.SZ", date(2024, 1, 3), "close") == pytest.approx(
            sample_df.loc[sample_df["date"] == pd.Timestamp("2024-01-03"), "close"].iloc[0]
        )
        assert store.read("000002.SZ", date(2024, 1, 3), "close") == pytest.approx(
            sample_df.loc[sample_df["date"] == pd.Timestamp("2024-01-03"), "close"].iloc[0]
        )

    def test_write_batch_rejects_incomplete_ohlcv(self, store):
        with pytest.raises(ValueError, match="required columns"):
            store.write_batch({
                "000001.SZ": pd.DataFrame({"date": [pd.Timestamp("2024-01-02")]})
            })


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

    def test_index_component_snapshot_is_point_in_time(self, db):
        db.replace_index_components(
            "999999.SH", date(2024, 1, 31),
            [{"code": "000001.SZ", "name": "平安银行", "weight": 1.2}],
            source="test:snapshot",
        )
        db.replace_index_components(
            "999999.SH", date(2024, 7, 31),
            [{"code": "600000.SH", "name": "浦发银行", "weight": 1.1}],
            source="test:snapshot",
        )

        january = db.get_index_components("999999.SH", date(2024, 3, 1))
        august = db.get_index_components("999999.SH", date(2024, 8, 1))
        before = db.get_index_components("999999.SH", date(2023, 12, 31))

        assert [row["code"] for row in january] == ["000001.SZ"]
        assert [row["code"] for row in august] == ["600000.SH"]
        assert before == []
        assert db.get_index_snapshot_coverage("999999.SH") == [
            {"as_of": "2024-01-31", "constituent_count": 1},
            {"as_of": "2024-07-31", "constituent_count": 1},
        ]

    def test_index_snapshot_deduplicates_and_rejects_nonfinite_weight(self, db):
        count = db.replace_index_components(
            "999999.SH", date(2024, 1, 31),
            [
                {"code": "000001.SZ", "name": "平安银行", "weight": 1.0},
                {"code": "000001.SZ", "name": "重复", "weight": 1.0},
            ],
        )
        assert count == 1
        with pytest.raises(ValueError, match="invalid index weight"):
            db.replace_index_components(
                "999999.SH", date(2024, 2, 1),
                [{"code": "000002.SZ", "name": "万科A", "weight": float("inf")}],
            )

    def test_index_snapshot_rejects_future_date_at_storage_boundary(self, db):
        tomorrow = date.today() + timedelta(days=1)
        with pytest.raises(ValueError, match="future"):
            db.replace_index_components(
                "999999.SH", tomorrow,
                [{"code": "000001.SZ", "name": "平安银行"}],
            )
        with pytest.raises(ValueError, match="query date cannot be in the future"):
            db.get_index_components("999999.SH", tomorrow)

    def test_index_snapshot_received_at_requires_timezone(self, db):
        with pytest.raises(ValueError, match="include a timezone"):
            db.replace_index_components(
                "999999.SH", date(2024, 1, 31),
                [{"code": "000001.SZ", "name": "平安银行"}],
                received_at="2024-02-01T00:00:00",
            )

    def test_index_snapshot_batch_validates_before_atomic_write(self, db):
        db.replace_index_components(
            "999999.SH", date(2024, 1, 31),
            [{"code": "000001.SZ", "name": "旧快照"}],
        )
        with pytest.raises(ValueError, match="invalid constituent code"):
            db.replace_index_components_batch([
                {
                    "index_code": "999999.SH", "as_of": date(2024, 1, 31),
                    "rows": [{"code": "000002.SZ", "name": "新快照"}],
                },
                {
                    "index_code": "999999.SH", "as_of": date(2024, 2, 29),
                    "rows": [{"code": "not-a-code", "name": "坏数据"}],
                },
            ])
        assert db.get_index_components("999999.SH", date(2024, 2, 1))[0]["code"] == "000001.SZ"

    def test_index_snapshot_batch_success_and_dry_validation(self, db):
        snapshots = [
            {
                "index_code": "999999.SH", "as_of": date(2024, 1, 31),
                "rows": [{"code": "000001.SZ", "name": "平安银行"}],
                "source": "test:batch",
            },
            {
                "index_code": "999999.SH", "as_of": date(2024, 2, 29),
                "rows": [{"code": "600000.SH", "name": "浦发银行"}],
                "source": "test:batch",
            },
        ]
        validation = db.validate_index_components_batch(snapshots)
        assert [item["as_of"] for item in validation] == ["2024-01-31", "2024-02-29"]
        assert db.get_index_components("999999.SH", date(2024, 2, 15)) == []
        assert db.replace_index_components_batch(snapshots) == 2
        assert db.get_index_components("999999.SH", date(2024, 2, 15))[0]["code"] == "000001.SZ"

    def test_known_index_requires_minimum_coverage_and_valid_exchange(self, db):
        with pytest.raises(ValueError, match="at least 240"):
            db.replace_index_components(
                "000300", date(2024, 1, 31),
                [{"code": "000001.SZ", "name": "平安银行"}],
            )
        with pytest.raises(ValueError, match="conflicts"):
            db.replace_index_components(
                "999999.SH", date(2024, 1, 31),
                [{"code": "600519.BJ", "name": "贵州茅台"}],
            )

    def test_update_log(self, db):
        db.log_update("akshare_daily", 5000, "success")
        log = db.get_last_update("akshare_daily")
        assert log["record_count"] == 5000
        assert log["status"] == "success"

    def test_legacy_index_snapshot_schema_is_isolated_on_open(self, tmp_path):
        legacy_path = tmp_path / "legacy-meta.db"
        with sqlite3.connect(legacy_path) as conn:
            conn.execute(
                "CREATE TABLE index_components (index_code TEXT, as_of TEXT, code TEXT)"
            )
        migrated = MetaDB(db_path=str(legacy_path))
        with sqlite3.connect(legacy_path) as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert "index_components" in tables
        assert any(name.startswith("index_components_legacy") for name in tables)
        assert migrated.get_index_snapshot_coverage("999999.SH") == []

    def test_legacy_index_snapshot_missing_index_columns_is_migrated_before_index_creation(self, tmp_path):
        legacy_path = tmp_path / "legacy-minimal-meta.db"
        with sqlite3.connect(legacy_path) as conn:
            conn.execute("CREATE TABLE index_components (code TEXT)")

        MetaDB(db_path=str(legacy_path))

        with sqlite3.connect(legacy_path) as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(index_components)").fetchall()
            }
            indexes = {
                row[1] for row in conn.execute("PRAGMA index_list(index_components)").fetchall()
            }
        assert {"index_code", "as_of", "code", "source", "received_at"} <= columns
        assert "idx_index_components_lookup" in indexes

    def test_legacy_nullable_snapshot_schema_is_migrated(self, tmp_path):
        legacy_path = tmp_path / "legacy-nullable-meta.db"
        with sqlite3.connect(legacy_path) as conn:
            conn.execute(
                "CREATE TABLE index_components ("
                "index_code TEXT, as_of TEXT, code TEXT, name TEXT, weight REAL, "
                "source TEXT, received_at TEXT, "
                "PRIMARY KEY (index_code, as_of, code))"
            )

        MetaDB(db_path=str(legacy_path))

        with sqlite3.connect(legacy_path) as conn:
            table_info = conn.execute("PRAGMA table_info(index_components)").fetchall()
        not_null = {row[1] for row in table_info if row[3] or row[5]}
        assert {"index_code", "as_of", "code", "source", "received_at"} <= not_null
