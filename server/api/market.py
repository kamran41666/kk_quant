"""Historical and live market data endpoints."""
from datetime import date, datetime, timedelta
from pathlib import Path
import re
import sqlite3

import pyarrow.parquet as pq
import pandas as pd
from fastapi import APIRouter, HTTPException, Query
from typing import Optional

from quant_engine.data.api import DataAPI
from quant_engine.data.live import (
    AKShareLiveMarketDataProvider,
    FallbackLiveMarketDataProvider,
    MarketDataUnavailableError,
    TencentDailyKlineProvider,
    TencentLiveMarketDataProvider,
)
from server.config import settings

router = APIRouter(prefix="/market", tags=["market"])
# Tencent's public snapshot is fast and keyless for a beginner watchlist;
# AKShare remains the independent Eastmoney/Sina fallback.  Every response
# keeps its actual source and freshness metadata, and total failure is still a
# 503 rather than a fabricated quote.
live_market_provider = FallbackLiveMarketDataProvider([
    TencentLiveMarketDataProvider(),
    AKShareLiveMarketDataProvider(),
])
tencent_daily_provider = TencentDailyKlineProvider()
_CODE_PATTERN = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


@router.get("/stocks")
def list_stocks(
    search: Optional[str] = Query(None, description="Search by code or name"),
    limit: int = Query(100, ge=1, le=500),
):
    """Search stock list"""
    api = DataAPI()
    stocks = api.stock_list()
    if search:
        search_lower = search.lower()
        stocks = [
            s for s in stocks
            if search_lower in s.get("code", "").lower()
            or search_lower in s.get("name", "").lower()
        ]
    return stocks[:limit]


@router.get("/daily/{code}")
def get_daily(
    code: str,
    start_date: str,
    end_date: str,
    fields: str = "close",
):
    """Get daily OHLCV data for a stock"""
    code = _parse_codes(code)[0]
    try:
        requested_start = date.fromisoformat(start_date)
        requested_end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_DATE_RANGE",
                "message": "start_date and end_date must use YYYY-MM-DD.",
            },
        ) from exc
    if requested_start > requested_end:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_DATE_RANGE",
                "message": "start_date must not be after end_date.",
            },
        )
    api = DataAPI()
    df = api.daily(
        codes=[code],
        start=requested_start,
        end=requested_end,
        fields=fields.split(","),
        adjust="event_driven",
    )
    # MultiIndex (code, date) -> reset to columns
    result = df.reset_index()
    # Convert Timestamp to string for JSON
    for col in result.columns:
        if col == "date" or hasattr(result[col], 'dt'):
            result[col] = result[col].astype(str)
    trading_days = _requested_trading_days(api, requested_start, requested_end)
    if not result.empty and _daily_result_covers(
        result, requested_start, requested_end, trading_days
    ):
        return result.to_dict(orient="records")
    if not trading_days and _range_is_weekend_only(requested_start, requested_end):
        return []
    if (requested_end - requested_start).days + 1 > 1000:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "HISTORICAL_RANGE_TOO_LARGE",
                "message": "Remote historical fallback supports at most 1000 calendar days.",
            },
        )
    # The bundled parquet sample is intentionally finite.  When a requested
    # market-page range is outside that local window, fetch a real recent K-line
    # set from the same Tencent source used for the live quote fallback.
    try:
        return tencent_daily_provider.fetch_daily(
            code, requested_start, requested_end,
            adjust="event_driven",
        )
    except MarketDataUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "HISTORICAL_MARKET_DATA_UNAVAILABLE",
                "message": "No local or live historical market data is available.",
                "attempts": exc.attempts,
            },
        ) from exc


@router.get("/calendar")
def get_calendar(
    start_date: str,
    end_date: str,
):
    """Get trading calendar"""
    api = DataAPI()
    trading_days = api.get_trading_dates(
        date.fromisoformat(start_date),
        date.fromisoformat(end_date),
    )
    return [d.isoformat() for d in trading_days]


@router.get("/quotes")
def get_quotes(
    codes: str = Query(
        ...,
        min_length=1,
        description="Comma-separated canonical codes, e.g. 000001.SZ,600000.SH",
    ),
):
    """Return current quotes with source and freshness metadata.

    No historical close, cached sample, or generated value is substituted when
    both live upstreams fail.
    """
    requested = _parse_codes(codes)
    try:
        quotes = live_market_provider.fetch_quotes(requested)
    except MarketDataUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "LIVE_MARKET_DATA_UNAVAILABLE",
                "message": "No configured live market data provider returned usable quotes.",
                "attempts": exc.attempts,
            },
        ) from exc

    by_code = {quote.code: quote for quote in quotes}
    data = [by_code[code].to_dict() for code in requested if code in by_code]
    missing = [code for code in requested if code not in by_code]
    sources = sorted({item["source"] for item in data})
    return {
        "data": data,
        "meta": {
            "requested_count": len(requested),
            "returned_count": len(data),
            "missing_codes": missing,
            "sources": sources,
            "fallback_used": any(item["is_fallback"] for item in data),
            "status": "ok" if not missing else "partial",
        },
    }


@router.get("/overview")
def get_market_overview():
    """Return a source-labelled breadth summary of the latest quote snapshot."""
    try:
        quotes = live_market_provider.fetch_quotes()
    except MarketDataUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "LIVE_MARKET_DATA_UNAVAILABLE",
                "message": "Market overview is unavailable because all live providers failed.",
                "attempts": exc.attempts,
            },
        ) from exc

    changes = [quote.change_pct for quote in quotes if quote.change_pct is not None]
    amounts = [quote.amount for quote in quotes if quote.amount is not None]
    return {
        "data": {
            "quoted_count": len(quotes),
            "advancers": sum(value > 0 for value in changes),
            "decliners": sum(value < 0 for value in changes),
            "unchanged": sum(value == 0 for value in changes),
            "total_amount": sum(amounts),
        },
        "meta": {
            "sources": sorted({quote.source for quote in quotes}),
            "fallback_used": any(quote.is_fallback for quote in quotes),
            "received_at": max(quote.received_at for quote in quotes),
        },
    }


@router.get("/health")
def get_market_health():
    """Report observed upstream state and read-only local data coverage."""
    providers = live_market_provider.health()
    statuses = {item["status"] for item in providers}
    if providers and providers[0]["status"] == "ok":
        status = "ok"
    elif "ok" in statuses:
        status = "degraded"
    elif statuses == {"unknown"}:
        status = "unknown"
    else:
        status = "unavailable"
    return {
        "status": status,
        "providers": providers,
        "latest_local_date": _latest_local_market_date(Path(settings.data_dir)),
        "stock_count": _local_stock_count(Path(settings.data_dir)),
    }


def _parse_codes(value: str) -> list[str]:
    codes = list(dict.fromkeys(part.strip().upper() for part in value.split(",") if part.strip()))
    if not codes:
        raise HTTPException(status_code=422, detail="At least one stock code is required")
    if len(codes) > 100:
        raise HTTPException(status_code=422, detail="At most 100 stock codes may be requested")
    invalid = [code for code in codes if not _CODE_PATTERN.fullmatch(code)]
    if invalid:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_STOCK_CODE",
                "message": "Codes must use six digits followed by .SH, .SZ, or .BJ",
                "invalid_codes": invalid,
            },
        )
    return codes


def _requested_trading_days(api, start: date, end: date) -> list[date]:
    getter = getattr(api, "get_trading_dates", None)
    if getter is None:
        return []
    try:
        return list(getter(start, end))
    except Exception:
        return []


def _range_is_weekend_only(start: date, end: date) -> bool:
    current = start
    while current <= end:
        if current.weekday() < 5:
            return False
        current += timedelta(days=1)
    return True


def _daily_result_covers(
    result, start: date, end: date, trading_days: Optional[list[date]] = None
) -> bool:
    """Return whether a local daily result spans the requested date bounds."""
    if result.empty or "date" not in result.columns:
        return False
    values = result["date"].dropna()
    if values.empty:
        return False
    dates = pd.to_datetime(values, errors="coerce").dropna()
    if dates.empty:
        return False
    bounds = trading_days or []
    first_required = bounds[0] if bounds else start
    last_required = bounds[-1] if bounds else end
    return dates.min().date() <= first_required and dates.max().date() >= last_required


def _latest_local_market_date(data_dir: Path) -> Optional[str]:
    daily_dir = data_dir / "raw" / "daily"
    if not daily_dir.exists():
        return None
    candidates = sorted(daily_dir.glob("year=*/quarter=*/*.parquet"), reverse=True)
    latest: Optional[datetime] = None
    for path in candidates:
        try:
            # Read the physical file directly so Hive-style directory names
            # (year=/quarter=) cannot introduce conflicting partition columns.
            table = pq.ParquetFile(path).read(columns=["date"])
            values = table.column("date").to_pylist()
            for value in values:
                stamp = datetime.combine(value, datetime.min.time()) if isinstance(value, date) and not isinstance(value, datetime) else value
                if isinstance(stamp, datetime) and (latest is None or stamp > latest):
                    latest = stamp
        except (OSError, KeyError, ValueError):
            continue
    return latest.date().isoformat() if latest else None


def _local_stock_count(data_dir: Path) -> int:
    db_path = (data_dir / "meta.db").resolve()
    if not db_path.exists():
        return 0
    try:
        with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT COUNT(*) FROM stock_info").fetchone()
            return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0
