"""Market data endpoints — thin wrapper around DataAPI"""
from datetime import date
from fastapi import APIRouter, Query
from typing import Optional
from quant_engine.data.api import DataAPI

router = APIRouter(prefix="/market", tags=["market"])


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
    api = DataAPI()
    df = api.daily(
        codes=[code],
        start=date.fromisoformat(start_date),
        end=date.fromisoformat(end_date),
        fields=fields.split(","),
        adjust="event_driven",
    )
    # MultiIndex (code, date) -> reset to columns
    result = df.reset_index()
    # Convert Timestamp to string for JSON
    for col in result.columns:
        if hasattr(result[col], 'dt'):
            result[col] = result[col].astype(str)
    return result.to_dict(orient="records")


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
