import pytest
from datetime import date
from quant_engine.data.fetcher.base import DataSource
from quant_engine.data.fetcher.akshare_adapter import AKShareAdapter


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
