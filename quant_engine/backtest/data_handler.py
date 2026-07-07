"""DataHandler — Point-in-Time 数据供给

回测引擎的数据来源。每次 push_day() 推进一个交易日，
只向策略暴露当前日及之前的数据，杜绝前视偏差。
"""
from datetime import date
from typing import Optional

from quant_engine.data.api import DataAPI


class DataHandler:
    """Point-in-Time 数据供给器

    回测引擎通过 DataHandler 访问行情数据。
    数据按交易日逐步释放，策略只能看到"当前已知"的信息。
    """

    def __init__(self, codes: list[str], start: date, end: date):
        self._codes = list(codes)
        self._start = start
        self._end = end
        self._api = DataAPI()

        # 预加载整个区间数据到内存
        self._daily_data = self._api.daily(
            codes=self._codes,
            start=start,
            end=end,
            fields=["open", "high", "low", "close", "volume", "amount",
                    "turnover_rate", "up_limit", "down_limit", "is_suspended"],
            adjust="event_driven",
        )

        self._current_date: Optional[date] = None

    @property
    def current_date(self) -> Optional[date]:
        return self._current_date

    @property
    def stock_list(self) -> list[str]:
        return list(self._codes)

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
