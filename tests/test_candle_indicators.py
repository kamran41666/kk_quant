from datetime import date
import math

import pandas as pd
import pytest

from quant_engine.analytics.candle_indicators import (
    CandleDataError,
    add_indicators,
    aggregate_candles,
    expand_indicators,
)


def rows(start="2026-01-01", count=80):
    dates = pd.date_range(start, periods=count, freq="B")
    return [
        {
            "date": stamp.date().isoformat(),
            "open": float(10 + index),
            "high": float(10.5 + index),
            "low": float(9.5 + index),
            "close": float(10.25 + index),
            "volume": float(100 + index),
            "source": "test:daily",
            "adjust": "event_driven",
        }
        for index, stamp in enumerate(dates)
    ]


def test_weekly_and_monthly_use_actual_last_trading_day_and_sum_volume():
    source = [
        {"date": "2026-01-02", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 2},
        {"date": "2026-01-05", "open": 11, "high": 13, "low": 10, "close": 12, "volume": 3},
        {"date": "2026-01-09", "open": 12, "high": 14, "low": 11, "close": 13, "volume": 4},
        {"date": "2026-02-02", "open": 13, "high": 15, "low": 12, "close": 14, "volume": 5},
    ]

    weekly = aggregate_candles(source, "1w")
    monthly = aggregate_candles(source, "1mo")

    assert [row["date"] for row in weekly] == ["2026-01-02", "2026-01-09", "2026-02-02"]
    assert weekly[1]["period_start"] == "2026-01-05"
    assert weekly[1]["period_end"] == "2026-01-09"
    assert weekly[1]["open"] == 11.0
    assert weekly[1]["close"] == 13.0
    assert weekly[1]["high"] == 14.0
    assert weekly[1]["low"] == 10.0
    assert weekly[1]["volume"] == 7.0
    assert monthly[0]["date"] == "2026-01-09"
    assert monthly[0]["volume"] == 9.0


def test_daily_rows_are_sorted_deduplicated_and_invalid_ohlc_is_dropped():
    source = [
        {"date": "2026-01-03", "open": 12, "high": 13, "low": 11, "close": 12.5},
        {"date": "2026-01-02", "open": 10, "high": 11, "low": 9, "close": 10.5},
        # last duplicate wins
        {"date": "2026-01-02", "open": 10.1, "high": 11.2, "low": 9.9, "close": 10.8},
        # high/low relation is impossible and must not become a candle
        {"date": "2026-01-04", "open": 13, "high": 12, "low": 11, "close": 12.5},
    ]
    result = aggregate_candles(source)
    assert [row["date"] for row in result] == ["2026-01-02", "2026-01-03"]
    assert result[0]["close"] == 10.8


def test_missing_volume_remains_null_and_empty_input_is_safe():
    result = aggregate_candles([
        {"date": "2026-01-02", "open": 10, "high": 11, "low": 9, "close": 10.5},
    ], "1mo")
    assert result[0]["volume"] is None
    assert aggregate_candles([]) == []


def test_indicator_groups_expand_without_duplicates():
    assert expand_indicators(["ma", "ma20", "macd"]) == (
        "ma5", "ma20", "ma60", "macd", "macd_signal", "macd_hist"
    )
    with pytest.raises(CandleDataError):
        expand_indicators(["unknown"])


def test_ma_ema_warmup_and_known_values_are_causal():
    source = rows(count=80)
    result, fields = add_indicators(source, ["ma", "ema"])
    assert fields == ("ma5", "ma20", "ma60", "ema12", "ema26")
    assert result[3]["ma5"] is None
    assert result[4]["ma5"] == pytest.approx(sum(10.25 + i for i in range(5)) / 5)
    assert result[18]["ma20"] is None
    assert result[19]["ma20"] == pytest.approx(sum(10.25 + i for i in range(20)) / 20)
    assert result[10]["ema12"] is None
    assert result[11]["ema12"] is not None
    assert result[25]["ema26"] is not None


def test_rsi_flat_and_directional_windows_have_explicit_values():
    base = rows(count=20)
    up, _ = add_indicators(base, ["rsi14"])
    assert up[-1]["rsi14"] == 100.0

    down = [dict(item, close=20 - i, open=20 - i, high=21 - i, low=19 - i) for i, item in enumerate(base)]
    down_result, _ = add_indicators(down, ["rsi14"])
    assert down_result[-1]["rsi14"] == 0.0

    flat = [dict(item, close=10, open=10, high=10.5, low=9.5) for item in base]
    flat_result, _ = add_indicators(flat, ["rsi14"])
    assert flat_result[-1]["rsi14"] == 50.0


def test_rsi_uses_wilder_seed_for_first_complete_window():
    closes = [44, 44.15, 43.9, 44.35, 44.8, 45.1, 44.9, 45.2, 45.6, 45.3, 45.0, 45.4, 45.9, 46.2, 46.0]
    source = [
        {"date": f"2026-01-{index + 1:02d}", "open": close, "high": close + 0.2, "low": close - 0.2, "close": close}
        for index, close in enumerate(closes)
    ]
    result, _ = add_indicators(source, ["rsi14"])
    # The first 14 changes seed the Wilder averages before recursion.
    assert result[14]["rsi14"] == pytest.approx(72.222222, abs=1e-6)


def test_macd_boll_kdj_have_no_nan_or_infinity_and_flat_kdj_is_null():
    result, fields = add_indicators(rows(count=80), ["macd", "boll", "kdj"])
    assert fields == (
        "macd", "macd_signal", "macd_hist",
        "boll_mid", "boll_upper", "boll_lower",
        "kdj_k", "kdj_d", "kdj_j",
    )
    for row in result:
        for key, value in row.items():
            if key in fields and value is not None:
                assert math.isfinite(value)

    flat = [dict(item, close=10, open=10, high=10, low=10) for item in rows(count=20)]
    flat_result, _ = add_indicators(flat, ["kdj"])
    assert flat_result[-1]["kdj_k"] is None
    assert flat_result[-1]["kdj_d"] is None
    assert flat_result[-1]["kdj_j"] is None


def test_kdj_starts_at_first_valid_nine_day_rsv():
    result, _ = add_indicators(rows(count=12), ["kdj"])
    assert result[7]["kdj_k"] is None
    assert result[8]["kdj_k"] is not None
    assert result[8]["kdj_d"] == pytest.approx(result[8]["kdj_k"])


def test_future_rows_do_not_change_prior_indicator_values():
    before, _ = add_indicators(rows(count=60), ["ma", "ema", "rsi14", "macd", "boll", "kdj"])
    extended_source = rows(count=70)
    after, _ = add_indicators(extended_source, ["ma", "ema", "rsi14", "macd", "boll", "kdj"])
    for index in range(60):
        for field in ("ma5", "ma20", "ma60", "ema12", "ema26", "rsi14", "macd", "macd_signal", "macd_hist", "boll_mid", "boll_upper", "boll_lower", "kdj_k", "kdj_d", "kdj_j"):
            assert after[index][field] == before[index][field]
