"""Historical and live market data endpoints."""
from datetime import date, datetime, timedelta
from dataclasses import replace
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
from quant_engine.data.security_master import (
    SecurityMasterProvider,
    SecurityMasterUnavailableError,
)
from quant_engine.data.store import MetaDB
from server.config import settings

router = APIRouter(prefix="/market", tags=["market"])
# Tencent's public snapshot is fast and keyless for a beginner watchlist;
# AKShare remains the independent Eastmoney/Sina fallback.  Every response
# keeps its actual source and freshness metadata, and total failure is still a
# 503 rather than a fabricated quote.
_tencent_quote_provider = TencentLiveMarketDataProvider()
live_market_provider = FallbackLiveMarketDataProvider([
    _tencent_quote_provider,
    AKShareLiveMarketDataProvider(),
])
index_market_provider = _tencent_quote_provider
security_master_provider = SecurityMasterProvider(
    db=MetaDB(db_path=str(Path(settings.data_dir) / "meta.db"))
)
tencent_daily_provider = TencentDailyKlineProvider()
_CODE_PATTERN = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
_INDEX_SPECS = (
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
    ("000300.SH", "沪深300"),
    ("000016.SH", "上证50"),
    ("000905.SH", "中证500"),
)


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


@router.get("/universe")
def list_universe(
    search: Optional[str] = Query(None, description="Search by code or name"),
    exchange: Optional[str] = Query(None, pattern="^(SH|SZ|BJ)$"),
    board: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    refresh: bool = Query(False, description="Force a security-master refresh"),
):
    """Return the searchable, source-labelled A-share security universe."""
    # Direct unit callers do not receive FastAPI's Query defaults.
    search = search if isinstance(search, str) else None
    exchange = exchange if isinstance(exchange, str) else None
    board = board if isinstance(board, str) else None
    page = page if isinstance(page, int) and not isinstance(page, bool) else 1
    page_size = page_size if isinstance(page_size, int) and not isinstance(page_size, bool) else 50
    refresh = refresh if isinstance(refresh, bool) else False
    try:
        rows, source_meta = security_master_provider.snapshot(force=refresh)
    except SecurityMasterUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "SECURITY_MASTER_UNAVAILABLE",
                "message": "No security master source or local cache is available.",
                "attempts": exc.attempts,
            },
        ) from exc

    needle = search.strip().lower() if search else None
    filtered = [
        row for row in rows
        if (not needle or needle in row["code"].lower() or needle in row["name"].lower())
        and (not exchange or row["exchange"] == exchange)
        and (not board or row["board"] == board)
    ]
    total = len(filtered)
    offset = (page - 1) * page_size
    return {
        "data": filtered[offset:offset + page_size],
        "meta": {
            **source_meta,
            "total_count": total,
            "page": page,
            "page_size": page_size,
            "returned_count": len(filtered[offset:offset + page_size]),
        },
    }


@router.get("/indexes")
def list_indexes():
    """Return the major A-share index snapshot cards."""
    codes = [code for code, _ in _INDEX_SPECS]
    try:
        quotes = index_market_provider.fetch_quotes(codes)
    except MarketDataUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "INDEX_MARKET_DATA_UNAVAILABLE",
                "message": "Index snapshots are unavailable from the configured public source.",
                "attempts": exc.attempts,
            },
        ) from exc
    by_code = {quote.code: quote for quote in quotes}
    data = [
        replace(by_code[code], name=name)
        for code, name in _INDEX_SPECS
        if code in by_code
    ]
    return {
        "data": [item.to_dict() for item in data],
        "meta": {
            "requested_count": len(codes),
            "returned_count": len(data),
            "missing_codes": [code for code in codes if code not in by_code],
            "sources": sorted({item.source for item in data}),
            "status": "ok" if len(data) == len(codes) else "partial",
        },
    }


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
    if (requested_end - requested_start).days + 1 > 1000:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "HISTORICAL_RANGE_TOO_LARGE",
                "message": "Remote historical fallback supports at most 1000 calendar days.",
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

    if not quotes:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "MARKET_BREADTH_EMPTY",
                "message": "The configured live source returned no usable market rows.",
            },
        )
    changes = [quote.change_pct for quote in quotes if quote.change_pct is not None]
    amounts = [quote.amount for quote in quotes if quote.amount is not None]
    breadth_status = "ok" if changes and amounts else "partial"
    return {
        "data": {
            "quoted_count": len(quotes),
            "advancers": sum(value > 0 for value in changes) if changes else None,
            "decliners": sum(value < 0 for value in changes) if changes else None,
            "unchanged": sum(value == 0 for value in changes) if changes else None,
            "total_amount": sum(amounts) if amounts else None,
            # Limit-up/down requires a source-specific price-limit rule and is
            # intentionally left explicit rather than inferred from missing
            # fields.
            "limit_up": None,
            "limit_down": None,
        },
        "meta": {
            "sources": sorted({quote.source for quote in quotes}),
            "fallback_used": any(quote.is_fallback for quote in quotes),
            "received_at": max(quote.received_at for quote in quotes),
            # This is the provider's returned universe.  It is intentionally
            # not described as a complete exchange census when a source only
            # returns a partial snapshot.
            "coverage": "source_reported",
            "status": breadth_status,
            "valid_change_count": len(changes),
            "valid_amount_count": len(amounts),
            "unsupported_metrics": ["limit_up", "limit_down"],
        },
    }


@router.get("/breadth")
def get_market_breadth():
    """Alias exposing the market breadth card under an explicit resource name."""
    return get_market_overview()


@router.get("/health")
def get_market_health():
    """Report observed upstream state and read-only local data coverage."""
    providers = live_market_provider.health()
    security_master = security_master_provider.health()
    statuses = {item["status"] for item in providers}
    if providers and providers[0]["status"] == "ok":
        status = "ok"
    elif "ok" in statuses:
        status = "degraded"
    elif statuses == {"unknown"}:
        status = "unknown"
    else:
        status = "unavailable"
    if security_master.get("status") == "unavailable" and status == "ok":
        status = "degraded"
    return {
        "status": status,
        "providers": providers,
        "security_master": security_master,
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
