"""交易日历模块

A 股交易日历基于中国金融期货交易所发布的交易日历。
使用 AKShare 获取官方日历数据，本地缓存到 Parquet。
"""
import warnings
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd


class TradingCalendar:
    """A 股交易日历"""

    def __init__(self, start_year: int = 2010, end_year: Optional[int] = None):
        self._start_year = start_year
        self._end_year = end_year or datetime.now().year + 1
        self._trading_dates: set[date] = set()
        self._sorted_dates: list[date] = []
        self._load()

    def _data_path(self) -> Path:
        configured_root = os.getenv("QUANT_DATA_DIR")
        base = (Path(configured_root) if configured_root else Path(__file__).parent.parent.parent / "data") / "raw" / "calendar"
        base.mkdir(parents=True, exist_ok=True)
        return base / "trading_dates.parquet"

    def _load(self):
        path = self._data_path()
        if path.exists():
            df = pd.read_parquet(path)
            df["date"] = pd.to_datetime(df["date"]).dt.date
        else:
            df = self._fetch_from_akshare()
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(path, index=False)

        mask = (df["date"] >= date(self._start_year, 1, 1)) & (
               df["date"] <= date(self._end_year, 12, 31))
        trading = df[mask & df["is_trading_day"]]
        self._trading_dates = set(trading["date"].tolist())
        self._sorted_dates = sorted(self._trading_dates)

    def _fetch_from_akshare(self) -> pd.DataFrame:
        import akshare as ak
        try:
            df = ak.tool_trade_date_hist_sina()
            df = df.rename(columns={"trade_date": "date"})
            df["date"] = pd.to_datetime(df["date"]).dt.date
            df["is_trading_day"] = True
            df["exchange"] = "SSE"
            return df[["date", "is_trading_day", "exchange"]]
        except Exception:
            warnings.warn(
                "AKShare 交易日历获取失败，回退到工作日历（不含节假日修正），"
                "请尽快手动更新交易日历"
            )
            dates = pd.date_range(
                f"{self._start_year}-01-01", f"{self._end_year}-12-31", freq="B"
            )
            return pd.DataFrame({
                "date": dates.date,
                "is_trading_day": True,
                "exchange": "SSE",
            })

    def is_trading_day(self, d: date) -> bool:
        return d in self._trading_dates

    def get_trading_days(self, start: date, end: date) -> list[date]:
        return [d for d in self._sorted_dates if start <= d <= end]

    def next_trading_day(self, d: date) -> date:
        for td in self._sorted_dates:
            if td > d:
                return td
        raise ValueError(f"No trading day after {d}")

    def prev_trading_day(self, d: date) -> date:
        prev = None
        for td in self._sorted_dates:
            if td >= d:
                break
            prev = td
        if prev is None:
            raise ValueError(f"No trading day before {d}")
        return prev

    def count_trading_days(self, start: date, end: date) -> int:
        return len(self.get_trading_days(start, end))

    def nth_trading_day_of_month(self, year: int, month: int, n: int) -> date:
        days = [d for d in self._sorted_dates
                if d.year == year and d.month == month]
        if n == -1:
            return days[-1]
        return days[n - 1]

    @property
    def all_dates(self) -> list[date]:
        return list(self._sorted_dates)
