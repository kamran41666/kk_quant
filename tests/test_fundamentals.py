from datetime import date
import json

import numpy as np
import pandas as pd
import pytest

from quant_engine.data.store import MetaDB
from server.api import market


def _row(*, announce_date="2024-02-15", value=10.0, field="roe", **extra):
    return {
        "code": "000001.SZ",
        "report_date": "2023-12-31",
        "announce_date": announce_date,
        "field": field,
        "value": value,
        "source": "test:fundamentals",
        **extra,
    }


def test_fundamentals_select_latest_announcement_visible_at_as_of(tmp_path):
    db = MetaDB(db_path=str(tmp_path / "meta.db"))
    db.replace_fundamentals_batch([
        _row(announce_date="2024-02-15", value=10.0),
        _row(announce_date="2024-04-15", value=12.0),
        _row(announce_date="2024-02-15", field="pb", value=1.2),
    ])

    before_revision = db.get_fundamentals(
        ["000001.SZ"], date(2024, 3, 1), ["2023-12-31"], ["roe", "pb"]
    )
    assert before_revision.loc[("000001.SZ", date(2023, 12, 31), "roe"), "value"] == 10.0
    assert before_revision.loc[("000001.SZ", date(2023, 12, 31), "pb"), "value"] == 1.2

    after_revision = db.get_fundamentals(
        ["000001.SZ"], date(2024, 5, 1), ["2023-12-31"], ["roe"]
    )
    assert after_revision.loc[("000001.SZ", date(2023, 12, 31), "roe"), "value"] == 12.0


def test_fundamental_query_preserves_provenance_and_empty_contract(tmp_path):
    db = MetaDB(db_path=str(tmp_path / "meta.db"))
    db.replace_fundamentals_batch([_row(received_at="2024-02-16T08:00:00+08:00")])
    frame = db.get_fundamentals(["000001.SZ"], date(2024, 3, 1))
    record = frame.iloc[0]
    assert record["announce_date"] == date(2024, 2, 15)
    assert record["source"] == "test:fundamentals"
    assert record["received_at"].endswith("+00:00")

    empty = db.get_fundamentals(["000002.SZ"], date(2024, 3, 1))
    assert empty.empty
    assert list(empty.index.names) == ["code", "report_date", "field"]


@pytest.mark.parametrize("bad", [
    {"announce_date": "2024-02-01", "report_date": "2024-03-31"},
    {"announce_date": "2999-01-01"},
    {"value": np.nan},
    {"field": "ROE%"},
])
def test_fundamental_import_rejects_unsafe_rows(tmp_path, bad):
    db = MetaDB(db_path=str(tmp_path / "meta.db"))
    row = _row(**bad)
    with pytest.raises(ValueError):
        db.validate_fundamentals([row])
    assert db.get_fundamental_coverage() == []


def test_fundamental_batch_is_atomic_when_data_version_is_invalid(tmp_path):
    db = MetaDB(db_path=str(tmp_path / "meta.db"))
    with pytest.raises(ValueError, match="data version"):
        db.replace_fundamentals_batch([_row()], data_versions={"": "bad"})
    assert db.get_fundamental_coverage() == []


def test_fundamental_revisions_are_visible_in_coverage(tmp_path):
    db = MetaDB(db_path=str(tmp_path / "meta.db"))
    db.replace_fundamentals_batch([
        _row(announce_date="2024-02-15", value=10.0),
        _row(announce_date="2024-04-15", value=12.0),
    ])
    assert db.get_fundamental_coverage("000001.SZ") == [{
        "code": "000001.SZ",
        "report_date": "2023-12-31",
        "field_count": 1,
        "announcement_versions": 2,
    }]


def test_importer_supports_json_and_sha256(tmp_path, monkeypatch):
    from scripts import import_fundamentals as importer

    source = tmp_path / "fundamentals.json"
    source.write_text(json.dumps({"rows": [_row()]}), encoding="utf-8")
    db = MetaDB(db_path=str(tmp_path / "meta.db"))
    monkeypatch.setattr(importer, "MetaDB", lambda: db)

    dry = importer.import_fundamentals(source, dry_run=True)
    assert dry["status"] == "validated"
    assert len(dry["sha256"]) == 64
    assert db.get_fundamental_coverage() == []

    result = importer.import_fundamentals(source)
    assert result["status"] == "ok"
    assert result["row_count"] == 1
    assert db.get_fundamental_coverage()[0]["field_count"] == 1


def test_market_fundamentals_endpoint_returns_pit_rows(tmp_path, monkeypatch):
    db = MetaDB(db_path=str(tmp_path / "meta.db"))
    db.replace_fundamentals_batch([_row(value=9.5)])

    class StubAPI:
        def fundamentals(self, codes, report_dates=None, fields=None, as_of=None):
            return db.get_fundamentals(codes, as_of, report_dates, fields)

        def fundamental_coverage(self, code=None):
            return db.get_fundamental_coverage(code)

    monkeypatch.setattr(market, "DataAPI", StubAPI)
    response = market.get_fundamentals(
        "000001.SZ", as_of=date(2024, 3, 1),
        report_dates="2023-12-31", fields="roe",
    )
    assert response["meta"]["status"] == "ok"
    assert response["meta"]["pit_rule"].startswith("announce_date")
    assert response["data"][0]["value"] == 9.5


def test_market_fundamentals_endpoint_rejects_future_as_of():
    with pytest.raises(Exception) as raised:
        market.get_fundamentals(
            "000001.SZ", as_of=date.today().replace(year=date.today().year + 1)
        )
    assert getattr(raised.value, "status_code", None) == 422
