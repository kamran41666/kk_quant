from datetime import UTC, datetime

import pandas as pd

from scripts.import_copa_dataset import _incremental_hfq_events, _normalize_daily


def test_incremental_hfq_events_reconstruct_published_factor_path():
    raw = pd.DataFrame({
        "date": ["2024-01-03", "2023-01-03", "1900-01-01"],
        "hfq_factor": [1.32, 1.20, 1.00],
    })
    events = _incremental_hfq_events(
        "600001.SH", raw, datetime.now(UTC).isoformat()
    )
    assert events["factor"].tolist() == [1.0, 1.2, 1.1]
    assert events["factor"].cumprod().tolist() == [1.0, 1.2, 1.32]


def test_normalize_daily_rejects_invalid_ohlc_and_preserves_source():
    raw = pd.DataFrame([{
        "date": "2024-01-02",
        "open": 10.0,
        "high": 10.5,
        "low": 9.5,
        "close": 10.2,
        "volume": 1000,
        "amount": 10_000,
        "turnover": 0.01,
    }])
    result = _normalize_daily("600001.SH", raw, "2024-01-02T16:00:00+00:00")
    assert result.iloc[0]["turnover_rate"] == 1.0
    assert result.iloc[0]["source"] == "akshare:tencent:stock_zh_a_hist_tx"

    invalid = raw.copy()
    invalid.loc[0, "high"] = 9.0
    try:
        _normalize_daily("600001.SH", invalid, "2024-01-02T16:00:00+00:00")
    except ValueError as exc:
        assert "invalid rows" in str(exc)
    else:
        raise AssertionError("invalid OHLC row was accepted")
