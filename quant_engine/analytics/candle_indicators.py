"""Deterministic OHLCV aggregation and beginner-friendly indicators.

The module deliberately operates only on rows that have already crossed the
market-data provider boundary.  It never fills missing prices and never uses a
future row while computing a value for the current candle.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
import math
from typing import Any

import numpy as np
import pandas as pd


SUPPORTED_INTERVALS = {"1d", "1w", "1mo"}
INDICATOR_GROUPS = {
    "ma": ("ma5", "ma20", "ma60"),
    "ema": ("ema12", "ema26"),
    "macd": ("macd", "macd_signal", "macd_hist"),
    "boll": ("boll_mid", "boll_upper", "boll_lower"),
    "kdj": ("kdj_k", "kdj_d", "kdj_j"),
    "rsi14": ("rsi14",),
    "ma5": ("ma5",),
    "ma20": ("ma20",),
    "ma60": ("ma60",),
    "ema12": ("ema12",),
    "ema26": ("ema26",),
}


class CandleDataError(ValueError):
    """Raised when a candle request cannot produce a trustworthy result."""


def expand_indicators(values: Iterable[str] | None) -> tuple[str, ...]:
    """Expand user-facing indicator groups into stable response fields."""
    if values is None:
        return ()
    fields: list[str] = []
    for value in values:
        key = str(value).strip().lower()
        if not key:
            continue
        if key not in INDICATOR_GROUPS:
            raise CandleDataError(f"unsupported indicator: {key}")
        for field in INDICATOR_GROUPS[key]:
            if field not in fields:
                fields.append(field)
    return tuple(fields)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _record_value(value: Any) -> float | int | str | None:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return None
    if isinstance(value, (np.floating, float, np.integer, int)):
        number = float(value)
        if not math.isfinite(number):
            return None
        return number
    return value


def _wilder_average(values: pd.Series, period: int) -> pd.Series:
    """Wilder smoothing seeded by the first full period's simple average."""
    result = pd.Series(np.nan, index=values.index, dtype=float)
    if len(values) <= period:
        return result
    seed = values.iloc[1:period + 1].dropna()
    if len(seed) != period:
        return result
    result.iloc[period] = float(seed.mean())
    for index in range(period + 1, len(values)):
        current = values.iloc[index]
        previous = result.iloc[index - 1]
        if pd.isna(current) or pd.isna(previous):
            continue
        result.iloc[index] = (previous * (period - 1) + float(current)) / period
    return result


def _as_frame(rows: Iterable[Mapping[str, Any]] | pd.DataFrame) -> pd.DataFrame:
    frame = rows.copy() if isinstance(rows, pd.DataFrame) else pd.DataFrame(list(rows))
    required = {"date", "open", "high", "low", "close"}
    if frame.empty and not set(frame.columns):
        return pd.DataFrame(columns=sorted(required))
    missing = required - set(frame.columns)
    if missing:
        raise CandleDataError(f"candle rows missing fields: {', '.join(sorted(missing))}")
    frame = frame.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for field in ("open", "high", "low", "close", "volume", "amount"):
        if field in frame.columns:
            frame[field] = pd.to_numeric(frame[field], errors="coerce")
    frame = frame.dropna(subset=["date", "open", "high", "low", "close"])
    frame = frame[
        (frame["open"] > 0)
        & (frame["high"] > 0)
        & (frame["low"] > 0)
        & (frame["close"] > 0)
        & (frame["high"] >= frame[["open", "close", "low"]].max(axis=1))
        & (frame["low"] <= frame[["open", "close", "high"]].min(axis=1))
    ]
    # Duplicate dates are not valid separate candles. Keep the last provider
    # row so a retry cannot create a visually duplicated bar.
    return frame.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)


def _source_for(frame: pd.DataFrame) -> str | None:
    if "source" not in frame.columns:
        return None
    values = [str(value) for value in frame["source"].dropna().unique() if str(value).strip()]
    return ",".join(values) if values else None


def _base_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    source = _source_for(frame)
    adjust = None
    if "adjust" in frame.columns:
        values = [str(value) for value in frame["adjust"].dropna().unique() if str(value).strip()]
        adjust = values[0] if values else None
    for _, row in frame.iterrows():
        item: dict[str, Any] = {
            "date": row["date"].date().isoformat(),
            "period_start": row["date"].date().isoformat(),
            "period_end": row["date"].date().isoformat(),
            "open": _record_value(row["open"]),
            "high": _record_value(row["high"]),
            "low": _record_value(row["low"]),
            "close": _record_value(row["close"]),
            "volume": _record_value(row.get("volume")),
            "amount": _record_value(row.get("amount")),
        }
        if source:
            item["source"] = source
        if adjust:
            item["adjust"] = adjust
        records.append(item)
    return records


def aggregate_candles(
    rows: Iterable[Mapping[str, Any]] | pd.DataFrame,
    interval: str = "1d",
) -> list[dict[str, Any]]:
    """Return strictly ascending daily, weekly, or monthly OHLCV candles."""
    if interval not in SUPPORTED_INTERVALS:
        raise CandleDataError(f"unsupported interval: {interval}")
    frame = _as_frame(rows)
    if frame.empty:
        return []
    if interval == "1d":
        return _base_records(frame)

    bucket = "W-FRI" if interval == "1w" else "M"
    frame = frame.copy()
    frame["_bucket"] = frame["date"].dt.to_period(bucket)
    grouped: list[dict[str, Any]] = []
    for _, part in frame.groupby("_bucket", sort=True, observed=True):
        part = part.sort_values("date")
        row: dict[str, Any] = {
            # Use the last actual trading date, not a synthetic weekend/month
            # end, so tooltip dates always correspond to a source bar.
            "date": part["date"].iloc[-1].date().isoformat(),
            "period_start": part["date"].iloc[0].date().isoformat(),
            "period_end": part["date"].iloc[-1].date().isoformat(),
            "open": _record_value(part["open"].iloc[0]),
            "high": _record_value(part["high"].max()),
            "low": _record_value(part["low"].min()),
            "close": _record_value(part["close"].iloc[-1]),
            "volume": _record_value(part["volume"].sum(min_count=1)) if "volume" in part else None,
            "amount": _record_value(part["amount"].sum(min_count=1)) if "amount" in part else None,
        }
        source = _source_for(part)
        if source:
            row["source"] = source
        if "adjust" in part.columns:
            values = [str(value) for value in part["adjust"].dropna().unique() if str(value).strip()]
            if values:
                row["adjust"] = values[0]
        grouped.append(row)
    return grouped


def add_indicators(
    rows: Iterable[Mapping[str, Any]] | pd.DataFrame,
    indicators: Iterable[str] | None,
) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """Append selected causal indicator fields, using ``None`` warm-up gaps."""
    fields = expand_indicators(indicators)
    frame = _as_frame(rows)
    if frame.empty or not fields:
        return _base_records(frame), fields

    close = frame["close"].astype(float)
    low = frame["low"].astype(float)
    high = frame["high"].astype(float)
    calculated: dict[str, pd.Series] = {}
    requested = set(fields)

    for period, name in ((5, "ma5"), (20, "ma20"), (60, "ma60")):
        if name in requested:
            calculated[name] = close.rolling(period, min_periods=period).mean()
    for period, name in ((12, "ema12"), (26, "ema26")):
        if name in requested:
            calculated[name] = close.ewm(span=period, adjust=False, min_periods=period).mean()

    if {"macd", "macd_signal", "macd_hist"} & requested:
        ema12 = close.ewm(span=12, adjust=False, min_periods=12).mean()
        ema26 = close.ewm(span=26, adjust=False, min_periods=26).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()
        # The histogram follows the common Chinese charting convention:
        # ``柱 = 2 * (DIF - DEA)``. The response metadata documents this.
        calculated.update({"macd": macd, "macd_signal": signal, "macd_hist": 2 * (macd - signal)})

    if {"boll_mid", "boll_upper", "boll_lower"} & requested:
        mid = close.rolling(20, min_periods=20).mean()
        std = close.rolling(20, min_periods=20).std(ddof=0)
        calculated.update({"boll_mid": mid, "boll_upper": mid + 2 * std, "boll_lower": mid - 2 * std})

    if "rsi14" in requested:
        delta = close.diff()
        # Wilder's RSI is seeded with the first 14 actual changes, then
        # recursively smoothed with alpha=1/14. This avoids pandas' default
        # ewm seed, which produces a materially different early value.
        gain = _wilder_average(delta.clip(lower=0), 14)
        loss = _wilder_average(-delta.clip(upper=0), 14)
        rsi = pd.Series(np.nan, index=frame.index, dtype=float)
        active = loss > 0
        rsi.loc[active] = 100 - (100 / (1 + gain.loc[active] / loss.loc[active]))
        rsi.loc[(loss == 0) & (gain > 0)] = 100.0
        rsi.loc[(loss == 0) & (gain == 0)] = 50.0
        calculated["rsi14"] = rsi

    if {"kdj_k", "kdj_d", "kdj_j"} & requested:
        lowest = low.rolling(9, min_periods=9).min()
        highest = high.rolling(9, min_periods=9).max()
        spread = highest - lowest
        # A flat high/low window has no defined RSV. Keep it as a gap instead
        # of inventing a neutral 50 reading.
        rsv = ((close - lowest) / spread * 100).where(spread != 0)
        # Start K/D at the first valid 9-day RSV (the common beginner-facing
        # KDJ(9,3,3) convention), then smooth recursively.
        k = rsv.ewm(alpha=1 / 3, adjust=False, min_periods=1).mean()
        d = k.ewm(alpha=1 / 3, adjust=False, min_periods=1).mean()
        k = k.where(spread != 0)
        d = d.where(spread != 0)
        calculated.update({"kdj_k": k, "kdj_d": d, "kdj_j": 3 * k - 2 * d})

    records = _base_records(frame)
    for index, record in enumerate(records):
        for field in fields:
            record[field] = _record_value(calculated[field].iloc[index])
    return records, fields
