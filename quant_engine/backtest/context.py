"""策略上下文 — 策略运行时通过 context 访问数据和组合信息"""
from datetime import date
from typing import Optional, Protocol, runtime_checkable

import pandas as pd

from quant_engine.data.calendar import TradingCalendar


@runtime_checkable
class PortfolioLike(Protocol):
    """Portfolio-like 接口 — 任何拥有这些属性的对象都可以作为组合传入"""
    cash: float
    market_value: float
    total_value: float

    @property
    def positions(self) -> dict: ...


class StrategyContext:
    """策略运行时上下文

    提供策略在回测中访问数据、组合信息和日志的能力。
    """

    def __init__(self, calendar: TradingCalendar):
        self._calendar = calendar
        self._current_date: Optional[date] = None
        self._portfolio: Optional[PortfolioLike] = None
        self._logs: list[str] = []

    @property
    def current_date(self) -> Optional[date]:
        return self._current_date

    @property
    def calendar(self) -> TradingCalendar:
        return self._calendar

    @property
    def portfolio(self) -> Optional[PortfolioLike]:
        return self._portfolio

    def set_date(self, dt: date):
        self._current_date = dt

    def set_portfolio(self, portfolio: PortfolioLike):
        self._portfolio = portfolio

    def get_factor(self, name: str, dt: date) -> pd.Series:
        """获取因子值。当前阶段返回空 Series (因子模块完成后实现)。"""
        return pd.Series(dtype=float)

    def log(self, message: str):
        self._logs.append(f"[{self._current_date}] {message}")

    def get_logs(self) -> list[str]:
        return list(self._logs)

    def clear_logs(self):
        self._logs.clear()
