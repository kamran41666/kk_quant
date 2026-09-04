"""事件驱动复权处理器

存储原始价格 + 分红送转事件，回测时按 Point-in-Time 应用。
仅应用除权除息日 ≤ 查询日期的事件，杜绝前视偏差。
"""
import warnings
from datetime import date
from typing import Optional

import pandas as pd

from quant_engine.data.calendar import TradingCalendar


class AdjustHandler:
    """事件驱动复权

    原则:
    - 存储原始（未复权）价格
    - 独立存储复权因子时间序列
    - 查询时只应用 ex_date <= query_date 的事件
    - 累积复权因子 = 所有适用事件 factor 的乘积
    """

    def __init__(self, calendar: TradingCalendar):
        self._calendar = calendar
        self._factor_cache: dict[str, pd.DataFrame] = {}

    def get_adjusted_price(
        self, code: str, dt: date, field: str = "close"
    ) -> float:
        """获取截至 dt 日期的复权价格"""
        raw = self._read_raw_price(code, dt, field)
        factor = self._get_adjust_factor_up_to(code, dt)
        return raw * factor

    def _get_adjust_factor_up_to(self, code: str, dt: date) -> float:
        """计算截至 dt 的累积复权因子 (Point-in-Time: 只看 ex_date <= dt)"""
        tbl = self._read_adjust_table(code)
        if tbl.empty:
            return 1.0
        applicable = tbl[tbl["date"] <= dt]
        if applicable.empty:
            return 1.0
        return float(applicable["factor"].prod())

    def factors_for_dates(
        self, code: str, dates: pd.Index | list[date]
    ) -> pd.Series:
        """Return cumulative point-in-time factors for many dates.

        The previous DataAPI implementation performed a DataFrame scalar write for
        every price cell.  Besides being slow, that made a normal multi-year demo
        appear to hang.  This method computes one vector per instrument while still
        applying only events whose ex-date is not later than the price date.
        """
        query_dates = pd.Index([
            value.date() if hasattr(value, "date") else value
            for value in dates
        ])
        table = self._read_adjust_table(code)
        if table.empty:
            return pd.Series(1.0, index=query_dates, dtype="float64")

        events = table[["date", "factor"]].copy().sort_values("date")
        events["cumulative_factor"] = events["factor"].astype(float).cumprod()
        lookup = pd.Series(
            events["cumulative_factor"].to_numpy(),
            index=pd.Index(events["date"]),
        )
        union = lookup.index.union(query_dates).sort_values()
        expanded = lookup.reindex(union).ffill().fillna(1.0)
        return expanded.reindex(query_dates).astype("float64")

    def _read_raw_price(
        self, code: str, dt: date, field: str
    ) -> float:
        """读取原始价格（从 Parquet 存储层）"""
        from quant_engine.data.store import PriceStore
        store = PriceStore()
        try:
            return store.read(code, dt, field)
        except KeyError:
            raise KeyError(
                f"Cannot read raw price for {code} on {dt}"
            ) from None

    def _read_adjust_table(self, code: str) -> pd.DataFrame:
        """读取某只股票的复权因子表 (带缓存)"""
        if code not in self._factor_cache:
            from quant_engine.data.store import AdjustStore
            store = AdjustStore()
            tbl = store.read_adjust_factors(code)
            if tbl.empty:
                self._factor_cache[code] = pd.DataFrame(
                    columns=["date", "factor"]
                ).astype({"factor": "float64"})
            else:
                self._factor_cache[code] = tbl
        return self._factor_cache[code]

    def is_ex_date(self, code: str, dt: date) -> bool:
        """判断 dt 是否为该股票的除权除息日"""
        tbl = self._read_adjust_table(code)
        if tbl.empty:
            return False
        return dt in tbl["date"].values

    def load_splits(self, code: str) -> list[dict]:
        """获取某只股票的送转事件列表"""
        from quant_engine.data.store import AdjustStore
        store = AdjustStore()
        df = store.read_splits(code)
        return df.to_dict("records") if len(df) > 0 else []

    def load_dividends(self, code: str) -> list[dict]:
        """获取某只股票的分红事件列表"""
        from quant_engine.data.store import AdjustStore
        store = AdjustStore()
        df = store.read_dividends(code)
        return df.to_dict("records") if len(df) > 0 else []
