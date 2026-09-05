"""Historical and live market data endpoints."""
from datetime import date, datetime, timedelta
from dataclasses import replace
from copy import deepcopy
from pathlib import Path
import re
import sqlite3
import threading
import time

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
from quant_engine.analytics.candle_indicators import (
    CandleDataError,
    add_indicators,
    aggregate_candles,
)
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
_CANDLE_CACHE_TTL_SECONDS = 30.0
_candle_cache: dict[tuple[str, str, str, str, str, str], tuple[float, dict]] = {}
_candle_cache_lock = threading.Lock()
_candle_key_locks: dict[tuple[str, str, str, str, str, str], threading.Lock] = {}
_QUOTE_BATCH_SIZE = 100
_MAX_QUOTE_CODES = 500


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
    adjust: str = "event_driven",
    allow_local_long: bool = False,
):
    """Get daily OHLCV data for a stock"""
    code = _parse_codes(code)[0]
    if adjust not in {"event_driven", "none"}:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UNSUPPORTED_ADJUSTMENT",
                "message": "adjust must be event_driven or none.",
            },
        )
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
    range_days = (requested_end - requested_start).days + 1
    if range_days > 1000 and not allow_local_long:
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
        adjust=adjust,
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
    if range_days > 1000:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "HISTORICAL_RANGE_TOO_LARGE",
                "message": "The local dataset does not fully cover this long request and public fallback supports at most 1000 calendar days.",
            },
        )
    if not trading_days and _range_is_weekend_only(requested_start, requested_end):
        return []
    # The bundled parquet sample is intentionally finite.  When a requested
    # market-page range is outside that local window, fetch a real recent K-line
    # set from the same Tencent source used for the live quote fallback.
    try:
        return tencent_daily_provider.fetch_daily(
            code, requested_start, requested_end,
            adjust=adjust,
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


def _candle_lock_for(key: tuple[str, str, str, str, str, str]) -> threading.Lock:
    with _candle_cache_lock:
        if len(_candle_key_locks) > 512:
            for stale_key, stale_lock in list(_candle_key_locks.items()):
                if stale_key not in _candle_cache and not stale_lock.locked():
                    _candle_key_locks.pop(stale_key, None)
                if len(_candle_key_locks) <= 256:
                    break
        lock = _candle_key_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _candle_key_locks[key] = lock
        return lock


def _build_candle_payload(
    code: str,
    requested_start: date,
    requested_end: date,
    interval: str,
    adjust: str,
    indicator_values: list[str],
) -> dict:
    daily_rows = get_daily(
        code,
        requested_start.isoformat(),
        requested_end.isoformat(),
        fields="open,high,low,close,volume",
        adjust=adjust,
        allow_local_long=True,
    )
    candles = aggregate_candles(daily_rows, interval=interval)
    candles, indicator_fields = add_indicators(candles, indicator_values)
    sources = sorted({
        str(row["source"])
        for row in daily_rows
        if row.get("source")
    })
    source = ",".join(sources) if sources else "local:parquet"
    remote_adjustments = {
        str(row.get("adjust"))
        for row in daily_rows
        if row.get("adjust")
    }
    adjust_applied = next(iter(remote_adjustments), adjust)
    warning_codes: list[str] = []
    if adjust == "event_driven" and "qfq" in remote_adjustments:
        adjust_applied = "qfq"
        warning_codes.append("ADJUSTMENT_FALLBACK")
    return {
        "data": candles,
        "meta": {
            "code": code,
            "interval": interval,
            "adjust": adjust,
            "adjust_requested": adjust,
            "adjust_applied": adjust_applied,
            "start_date": requested_start.isoformat(),
            "end_date": requested_end.isoformat(),
            "source": source,
            "sources": sources or ["local:parquet"],
            "returned_count": len(candles),
            "daily_source_count": len(daily_rows),
            "indicator_fields": list(indicator_fields),
            "indicator_definitions": {
                "ma": "SMA(5,20,60)",
                "ema": "EMA(12,26), adjust=False",
                "rsi14": "Wilder RSI(14)",
                "macd": "DIF=EMA12-EMA26; DEA=EMA9(DIF); histogram=2*(DIF-DEA)",
                "boll": "SMA20 +/- 2*std20 (ddof=0)",
                "kdj": "RSV9; K/D EMA(alpha=1/3) seeded by first valid RSV; J=3K-2D; flat window=null",
            },
            "as_of": candles[-1]["date"] if candles else None,
            "freshness": "historical",
            "warning_codes": warning_codes,
            "status": "ok" if candles else "empty",
        },
    }


@router.get("/candles/{code}")
def get_candles(
    code: str,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    interval: str = Query("1d", pattern="^(1d|1w|1mo)$"),
    adjust: str = Query("event_driven", pattern="^(event_driven|none)$"),
    indicators: Optional[str] = Query(
        None,
        description="Comma-separated groups: ma, ema, rsi14, macd, boll, kdj",
    ),
):
    """Return causal daily/weekly/monthly candles and optional indicators.

    Weekly and monthly bars are aggregated from the same validated daily
    source rows.  The last actual trading date is used for each bucket, so the
    API never invents a weekend/month-end price.
    """
    code = _parse_codes(code)[0]
    interval = interval if isinstance(interval, str) else "1d"
    adjust = adjust if isinstance(adjust, str) else "event_driven"
    start_date = start_date if isinstance(start_date, str) else None
    end_date = end_date if isinstance(end_date, str) else None
    indicator_values = (
        [item.strip().lower() for item in indicators.split(",") if item.strip()]
        if isinstance(indicators, str)
        else []
    )
    try:
        requested_end = date.fromisoformat(end_date) if end_date else date.today()
        requested_start = (
            date.fromisoformat(start_date)
            if start_date
            else requested_end - timedelta(days=365 if interval == "1d" else 900)
        )
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
    if (requested_end - requested_start).days + 1 > 1000 and interval == "1d":
        raise HTTPException(
            status_code=422,
            detail={
                "code": "HISTORICAL_RANGE_TOO_LARGE",
                "message": "Daily candle requests support at most 1000 calendar days.",
            },
        )
    indicator_key = ",".join(sorted(set(indicator_values)))
    cache_key = (
        code, requested_start.isoformat(), requested_end.isoformat(),
        interval, adjust, indicator_key,
    )
    now_monotonic = time.monotonic()
    with _candle_cache_lock:
        cached = _candle_cache.get(cache_key)
        if cached is not None:
            cached_at, cached_payload = cached
            age = now_monotonic - cached_at
            if age < _CANDLE_CACHE_TTL_SECONDS:
                result = deepcopy(cached_payload)
                result["meta"]["cache"] = "memory"
                result["meta"]["cache_age_seconds"] = round(max(0.0, age), 3)
                return result
    # Serialize one request per cache key. This prevents simultaneous refreshes
    # of the same security/interval from multiplying public-source traffic.
    with _candle_lock_for(cache_key):
        with _candle_cache_lock:
            cached = _candle_cache.get(cache_key)
            if cached is not None:
                cached_at, cached_payload = cached
                age = time.monotonic() - cached_at
                if age < _CANDLE_CACHE_TTL_SECONDS:
                    result = deepcopy(cached_payload)
                    result["meta"]["cache"] = "memory"
                    result["meta"]["cache_age_seconds"] = round(max(0.0, age), 3)
                    return result
        try:
            payload = _build_candle_payload(
                code, requested_start, requested_end, interval, adjust, indicator_values,
            )
        except CandleDataError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_CANDLE_REQUEST", "message": str(exc)},
            ) from exc
        except HTTPException:
            raise
        except Exception as exc:
            # Treat provider/storage failures as an explicit unavailable
            # boundary; never turn an exception into a fabricated candle set.
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "CANDLE_DATA_UNAVAILABLE",
                    "message": "No usable daily data is available for this candle request.",
                    "error_type": type(exc).__name__,
                },
            ) from exc
        with _candle_cache_lock:
            _candle_cache[cache_key] = (time.monotonic(), deepcopy(payload))
            # Keep this bounded even if a user explores many securities/ranges.
            if len(_candle_cache) > 256:
                oldest = min(_candle_cache, key=lambda key: _candle_cache[key][0])
                _candle_cache.pop(oldest, None)
        return payload


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
    collected: dict[str, Any] = {}
    attempts: list[dict[str, str]] = []
    # Public quote endpoints encode every symbol in one request.  Keep each
    # request bounded so a full-market page cannot exceed provider URL/row
    # limits, while still allowing the response to be partial when one shard
    # is unavailable.
    for offset in range(0, len(requested), _QUOTE_BATCH_SIZE):
        batch = requested[offset:offset + _QUOTE_BATCH_SIZE]
        try:
            quotes = live_market_provider.fetch_quotes(batch)
        except MarketDataUnavailableError as exc:
            attempts.extend(exc.attempts)
            continue
        except Exception as exc:
            attempts.append({
                "source": type(live_market_provider).__name__,
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue
        for quote in quotes:
            collected.setdefault(quote.code, quote)

    if not collected:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "LIVE_MARKET_DATA_UNAVAILABLE",
                "message": "No configured live market data provider returned usable quotes.",
                "attempts": attempts,
            },
        )

    data = [collected[code].to_dict() for code in requested if code in collected]
    missing = [code for code in requested if code not in collected]
    sources = sorted({item["source"] for item in data})
    response = {
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
    if attempts:
        response["meta"]["attempts"] = attempts
    return response


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
    if len(codes) > _MAX_QUOTE_CODES:
        raise HTTPException(status_code=422, detail=f"At most {_MAX_QUOTE_CODES} stock codes may be requested")
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
    # A bundled calendar can itself end before the requested range. Do not let
    # that truncated calendar make a partial local price file look complete.
    first_required = start
    if bounds and bounds[0] <= start + timedelta(days=7):
        first_required = bounds[0]
    last_required = end
    if bounds and bounds[-1] >= end - timedelta(days=7):
        last_required = bounds[-1]
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
