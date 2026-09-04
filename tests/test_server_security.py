import pytest

from server.api.backtest import _import_strategy, BacktestRunRequest
from strategies.small_cap_value import SmallCapValueStrategy


def test_import_strategy_allows_local_strategy_subclass():
    loaded = _import_strategy(
        "strategies.small_cap_value.SmallCapValueStrategy"
    )
    assert loaded is SmallCapValueStrategy


@pytest.mark.parametrize("path", [
    "os.system",
    "server.main.app",
    "quant_engine.backtest.engine.BacktestEngine",
])
def test_import_strategy_rejects_non_strategy_modules(path):
    with pytest.raises(ValueError, match="local strategies package"):
        _import_strategy(path)


@pytest.mark.parametrize("payload", [
    {"strategy_id": "s", "start_date": "2024-01-01", "end_date": "2024-01-02", "initial_capital": 0},
    {"strategy_id": "s", "start_date": "2024-02-30", "end_date": "2024-03-01"},
    {"strategy_id": "s", "start_date": "2024-02-01", "end_date": "2024-01-01"},
    {"strategy_id": "s", "start_date": "2024-01-01", "end_date": "2024-01-02", "rebalance_frequency": "hourly"},
])
def test_backtest_request_rejects_unsafe_parameters(payload):
    with pytest.raises(Exception):
        BacktestRunRequest.model_validate(payload)
