from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from server.models.database import Base
from server.models.schema import FundNavDataset, FundNavDatasetRow
from server.services.fund_nav_archive import archive_fund_nav_dataset, get_fund_nav_dataset, list_fund_nav_datasets


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_fund_nav_archive_is_content_addressed_and_immutable():
    db = _db()
    rows = [
        {"date": "2024-01-02", "nav": 1.0, "change_pct": None},
        {"date": "2024-01-03", "nav": 1.1, "change_pct": 10.0},
    ]
    first = archive_fund_nav_dataset(db, code="FUND:110022", rows=rows,
                                     start_date=date(2024, 1, 1), end_date=date(2024, 1, 4))
    replay = archive_fund_nav_dataset(db, code="110022", rows=list(reversed(rows)),
                                      start_date=date(2024, 1, 1), end_date=date(2024, 1, 4))
    assert replay["dataset_id"] == first["dataset_id"]
    assert first["immutable"] is True
    assert db.query(FundNavDataset).count() == 1
    assert db.query(FundNavDatasetRow).count() == 2
    assert get_fund_nav_dataset(db, first["dataset_id"], include_rows=True)["rows"][1]["nav"] == 1.1

    corrected = archive_fund_nav_dataset(db, code="110022", rows=[
        {"date": "2024-01-02", "nav": 1.0},
        {"date": "2024-01-03", "nav": 1.2},
    ], start_date=date(2024, 1, 1), end_date=date(2024, 1, 4))
    assert corrected["dataset_id"] != first["dataset_id"]
    assert db.query(FundNavDataset).count() == 2


def test_fund_nav_archive_rejects_invalid_source_future_and_duplicate_dates():
    db = _db()
    with pytest.raises(ValueError, match="source=eastmoney"):
        archive_fund_nav_dataset(db, code="110022", rows=[{"date": "2024-01-02", "nav": 1}], source="manual")
    future = (date.today() + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="no usable rows"):
        archive_fund_nav_dataset(db, code="110022", rows=[{"date": future, "nav": 1}])
    with pytest.raises(ValueError, match="duplicate"):
        archive_fund_nav_dataset(db, code="110022", rows=[
            {"date": "2024-01-02", "nav": 1}, {"date": "2024-01-02", "nav": 1.1}
        ])


def test_fund_nav_archive_list_filters_by_code():
    db = _db()
    archive_fund_nav_dataset(db, code="110022", rows=[{"date": "2024-01-02", "nav": 1}])
    archive_fund_nav_dataset(db, code="161725", rows=[{"date": "2024-01-02", "nav": 1}])
    assert len(list_fund_nav_datasets(db, code="110022")) == 1
    assert len(list_fund_nav_datasets(db)) == 2
