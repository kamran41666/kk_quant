import pytest
from datetime import date


@pytest.fixture
def sample_dates():
    """已知的 A 股交易日/非交易日样本 (手动验证过的)"""
    return {
        "trading": [date(2024, 1, 2), date(2024, 1, 3), date(2024, 12, 31)],
        "holiday": [date(2024, 1, 1), date(2024, 2, 12)],
        "weekend": [date(2024, 1, 6), date(2024, 1, 7)],
    }
