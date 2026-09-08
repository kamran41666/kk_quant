import pandas as pd
from fastapi.routing import APIRoute

from quant_engine.data.api import DataAPI
from quant_engine.data.store import PriceStore
from server.api import market


def _daily_frame(dates: list[str]) -> pd.DataFrame:
    size = len(dates)
    return pd.DataFrame({
        "date": pd.to_datetime(dates),
        "open": [10.0] * size,
        "high": [11.0] * size,
        "low": [9.0] * size,
        "close": [10.5] * size,
        "volume": [1000] * size,
    })


def test_price_store_inventory_is_empty_without_local_partitions(tmp_path):
    store = PriceStore(base_dir=str(tmp_path))

    assert store.inventory() == []


def test_price_store_inventory_aggregates_securities_and_real_date_ranges(tmp_path):
    store = PriceStore(base_dir=str(tmp_path))
    store.write("000001.SZ", _daily_frame(["2023-12-29", "2024-01-02"]))
    store.write("600000.SH", _daily_frame(["2024-04-01", "2024-04-02", "2024-04-03"]))

    assert store.inventory() == [
        {
            "code": "000001.SZ",
            "file_count": 2,
            "row_count": 2,
            "start_date": "2023-12-29",
            "end_date": "2024-01-02",
            "read_errors": [],
        },
        {
            "code": "600000.SH",
            "file_count": 1,
            "row_count": 3,
            "start_date": "2024-04-01",
            "end_date": "2024-04-03",
            "read_errors": [],
        },
    ]


def test_price_store_inventory_excludes_corrupt_file_rows_and_reports_error(tmp_path):
    store = PriceStore(base_dir=str(tmp_path))
    store.write("000001.SZ", _daily_frame(["2024-01-02", "2024-01-03"]))
    corrupt = store._base / "year=2024" / "quarter=2" / "000001.SZ.parquet"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_bytes(b"not a parquet file")

    item = store.inventory()[0]

    assert item["file_count"] == 2
    assert item["row_count"] == 2
    assert item["start_date"] == "2024-01-02"
    assert item["end_date"] == "2024-01-03"
    assert len(item["read_errors"]) == 1
    assert "year=2024/quarter=2/000001.SZ.parquet" in item["read_errors"][0]


def test_data_api_daily_inventory_only_delegates_to_price_store():
    expected = [{"code": "000001.SZ"}]

    class StubPriceStore:
        def inventory(self):
            return expected

    api = object.__new__(DataAPI)
    api._price_store = StubPriceStore()

    assert api.daily_inventory() is expected


def test_daily_inventory_http_contract_uses_data_api(monkeypatch):
    class StubDataAPI:
        def daily_inventory(self):
            return [
                {
                    "code": "000001.SZ",
                    "file_count": 2,
                    "row_count": 3,
                    "start_date": "2023-12-29",
                    "end_date": "2024-01-03",
                    "read_errors": ["year=2024/quarter=2/000001.SZ.parquet: invalid"],
                },
                {
                    "code": "600000.SH",
                    "file_count": 1,
                    "row_count": 4,
                    "start_date": "2024-04-01",
                    "end_date": "2024-04-04",
                    "read_errors": [],
                },
            ]

    monkeypatch.setattr(market, "DataAPI", lambda: StubDataAPI())
    inventory_route = next(
        route for route in market.router.routes
        if isinstance(route, APIRoute) and route.endpoint is market.get_daily_inventory
    )

    assert inventory_route.path == "/market/inventory/daily"
    assert inventory_route.methods == {"GET"}
    assert market.get_daily_inventory() == {
        "items": StubDataAPI().daily_inventory(),
        "summary": {
            "security_count": 2,
            "file_count": 3,
            "row_count": 7,
            "start_date": "2023-12-29",
            "end_date": "2024-04-04",
            "error_count": 1,
        },
        "source": "local:parquet",
        "coverage_verified": False,
    }
