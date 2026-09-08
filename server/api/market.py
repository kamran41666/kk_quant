"""Historical and live market data endpoints."""
from datetime import date, datetime, timedelta
from dataclasses import replace
from copy import deepcopy
from pathlib import Path
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pyarrow.parquet as pq
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Any, Optional
from sqlalchemy.orm import Session

from quant_engine.data.api import DataAPI
from quant_engine.data.live import (
    AKShareLiveMarketDataProvider,
    FallbackLiveMarketDataProvider,
    MarketDataUnavailableError,
    MarketQuote,
    SHANGHAI_TZ,
    TencentDailyKlineProvider,
    TencentLiveMarketDataProvider,
)
from quant_engine.data.security_master import (
    SecurityMasterProvider,
    SecurityMasterUnavailableError,
)
from quant_engine.data.store import MetaDB
from quant_engine.data.calendar import TradingCalendar
from quant_engine.data.fetcher.akshare_adapter import AKShareAdapter
from quant_engine.data.global_markets import EastmoneyFundDataProvider, GoldMarketDataProvider, YahooUSMarketDataProvider
from server.models.database import get_db
from server.services.fund_nav_registry import register_fund_nav
from server.services.fund_nav_archive import (
    archive_fund_nav_dataset,
    get_fund_nav_dataset,
    list_fund_nav_datasets,
)
from quant_engine.analytics.candle_indicators import (
    CandleDataError,
    add_indicators,
    aggregate_candles,
)
from server.config import settings

router = APIRouter(prefix="/market", tags=["market"])


def _cn_today() -> date:
    return datetime.now(SHANGHAI_TZ).date()


# Tencent's public snapshot is fast and keyless for a beginner watchlist;
# AKShare remains the independent Eastmoney/Sina fallback.  Every response
# keeps its actual source and freshness metadata, and total failure is still a
# 503 rather than a fabricated quote.
_tencent_quote_provider = TencentLiveMarketDataProvider()
live_market_provider = FallbackLiveMarketDataProvider([
    _tencent_quote_provider,
    AKShareLiveMarketDataProvider(),
])
_DEFAULT_LIVE_MARKET_PROVIDER = live_market_provider
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
_OVERVIEW_CACHE_TTL_SECONDS = 30.0
_OVERVIEW_BATCH_SIZE = 100
_OVERVIEW_MAX_WORKERS = 6
_overview_cache: Optional[tuple[float, object, dict]] = None
_overview_cache_lock = threading.Lock()
_cross_market_providers = {
    "cn-fund": EastmoneyFundDataProvider(),
    "us-equity": YahooUSMarketDataProvider(),
    "gold": GoldMarketDataProvider(),
}
_cross_market_specs = (
    {"id": "a-share", "name": "A 股", "asset_type": "equity", "currency": "CNY", "source": "tencent:qt / AKShare"},
    {"id": "cn-fund", "name": "国内基金", "asset_type": "fund", "currency": "CNY", "source": "eastmoney:fund_nav"},
    {"id": "us-equity", "name": "美股", "asset_type": "equity", "currency": "USD", "source": "yahoo:chart"},
    {"id": "gold", "name": "黄金", "asset_type": "commodity", "currency": "CNY/USD", "source": "sina:gold + yahoo:chart"},
)


class IndexSnapshotRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    name: str = Field(min_length=1, max_length=80)
    weight: Optional[float] = Field(default=None, ge=0, le=100)

    @field_validator("code")
    @classmethod
    def validate_code_exchange(cls, value: str) -> str:
        return AKShareAdapter._canonical_code(value)


class IndexSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index_code: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    as_of: date
    rows: list[IndexSnapshotRow] = Field(min_length=1, max_length=2000)
    source: str = Field(default="manual:index_snapshot", min_length=1, max_length=80)
    received_at: Optional[datetime] = None


class IndexSnapshotImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshots: list[IndexSnapshotRequest] = Field(min_length=1, max_length=64)
    dry_run: bool = False


class FundNavDatasetRequest(BaseModel):
    """Request a versioned local archive of one public fund NAV history."""
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=20)
    start_date: Optional[date] = None
    end_date: Optional[date] = None

    @field_validator("end_date")
    @classmethod
    def valid_end_date(cls, value: Optional[date]) -> Optional[date]:
        if value and value > _cn_today():
            raise ValueError("end_date cannot be in the future")
        return value


@router.get("/markets/cn-fund/datasets")
def list_fund_datasets(code: Optional[str] = Query(None, min_length=1, max_length=20), limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    """List immutable local NAV datasets available to fund research."""
    try:
        return {"data": list_fund_nav_datasets(db, code=code, limit=limit), "meta": {"market": "cn-fund", "immutable": True, "research_only": True}}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/markets/cn-fund/datasets/{dataset_id}")
def get_fund_dataset(dataset_id: str, include_rows: bool = Query(False), db: Session = Depends(get_db)):
    """Read one content-addressed NAV dataset and optionally its rows."""
    try:
        return {"data": get_fund_nav_dataset(db, dataset_id, include_rows=include_rows), "meta": {"market": "cn-fund", "immutable": True, "research_only": True}}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/markets/cn-fund/datasets", status_code=201)
def archive_fund_dataset(req: FundNavDatasetRequest, db: Session = Depends(get_db)):
    """Fetch Eastmoney history once and persist a reproducible local dataset."""
    provider = _cross_market_provider("cn-fund")
    end = req.end_date or _cn_today()
    start = req.start_date or (end - timedelta(days=365))
    if start > end:
        raise HTTPException(status_code=422, detail="start_date must not be after end_date")
    try:
        rows = provider.fetch_daily(req.symbol, start, end)
        dataset = archive_fund_nav_dataset(db, code=req.symbol, rows=rows,
                                           source=provider.source_name,
                                           start_date=start, end_date=end)
    except MarketDataUnavailableError as exc:
        raise HTTPException(status_code=503, detail={"code": "FUND_NAV_HISTORY_UNAVAILABLE", "attempts": exc.attempts}) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"data": dataset, "meta": {"market": "cn-fund", "status": "ok", "immutable": True, "research_only": True}}


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
        # Index cards are research instruments, not directly tradable
        # securities.  Keep that distinction in the API contract so the
        # detail page cannot accidentally offer a paper order for an index.
        replace(by_code[code], name=name, asset_type="index", market="CN", currency="CNY")
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


@router.get("/markets")
def list_supported_markets():
    """Return market tabs and their current research-only data contracts."""
    return {
        "data": [
            {
                **spec,
                "status": "ok" if spec["id"] == "a-share" else "configured",
                "supports_quotes": True,
                "supports_candles": True,
                "supports_live_execution": False,
            }
            for spec in _cross_market_specs
        ],
        "meta": {"default": "a-share", "research_only": True},
    }


def _cross_market_provider(market: str):
    provider = _cross_market_providers.get(market)
    if provider is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UNSUPPORTED_MARKET",
                "message": "market must be cn-fund, us-equity or gold",
                "market": market,
            },
        )
    return provider


@router.get("/markets/{market}/quotes")
def get_cross_market_quotes(
    market: str,
    symbols: str = Query(..., min_length=1, description="Comma-separated market symbols"),
    db: Session = Depends(get_db),
):
    """Return quotes for a non-A-share research market."""
    provider = _cross_market_provider(market)
    raw_symbols = list(dict.fromkeys(item.strip() for item in symbols.split(",") if item.strip()))
    if not raw_symbols:
        raise HTTPException(status_code=422, detail="At least one market symbol is required")
    if len(raw_symbols) > 20:
        raise HTTPException(status_code=422, detail="At most 20 market symbols may be requested")
    try:
        quotes = provider.fetch_quotes(raw_symbols)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MarketDataUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "CROSS_MARKET_DATA_UNAVAILABLE",
                "message": "The selected public market source returned no usable quotes.",
                "market": market,
                "attempts": exc.attempts,
            },
        ) from exc
    data = [quote.to_dict() for quote in quotes]
    if market == "cn-fund":
        for quote in quotes:
            register_fund_nav(code=quote.code, price=float(quote.price), source=quote.source,
                              as_of=quote.as_of, freshness=quote.freshness,
                              received_at=quote.received_at,
                              # Direct unit calls receive FastAPI's Depends
                              # sentinel; the registry treats that as the
                              # intentionally non-durable test path.
                              db=db if isinstance(db, Session) else None)
    returned = {quote.code for quote in quotes}
    return {
        "data": data,
        "meta": {
            "market": market,
            "requested_count": len(raw_symbols),
            "returned_count": len(data),
            "missing_symbols": [symbol for symbol in raw_symbols if symbol.upper() not in returned],
            "sources": sorted({quote.source for quote in quotes}),
            "status": "ok" if len(data) == len(raw_symbols) else "partial",
            "research_only": True,
        },
    }


@router.get("/markets/{market}/candles/{symbol}")
def get_cross_market_candles(
    market: str,
    symbol: str,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    interval: str = Query("1d", pattern="^(1d|1w|1mo)$"),
    indicators: Optional[str] = Query(None),
):
    """Return shared daily/weekly/monthly chart data for fund or US symbols."""
    provider = _cross_market_provider(market)
    try:
        requested_end = date.fromisoformat(end_date) if end_date else _cn_today()
        requested_start = date.fromisoformat(start_date) if start_date else requested_end - timedelta(days=365)
        if requested_start > requested_end:
            raise ValueError("start_date must not be after end_date")
        if requested_end > _cn_today():
            raise ValueError("end_date cannot be in the future")
        daily_rows = provider.fetch_daily(symbol, requested_start, requested_end)
        candles = aggregate_candles(daily_rows, interval=interval)
        indicator_values = [item.strip().lower() for item in indicators.split(",") if item.strip()] if indicators else []
        candles, indicator_fields = add_indicators(candles, indicator_values)
    except MarketDataUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "CROSS_MARKET_HISTORICAL_DATA_UNAVAILABLE",
                "message": "No usable historical data is available for the selected symbol.",
                "market": market,
                "attempts": exc.attempts,
            },
        ) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    source_values = sorted({str(item.get("source")) for item in daily_rows if item.get("source")})
    reference_symbols = sorted({str(item.get("reference_symbol")) for item in daily_rows if item.get("reference_symbol")})
    spec = next(item for item in _cross_market_specs if item["id"] == market)
    return {
        "data": candles,
        "meta": {
            "market": market,
            "symbol": symbol.upper(),
            "interval": interval,
            "source": ",".join(source_values) if source_values else spec["source"],
            "sources": source_values,
            "start_date": requested_start.isoformat(),
            "end_date": requested_end.isoformat(),
            "returned_count": len(candles),
            "daily_source_count": len(daily_rows),
            "indicator_fields": list(indicator_fields),
            "as_of": candles[-1]["date"] if candles else None,
            "freshness": "historical",
            "status": "ok" if candles else "empty",
            "research_only": True,
            "note": (
                f"历史源不可用，显示 {', '.join(reference_symbols)} 参考走势；不代表 {symbol.upper()} 的本地报价。"
                if market == "gold" and reference_symbols
                else "基金净值按平值日线展示；不代表交易所 OHLC。" if market == "cn-fund" else "公开源低频研究数据。"
            ),
        },
    }


@router.post("/index-snapshots")
def archive_index_snapshot(request: IndexSnapshotRequest):
    """Archive a dated constituent snapshot for point-in-time backtests."""
    if request.as_of > _cn_today():
        raise HTTPException(status_code=422, detail="snapshot as_of cannot be in the future")
    try:
        archive_kwargs = {"source": request.source}
        if request.received_at:
            archive_kwargs["received_at"] = request.received_at.isoformat()
        count = DataAPI().archive_index_components(
            request.index_code, request.as_of,
            [row.model_dump() for row in request.rows], **archive_kwargs
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "index_code": request.index_code,
        "as_of": request.as_of.isoformat(),
        "stored_count": count,
        "source": request.source,
        "status": "ok",
    }


@router.post("/index-snapshots/import")
def import_index_snapshots(request: IndexSnapshotImportRequest):
    """Validate or atomically import several dated constituent snapshots.

    ``dry_run=true`` performs the same structural and point-in-time checks
    without modifying the database, which is useful before importing a file
    converted to JSON.  A non-dry run never writes the first period until all
    periods have passed validation.
    """
    payload = []
    seen_periods: set[tuple[str, date]] = set()
    for snapshot in request.snapshots:
        if snapshot.as_of > _cn_today():
            raise HTTPException(status_code=422, detail="snapshot as_of cannot be in the future")
        period = (snapshot.index_code, snapshot.as_of)
        if period in seen_periods:
            raise HTTPException(
                status_code=422,
                detail=f"duplicate index snapshot period: {snapshot.index_code} {snapshot.as_of.isoformat()}",
            )
        seen_periods.add(period)
        payload.append({
            "index_code": snapshot.index_code,
            "as_of": snapshot.as_of,
            "rows": [row.model_dump() for row in snapshot.rows],
            "source": snapshot.source,
            "received_at": snapshot.received_at.isoformat() if snapshot.received_at else None,
        })
    api = DataAPI()
    try:
        validation = api.validate_index_components_batch(payload)
        if request.dry_run:
            return {"status": "validated", "dry_run": True, "snapshots": validation,
                    "total_constituents": sum(item["constituent_count"] for item in validation)}
        total = api.archive_index_components_batch(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "ok", "dry_run": False, "snapshots": validation,
            "total_constituents": total}


@router.post("/index-snapshots/current")
def archive_current_index_snapshot(index_code: str = Query(..., pattern=r"^\d{6}\.(SH|SZ|BJ)$")):
    """Fetch and archive today's current constituent snapshot explicitly.

    It is never exposed as a historical-date fetch; callers must import dated
    historical files separately for unbiased backtests.
    """
    if index_code not in {"000300.SH", "000905.SH"}:
        raise HTTPException(status_code=422, detail="current constituent source supports 000300.SH or 000905.SH")
    adapter = AKShareAdapter()
    try:
        frame = adapter.fetch_current_index_components(index_code)
        if frame.empty:
            raise ValueError("current index source returned no constituents")
        if "weight" not in frame.columns:
            frame["weight"] = None
        count = DataAPI().archive_index_components(
            index_code,
            _cn_today(),
            frame[["code", "name", "weight"]].to_dict(orient="records"),
            source="akshare:index_components_current",
            received_at=frame["received_at"].iloc[0],
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "INDEX_SNAPSHOT_SOURCE_UNAVAILABLE", "message": str(exc)},
        ) from exc
    finally:
        adapter.close()
    return {"index_code": index_code, "as_of": _cn_today().isoformat(), "stored_count": count, "status": "ok"}


@router.get("/index-snapshots/{index_code}")
def get_index_snapshot(
    index_code: str,
    as_of: date = Query(..., description="Return the latest snapshot effective on or before this date"),
):
    """Inspect the point-in-time snapshot used by the backtest universe."""
    if not _CODE_PATTERN.fullmatch(index_code):
        raise HTTPException(status_code=422, detail="index_code must use six digits and .SH/.SZ/.BJ")
    if as_of > _cn_today():
        raise HTTPException(status_code=422, detail="snapshot as_of cannot be in the future")
    rows = DataAPI().index_snapshot(index_code, as_of)
    if not rows:
        raise HTTPException(
            status_code=404,
            detail={"code": "INDEX_SNAPSHOT_NOT_FOUND", "index_code": index_code, "as_of": as_of.isoformat()},
        )
    snapshot_as_of = rows[0]["as_of"]
    return {"data": rows, "meta": {"index_code": index_code, "as_of": snapshot_as_of, "count": len(rows)}}


@router.get("/index-snapshots/{index_code}/coverage")
def get_index_snapshot_coverage(index_code: str):
    """List dated constituent snapshots currently available for an index."""
    if not _CODE_PATTERN.fullmatch(index_code):
        raise HTTPException(status_code=422, detail="index_code must use six digits and .SH/.SZ/.BJ")
    try:
        periods = DataAPI().index_snapshot_coverage(index_code)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "data": periods,
        "meta": {
            "index_code": index_code,
            "snapshot_count": len(periods),
            "first_as_of": periods[0]["as_of"] if periods else None,
            "latest_as_of": periods[-1]["as_of"] if periods else None,
            "status": "ok" if periods else "empty",
        },
    }


def _split_query_values(value: Optional[str], label: str) -> Optional[list[str]]:
    if value is None:
        return None
    items = [item.strip() for item in value.split(",") if item.strip()]
    if not items:
        raise HTTPException(status_code=422, detail=f"{label} must contain at least one value")
    return list(dict.fromkeys(items))


@router.get("/fundamentals/{code}/coverage")
def get_fundamental_coverage(code: str):
    """Return imported fundamental periods and field coverage for a security."""
    try:
        canonical = _parse_codes(code)[0]
        periods = DataAPI().fundamental_coverage(canonical)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "data": periods,
        "meta": {
            "code": canonical,
            "period_count": len(periods),
            "status": "ok" if periods else "empty",
        },
    }


@router.get("/fundamentals/{code}")
def get_fundamentals(
    code: str,
    as_of: Optional[date] = Query(None, description="Information date; only announce_date <= as_of is eligible"),
    report_dates: Optional[str] = Query(None, description="Comma-separated report period end dates"),
    fields: Optional[str] = Query(None, description="Comma-separated canonical field names"),
):
    """Return point-in-time fundamental observations for one security.

    This endpoint is import-backed by design.  When no authorized financial
    dataset has been imported it returns an explicit empty result instead of
    querying an unverified webpage source or manufacturing current values.
    """
    try:
        canonical = _parse_codes(code)[0]
        effective_as_of = as_of or _cn_today()
        if effective_as_of > _cn_today():
            raise ValueError("fundamental as_of cannot be in the future")
        requested_reports = _split_query_values(report_dates, "report_dates")
        requested_fields = _split_query_values(fields, "fields")
        frame = DataAPI().fundamentals(
            [canonical], report_dates=requested_reports,
            fields=requested_fields, as_of=effective_as_of,
        )
    except HTTPException:
        raise
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    rows: list[dict[str, Any]] = []
    if not frame.empty:
        for index, record in frame.reset_index().iterrows():
            value = record.get("value")
            rows.append({
                "code": str(record["code"]),
                "report_date": record["report_date"].isoformat(),
                "announce_date": record["announce_date"].isoformat(),
                "field": str(record["field"]),
                "value": None if pd.isna(value) else float(value),
                "source": str(record["source"]),
                "received_at": str(record["received_at"]),
            })
    return {
        "data": rows,
        "meta": {
            "code": canonical,
            "as_of": effective_as_of.isoformat(),
            "requested_report_dates": requested_reports,
            "requested_fields": requested_fields,
            "returned_count": len(rows),
            "sources": sorted({row["source"] for row in rows}),
            "status": "ok" if rows else "empty",
            "pit_rule": "announce_date <= as_of; latest announcement per code/report_date/field",
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


@router.get("/coverage/daily")
def get_daily_coverage(
    codes: str = Query(..., min_length=1, description="Comma-separated canonical A-share codes"),
    start_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    fields: str = Query("open,high,low,close,volume"),
):
    """Return a machine-readable local daily-data coverage report.

    This endpoint is for research preflight only.  It reports missing dates
    and invalid fields instead of substituting a live quote or claiming that
    a security-master row proves historical coverage.
    """
    try:
        requested_start = date.fromisoformat(start_date)
        requested_end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "INVALID_DATE_RANGE", "message": "start_date and end_date must use YYYY-MM-DD."}) from exc
    if requested_start > requested_end:
        raise HTTPException(status_code=422, detail={"code": "INVALID_DATE_RANGE", "message": "start_date must not be after end_date."})
    if (requested_end - requested_start).days > 3660:
        raise HTTPException(status_code=422, detail={"code": "COVERAGE_RANGE_TOO_LARGE", "message": "Coverage reports support at most ten years per request."})
    try:
        requested_codes = _parse_codes(codes)
    except HTTPException:
        raise
    field_text = fields if isinstance(fields, str) else str(getattr(fields, "default", ""))
    selected_fields = list(dict.fromkeys(item.strip() for item in field_text.split(",") if item.strip()))
    if not selected_fields:
        raise HTTPException(status_code=422, detail={"code": "INVALID_FIELDS", "message": "At least one field is required."})
    report = DataAPI().daily_coverage(requested_codes, requested_start, requested_end, selected_fields)
    report["meta"] = {"research_only": True, "calendar": "TradingCalendar", "adjustment": "none"}
    return report


@router.get("/inventory/daily")
def get_daily_inventory():
    """Describe local daily Parquet inventory without claiming coverage."""
    items = DataAPI().daily_inventory()
    starts = [item["start_date"] for item in items if item["start_date"] is not None]
    ends = [item["end_date"] for item in items if item["end_date"] is not None]
    return {
        "items": items,
        "summary": {
            "security_count": len(items),
            "file_count": sum(item["file_count"] for item in items),
            "row_count": sum(item["row_count"] for item in items),
            "start_date": min(starts) if starts else None,
            "end_date": max(ends) if ends else None,
            "error_count": sum(len(item["read_errors"]) for item in items),
        },
        "source": "local:parquet",
        "coverage_verified": False,
    }


@router.get("/research-datasets")
def list_research_datasets():
    """List frozen research manifests separately from the mutable daily cache."""
    import json
    datasets, errors = [], []
    paths = sorted(Path(settings.data_dir).glob("research/*/normalized-*/manifest.json"))
    latest = {}
    for path in paths:
        group = path.parent.parent.name
        if group not in latest or path.stat().st_mtime > latest[group].stat().st_mtime:
            latest[group] = path
    for path in paths:
        try:
            manifest = json.loads(path.read_text())
            quality = manifest["quality"]
            datasets.append({
                "dataset_id": manifest["dataset_id"], "content_hash": manifest["content_hash"],
                "universe_count": manifest["universe_count"],
                "start_date": manifest["start_date"], "end_date": manifest["end_date"],
                "row_count": sum(item["rows"] for item in quality),
                "action_count": sum(item["corporate_action_count"] for item in quality),
                "missing_count": sum(item["missing_count"] for item in quality),
                "invalid_rows": sum(item["invalid_trading_rows"] for item in quality),
                "unresolved_actions": sum(len(item["unexplained_reference_adjustments"]) for item in quality),
                "source": manifest["source"], "signal_adjustment": manifest.get("signal_adjustment", "vendor_factor"),
                "latest": path == latest[path.parent.parent.name],
            })
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append({"dataset": path.parent.parent.name + "/" + path.parent.name,
                           "error": f"{type(exc).__name__}: {exc}"})
    return {"data": datasets, "errors": errors, "meta": {"research_only": True, "counts_from_archived_manifest": True}}


@router.get("/calendar/coverage")
def get_calendar_coverage(
    start_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    refresh: bool = Query(True, description="Refresh an unverified or stale local calendar from AKShare."),
):
    """Return provenance and strict coverage for the A-share trading calendar."""
    try:
        requested_start = date.fromisoformat(start_date)
        requested_end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_DATE_RANGE", "message": "start_date and end_date must use YYYY-MM-DD."},
        ) from exc
    if requested_start > requested_end:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_DATE_RANGE", "message": "start_date must not be after end_date."},
        )
    if (requested_end - requested_start).days > 3660:
        raise HTTPException(
            status_code=422,
            detail={"code": "COVERAGE_RANGE_TOO_LARGE", "message": "Calendar reports support at most ten years per request."},
        )
    calendar = TradingCalendar(start_year=requested_start.year, end_year=requested_end.year)
    report = calendar.ensure_coverage(requested_start, requested_end) if refresh else calendar.coverage_report(requested_start, requested_end)
    report["meta"] = {
        "research_only": True,
        "refresh_requested": bool(refresh),
        "fail_closed": True,
    }
    return report


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
        requested_end = date.fromisoformat(end_date) if end_date else _cn_today()
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
    global _overview_cache
    provider = live_market_provider
    if not _overview_cache_lock.acquire(blocking=False):
        # A slow public-source refresh must not make concurrent readers wait
        # when a previous snapshot exists. Return that snapshot explicitly as
        # stale; callers can retry after the single refresh completes.
        cached = _overview_cache
        if cached and cached[1] is provider:
            stale_response = deepcopy(cached[2])
            stale_response.setdefault("meta", {})["cache_state"] = "stale"
            return stale_response
        _overview_cache_lock.acquire()
    try:
        now = time.monotonic()
        if (_overview_cache and _overview_cache[1] is provider
                and now - _overview_cache[0] < _OVERVIEW_CACHE_TTL_SECONDS):
            cached_response = deepcopy(_overview_cache[2])
            cached_response.setdefault("meta", {})["cache_state"] = "fresh"
            return cached_response

        master_meta: Optional[dict[str, Any]] = None
        requested_count: Optional[int] = None
        attempts: list[dict[str, str]] = []
        collected: dict[str, MarketQuote] = {}

        if live_market_provider is _DEFAULT_LIVE_MARKET_PROVIDER:
            # Tencent requires an explicit symbol list. Use the cached security
            # master and bounded concurrent batches so breadth covers the A-share
            # universe without constructing an oversized URL or unbounded fan-out.
            try:
                securities, master_meta = security_master_provider.snapshot()
                requested = [row["code"] for row in securities]
            except SecurityMasterUnavailableError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "SECURITY_MASTER_UNAVAILABLE",
                        "message": "The security universe is unavailable for market breadth.",
                        "attempts": exc.attempts,
                    },
                ) from exc
            requested_count = len(requested)

            def fetch_batch(batch: list[str]) -> tuple[list[MarketQuote], list[dict[str, str]]]:
                try:
                    return live_market_provider.fetch_quotes(batch), []
                except MarketDataUnavailableError as exc:
                    return [], exc.attempts
                except Exception as exc:
                    return [], [{
                        "source": type(live_market_provider).__name__,
                        "error": f"{type(exc).__name__}: {exc}",
                    }]

            batches = [requested[offset:offset + _OVERVIEW_BATCH_SIZE]
                       for offset in range(0, len(requested), _OVERVIEW_BATCH_SIZE)]
            with ThreadPoolExecutor(max_workers=_OVERVIEW_MAX_WORKERS,
                                    thread_name_prefix="market-overview") as executor:
                futures = [executor.submit(fetch_batch, batch) for batch in batches]
                for future in as_completed(futures):
                    batch_quotes, batch_attempts = future.result()
                    attempts.extend(batch_attempts)
                    for quote in batch_quotes:
                        collected.setdefault(quote.code, quote)
        else:
            # Alternate providers may already expose a complete snapshot; keep
            # their simpler no-argument contract for compatibility.
            try:
                for quote in live_market_provider.fetch_quotes():
                    collected.setdefault(quote.code, quote)
            except MarketDataUnavailableError as exc:
                attempts.extend(exc.attempts)

        quotes = list(collected.values())
        if not quotes:
            error_code = "MARKET_BREADTH_EMPTY" if requested_count is None else "LIVE_MARKET_DATA_UNAVAILABLE"
            message = ("The configured live source returned no usable market rows."
                       if requested_count is None else
                       "Market overview is unavailable because all live providers failed.")
            raise HTTPException(
                status_code=503,
                detail={
                    "code": error_code,
                    "message": message,
                    "attempts": attempts,
                },
            )
        changes = [quote.change_pct for quote in quotes if quote.change_pct is not None]
        amounts = [quote.amount for quote in quotes if quote.amount is not None]
        freshness_values = {quote.freshness for quote in quotes}
        overview_freshness = (
            "fresh"
            if freshness_values and freshness_values.issubset({"fresh", "realtime"})
            else "unknown"
            if "unknown" in freshness_values
            else "stale"
            if "stale" in freshness_values
            else "delayed"
            if "delayed" in freshness_values
            else "unknown"
        )
        breadth_status = (
            "ok"
            if changes and amounts
            and len(changes) == len(quotes)
            and len(amounts) == len(quotes)
            else "partial"
        )
        response = {
            "data": {
                "quoted_count": len(quotes),
                "advancers": sum(value > 0 for value in changes) if changes else None,
                "decliners": sum(value < 0 for value in changes) if changes else None,
                "unchanged": sum(value == 0 for value in changes) if changes else None,
                "total_amount": sum(amounts) if amounts else None,
                # These are bounded, source-returned rankings.  They are not
                # inferred buy/sell counts: active rows use turnover when the
                # source supplies it, otherwise volume; gainers use the
                # source's daily percentage change.
                "top_active": [
                    quote.to_dict()
                    for quote in sorted(
                        (item for item in quotes if item.amount is not None or item.volume is not None),
                        key=lambda item: item.amount if item.amount is not None else item.volume or 0.0,
                        reverse=True,
                    )[:5]
                ],
                "top_gainers": [
                    quote.to_dict()
                    for quote in sorted(
                        (item for item in quotes if item.change_pct is not None),
                        key=lambda item: item.change_pct or 0.0,
                        reverse=True,
                    )[:5]
                ],
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
                "coverage": "security_master_requested" if requested_count is not None else "source_reported",
                "requested_count": requested_count,
                "quoted_count": len(quotes),
                "security_master_source": master_meta.get("source") if master_meta else None,
                "security_master_freshness": master_meta.get("freshness") if master_meta else None,
                "status": (breadth_status if requested_count is None or len(quotes) == requested_count
                           else "partial"),
                "freshness": overview_freshness,
                "valid_change_count": len(changes),
                "valid_amount_count": len(amounts),
                "unsupported_metrics": ["limit_up", "limit_down"],
            },
        }
        if attempts:
            response["meta"]["attempts"] = attempts[-20:]
        response["meta"]["cache_state"] = "fresh"
        _overview_cache = (time.monotonic(), provider, deepcopy(response))
        return response
    finally:
        _overview_cache_lock.release()


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
