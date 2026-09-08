"""DataHandler — Point-in-Time 数据供给

回测引擎的数据来源。每次 push_day() 推进一个交易日，
只向策略暴露当前日及之前的数据，杜绝前视偏差。
"""
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

from quant_engine.data.api import DataAPI


class DataHandler:
    """Point-in-Time 数据供给器

    回测引擎通过 DataHandler 访问行情数据。
    数据按交易日逐步释放，策略只能看到"当前已知"的信息。
    """

    def __init__(self, codes: list[str], start: date, end: date, calendar=None):
        self._codes = list(codes)
        self._start = start
        self._end = end
        self._api = DataAPI()

        # Ask the storage layer for an auditable report before loading the
        # frame.  DataAPI implementations used by deterministic unit tests
        # may not expose this optional report; the production implementation
        # always returns a dict and the engine applies the strict gate.
        self._coverage_report = None
        coverage = getattr(self._api, "daily_coverage", None)
        if callable(coverage):
            try:
                self._coverage_report = coverage(
                    codes=self._codes,
                    start=start,
                    end=end,
                    # OHLCV is the minimum deterministic execution surface;
                    # optional limit/suspension columns are still loaded when
                    # present but are not allowed to make an otherwise valid
                    # historical dataset look incomplete.
                    fields=["open", "high", "low", "close", "volume"],
                    adjust="event_driven",
                    calendar=calendar,
                )
            except Exception as exc:
                # Preserve the error as evidence so the engine can fail closed
                # with an actionable message instead of silently proceeding.
                self._coverage_report = {
                    "coverage_version": "daily-coverage-v1",
                    "market": "a-share",
                    "source": "local:parquet",
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "requested_codes": list(self._codes),
                    "complete": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
        self._load_error: Optional[str] = None

        # 预加载整个区间数据到内存
        try:
            self._daily_data = self._api.daily(
                codes=self._codes,
                start=start,
                end=end,
                fields=["open", "high", "low", "close", "volume", "amount",
                        "turnover_rate"],
                adjust="event_driven",
            )
        except Exception as exc:
            self._daily_data = pd.DataFrame(
                columns=["open", "high", "low", "close", "volume", "amount",
                         "turnover_rate", "up_limit", "down_limit", "is_suspended"],
                index=pd.MultiIndex.from_arrays([[], []], names=["code", "date"]),
            )
            self._load_error = f"{type(exc).__name__}: {exc}"

        # Normalize date level to datetime.date (Parquet round-trips produce Timestamps)
        if not self._daily_data.empty:
            code_level = self._daily_data.index.get_level_values(0)
            date_level = self._daily_data.index.get_level_values(1)
            date_level = pd.to_datetime(date_level).date
            self._daily_data.index = pd.MultiIndex.from_arrays(
                [code_level, date_level], names=["code", "date"]
            )

        self._current_date: Optional[date] = None

    @property
    def current_date(self) -> Optional[date]:
        return self._current_date

    @property
    def coverage_report(self) -> Optional[dict]:
        """Machine-readable local data coverage evidence, when available."""
        return self._coverage_report

    @property
    def load_error(self) -> Optional[str]:
        """Error raised while loading the local frame, if any."""
        return self._load_error

    @property
    def stock_list(self) -> list[str]:
        return list(self._codes)

    def requirement_coverage(self, fields: list[str]) -> dict:
        """Report usable finite bars per code for a strategy data requirement."""
        missing_fields = sorted(set(fields) - set(self._daily_data.columns))
        if missing_fields:
            return {
                "missing_fields": missing_fields,
                "usable_bars": {code: 0 for code in self._codes},
            }
        selected = self._daily_data[fields].apply(pd.to_numeric, errors="coerce")
        usable_mask = pd.Series(
            np.isfinite(selected.to_numpy()).all(axis=1),
            index=selected.index,
        )
        counts = usable_mask.groupby(level="code").sum()
        return {
            "missing_fields": [],
            "usable_bars": {code: int(counts.get(code, 0)) for code in self._codes},
        }

    @property
    def available_start(self) -> Optional[date]:
        """Earliest date actually present in the preloaded local dataset."""
        if self._daily_data.empty:
            return None
        values = [value for value in pd.to_datetime(self._daily_data.index.get_level_values("date")).date
                  if self._start <= value <= self._end]
        if not values:
            return None
        return min(values)

    @property
    def available_end(self) -> Optional[date]:
        """Latest date actually present in the preloaded local dataset."""
        if self._daily_data.empty:
            return None
        values = [value for value in pd.to_datetime(self._daily_data.index.get_level_values("date")).date
                  if self._start <= value <= self._end]
        if not values:
            return None
        return max(values)

    def push_day(self, dt: date):
        """推进到指定交易日"""
        self._current_date = dt

    def get_price(self, code: str, field: str = 'close') -> float:
        """获取当前交易日的单个股票价格"""
        if self._current_date is None:
            raise RuntimeError("DataHandler not initialized: call push_day() first")

        try:
            return float(
                self._daily_data.loc[(code, self._current_date), field]
            )
        except KeyError:
            return float('nan')

    def get_prices(
        self, codes: list[str], field: str = 'close'
    ) -> dict[str, float]:
        """批量获取当前交易日多个股票的价格"""
        result = {}
        for code in codes:
            result[code] = self.get_price(code, field)
        return result

    def get_suspended_codes(self) -> list[str]:
        """获取当前交易日停牌的股票列表"""
        if self._current_date is None:
            return []

        suspended = []
        for code in self._codes:
            if "is_suspended" not in self._daily_data.columns:
                if (code, self._current_date) not in self._daily_data.index:
                    suspended.append(code)
                continue
            try:
                is_susp = self._daily_data.loc[
                    (code, self._current_date), 'is_suspended'
                ]
                if is_susp:
                    suspended.append(code)
            except KeyError:
                suspended.append(code)  # 无数据 = 视为停牌
        return suspended

    def is_suspended(self, code: str) -> bool:
        """判断某只股票在当前交易日是否停牌"""
        return code in self.get_suspended_codes()

    def get_limit_prices(self, code: str) -> tuple[float, float]:
        """获取涨跌停价格

        Returns:
            (down_limit, up_limit) — 跌停价, 涨停价
        """
        if self._current_date is None:
            raise RuntimeError("DataHandler not initialized")

        try:
            up = float(
                self._daily_data.loc[(code, self._current_date), 'up_limit']
            )
            down = float(
                self._daily_data.loc[(code, self._current_date), 'down_limit']
            )
            return down, up
        except KeyError:
            return 0.0, float('inf')

    def get_historical_prices(
        self, code: str, lookback: int, field: str = 'close'
    ) -> list[float]:
        """获取当前日及之前 lookback 个交易日的价格序列

        Point-in-Time 安全: 只返回 <= current_date 的数据。

        Args:
            code: 股票代码
            lookback: 回溯天数（自然日）
            field: 价格字段

        Returns:
            价格列表，从早到晚排列
        """
        if self._current_date is None:
            return []

        # 从预加载数据中筛选
        df = self._daily_data.loc[code]
        # 只取 <= current_date 的
        mask = df.index <= self._current_date
        recent = df[mask].tail(lookback)
        return recent[field].tolist()

    def get_history(
        self,
        codes: list[str] | None = None,
        lookback: int = 30,
        fields: list[str] | None = None,
    ) -> pd.DataFrame:
        """Return an aligned point-in-time window from the preload cache."""
        if self._current_date is None or self._daily_data.empty:
            return self._daily_data.iloc[0:0].copy()
        selected_codes = codes or self._codes
        selected_fields = fields or ["close"]
        available = [field for field in selected_fields if field in self._daily_data.columns]
        if not available:
            return self._daily_data.iloc[0:0].copy()

        code_values = self._daily_data.index.get_level_values("code")
        date_values = self._daily_data.index.get_level_values("date")
        mask = code_values.isin(selected_codes) & (date_values <= self._current_date)
        history = self._daily_data.loc[mask, available]
        return history.groupby(level="code", group_keys=False).tail(lookback)
