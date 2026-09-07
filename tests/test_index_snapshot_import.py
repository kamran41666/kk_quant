import json
from datetime import date

import pandas as pd
import pytest

from scripts.import_index_snapshots import import_snapshots, load_snapshots
import scripts.import_index_snapshots as import_module
from quant_engine.data.store import MetaDB


def test_csv_snapshot_import_supports_dry_run_and_atomic_write(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_DATA_DIR", str(tmp_path / "data"))
    path = tmp_path / "components.csv"
    pd.DataFrame([
        {"index_code": "999999.SH", "as_of": "2024-01-31", "code": "000001.SZ", "name": "平安银行", "weight": "1.2"},
        {"index_code": "999999.SH", "as_of": "2024-02-29", "code": "600000.SH", "name": "浦发银行", "weight": "1.1"},
    ]).to_csv(path, index=False)

    preview = import_snapshots(path, dry_run=True)
    assert preview["status"] == "validated"
    assert preview["total_constituents"] == 2
    assert MetaDB().get_index_components("999999.SH", date(2024, 2, 1)) == []

    result = import_snapshots(path)
    assert result["status"] == "ok"
    db = MetaDB()
    assert db.get_index_components("999999.SH", date(2024, 2, 1))[0]["code"] == "000001.SZ"
    assert db.get_data_version("index_snapshot_last_sha256") == result["sha256"]


def test_nested_json_is_grouped_into_snapshot_contract(tmp_path):
    path = tmp_path / "components.json"
    path.write_text(json.dumps({"snapshots": [
        {"index_code": "999999.SH", "as_of": "2024-01-31", "source": "vendor:test",
         "received_at": "2024-02-01T00:00:00+00:00", "rows": [
            {"code": "000001.SZ", "name": "平安银行"},
        ]},
    ]}), encoding="utf-8")
    loaded = load_snapshots(path)
    assert loaded[0]["as_of"] == date(2024, 1, 31)
    assert loaded[0]["rows"][0]["code"] == "000001.SZ"
    assert loaded[0]["source"] == "vendor:test"
    assert loaded[0]["received_at"] == "2024-02-01T00:00:00+00:00"


def test_parquet_timestamp_is_normalized_to_date(tmp_path):
    path = tmp_path / "components.parquet"
    pd.DataFrame([{
        "index_code": "999999.SH",
        "as_of": pd.Timestamp("2024-01-31 00:00:00"),
        "code": "000001.SZ",
        "name": "平安银行",
        "weight": 1.2,
        "received_at": pd.Timestamp("2024-02-01 08:00:00"),
    }]).to_parquet(path, index=False)

    loaded = load_snapshots(path)

    assert loaded[0]["as_of"] == date(2024, 1, 31)
    assert isinstance(loaded[0]["as_of"], date)
    assert loaded[0]["received_at"] == "2024-02-01T08:00:00+00:00"


def test_empty_constituent_code_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_DATA_DIR", str(tmp_path / "data"))
    path = tmp_path / "bad.csv"
    pd.DataFrame([{
        "index_code": "999999.SH", "as_of": "2024-01-31",
        "code": "", "name": "无代码",
    }]).to_csv(path, index=False)

    with pytest.raises(ValueError, match="empty constituent code"):
        import_snapshots(path, dry_run=True)


def test_conflicting_received_at_values_are_rejected(tmp_path):
    path = tmp_path / "conflict.json"
    path.write_text(json.dumps([
        {"index_code": "999999.SH", "as_of": "2024-01-31", "code": "000001.SZ",
         "name": "平安银行", "received_at": "2024-02-01T00:00:00+00:00"},
        {"index_code": "999999.SH", "as_of": "2024-01-31", "code": "000002.SZ",
         "name": "万科A", "received_at": "2024-02-02T00:00:00+00:00"},
    ]), encoding="utf-8")

    with pytest.raises(ValueError, match="mixed received_at"):
        load_snapshots(path)


def test_equivalent_received_at_offsets_are_normalized_before_compare(tmp_path):
    path = tmp_path / "equivalent-times.json"
    path.write_text(json.dumps([
        {"index_code": "999999.SH", "as_of": "2024-01-31", "code": "000001.SZ",
         "name": "平安银行", "received_at": "2024-02-01T00:00:00+00:00"},
        {"index_code": "999999.SH", "as_of": "2024-01-31", "code": "000002.SZ",
         "name": "万科A", "received_at": "2024-02-01T08:00:00+08:00"},
    ]), encoding="utf-8")

    loaded = load_snapshots(path)
    assert loaded[0]["received_at"] == "2024-02-01T00:00:00+00:00"


def test_import_rejects_file_changed_during_validation(tmp_path, monkeypatch):
    path = tmp_path / "changing.csv"
    pd.DataFrame([{
        "index_code": "999999.SH", "as_of": "2024-01-31",
        "code": "000001.SZ", "name": "平安银行",
    }]).to_csv(path, index=False)
    original = import_module.load_snapshots

    def mutate_then_load(input_path, source="import:index_snapshot"):
        input_path = __import__("pathlib").Path(input_path)
        records = original(input_path, source=source)
        input_path.write_text("changed", encoding="utf-8")
        return records

    monkeypatch.setattr(import_module, "load_snapshots", mutate_then_load)
    with pytest.raises(ValueError, match="changed during import validation"):
        import_snapshots(path, dry_run=True)
