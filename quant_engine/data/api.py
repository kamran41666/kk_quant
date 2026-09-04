"""DataAPI — 统一数据查询接口

单例模式。上层代码只通过这个类访问数据，物理存储细节对外透明。
"""
from datetime import date
from typing import Optional

import pandas as pd

from quant_engine.data.calendar import TradingCalendar
from quant_engine.data.adjust import AdjustHandler
from quant_engine.data.store import PriceStore, MetaDB


class DataAPI:
    _instance: Optional["DataAPI"] = None

    def __new__(cls) -> "DataAPI":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._calendar = TradingCalendar()
        self._adjust = AdjustHandler(self._calendar)
        self._price_store = PriceStore()
        self._meta = MetaDB()
        self._initialized = True

    def get_trading_dates(self, start: date, end: date) -> list[date]:
        return self._calendar.get_trading_days(start, end)

    def is_trading_day(self, d: date) -> bool:
        return self._calendar.is_trading_day(d)

    def daily(
        self,
        codes: list[str],
        start: date,
        end: date,
        fields: Optional[list[str]] = None,
        adjust: str = "event_driven",
    ) -> pd.DataFrame:
        if fields is None:
            fields = ["open", "high", "low", "close", "volume"]

        df = self._price_store.read_range(codes, start, end, fields)

        if adjust == "none":
            return df
        if adjust == "event_driven":
            return self._apply_event_driven_adjust(df, fields)
        raise ValueError(f"Unknown adjust method: {adjust}")

    def _apply_event_driven_adjust(
        self, df: pd.DataFrame, fields: list[str]
    ) -> pd.DataFrame:
        price_fields = {"open", "high", "low", "close"} & set(fields)
        if not price_fields:
            return df

        result = df.copy()
        if result.empty:
            return result

        # Compute one aligned factor vector per instrument.  Avoid scalar .loc
        # writes, which are quadratic enough to make multi-year runs unusable.
        for code in result.index.get_level_values("code").unique():
            mask = result.index.get_level_values("code") == code
            dates = result.index.get_level_values("date")[mask]
            factors = self._adjust.factors_for_dates(code, dates).to_numpy()
            result.loc[mask, list(price_fields)] = (
                result.loc[mask, list(price_fields)].to_numpy() * factors[:, None]
            )
        return result

    def stock_list(self) -> list[dict]:
        stocks = self._meta.get_all_stocks()
        return [
            {
                "code": s.code, "name": s.name,
                "exchange": s.exchange, "board": s.board,
                "listed_date": s.listed_date,
            }
            for s in stocks
        ]

    def index_components(self, index_code: str, dt: date) -> list[str]:
        from quant_engine.data.fetcher.akshare_adapter import AKShareAdapter
        adapter = AKShareAdapter()
        df = adapter.fetch_index_components(index_code, dt)
        if "code" in df.columns:
            return df["code"].tolist()
        return []

    def industry(self, code: str, dt: date) -> Optional[str]:
        from pathlib import Path
        path = Path("data/raw/industry/sw_industry.parquet")
        if not path.exists():
            return None
        df = pd.read_parquet(path)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        match = df[(df["code"] == code) & (df["date"] <= dt)]
        if match.empty:
            return None
        return match.sort_values("date").iloc[-1]["level1"]

    def fundamentals(
        self,
        codes: list[str],
        report_dates: list[str],
        fields: list[str],
    ) -> pd.DataFrame:
        raise NotImplementedError("Fundamentals fetching not yet implemented")
