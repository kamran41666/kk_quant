"""策略上下文 — 策略运行时通过 context 访问数据和组合信息"""
from datetime import date
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

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


@runtime_checkable
class StrategyDataPortal(Protocol):
    """Stable point-in-time data surface exposed to every strategy."""

    @property
    def universe(self) -> Sequence[str]: ...

    def history(self, codes: Sequence[str] | None, lookback: int, fields: Sequence[str]) -> pd.DataFrame: ...

    def current(self, code: str, field: str) -> float: ...

    def factor(self, name: str, as_of: date) -> pd.Series: ...


class DataHandlerPortal:
    """Adapter that keeps the concrete A-share DataHandler out of strategies."""

    def __init__(self, handler: Any):
        self._handler = handler

    @property
    def universe(self) -> Sequence[str]:
        return self._handler.stock_list

    def history(self, codes, lookback, fields) -> pd.DataFrame:
        return self._handler.get_history(codes=list(codes) if codes else None, lookback=lookback, fields=list(fields))

    def current(self, code: str, field: str) -> float:
        return self._handler.get_price(code, field)

    def factor(self, name: str, as_of: date) -> pd.Series:
        return self._handler.get_factor(name, as_of)


class FundNavPortal:
    """Point-in-time adapter over the canonical fund NAV archive."""

    def __init__(self, rows: Mapping[str, Sequence[Mapping[str, Any]]]):
        self._rows = {str(code): list(values) for code, values in rows.items()}
        self._current_date: Optional[date] = None

    @property
    def universe(self) -> Sequence[str]:
        return tuple(self._rows)

    def set_date(self, dt: date) -> None:
        self._current_date = dt

    def history(self, codes, lookback, fields) -> pd.DataFrame:
        selected = list(codes) if codes else list(self._rows)
        rows: list[dict[str, Any]] = []
        for code in selected:
            usable = []
            for item in self._rows.get(code, []):
                try:
                    item_date = pd.Timestamp(item["date"]).date()
                except (KeyError, TypeError, ValueError):
                    continue
                # An unpositioned portal is deliberately empty.  In
                # particular, initialize() must not see the run's future NAVs.
                if self._current_date is not None and item_date <= self._current_date:
                    usable.append({**item, "code": code, "date": item_date})
            rows.extend(usable[-lookback:])
        if not rows:
            return pd.DataFrame(columns=list(fields), index=pd.MultiIndex.from_arrays([[], []], names=["code", "date"]))
        frame = pd.DataFrame(rows).set_index(["code", "date"]).sort_index()
        available = [name for name in fields if name in frame.columns]
        return frame[available]

    def current(self, code: str, field: str) -> float:
        frame = self.history([code], 1, [field])
        if frame.empty or field not in frame:
            return float("nan")
        return float(frame.iloc[-1][field])

    def factor(self, name: str, as_of: date) -> pd.Series:
        raise ValueError(f"panel factor {name!r} is not supported for fund NAV data")


class StrategyContext:
    """策略运行时上下文

    提供策略在回测中访问数据、组合信息和日志的能力。
    """

    def __init__(self, calendar: TradingCalendar):
        self._calendar = calendar
        self._current_date: Optional[date] = None
        self._portfolio: Optional[PortfolioLike] = None
        self._data: Optional[StrategyDataPortal] = None
        self._logs: list[str] = []
        self._diagnostics: list[dict[str, Any]] = []

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

    def bind_data(self, portal: StrategyDataPortal) -> None:
        """Bind the engine-owned point-in-time data adapter before initialize."""
        self._data = portal

    @property
    def universe(self) -> list[str]:
        return list(self._data.universe) if self._data is not None else []

    @property
    def stock_list(self) -> list[str]:
        """Legacy v1 alias retained while stored strategies migrate to ``universe``."""
        return self.universe

    def history(
        self,
        codes: Sequence[str] | None = None,
        *,
        lookback: int = 30,
        fields: Sequence[str] = ("close",),
    ) -> pd.DataFrame:
        if self._data is None:
            raise RuntimeError("strategy data portal is not bound")
        if lookback < 1:
            raise ValueError("lookback must be >= 1")
        return self._data.history(codes, lookback, fields)

    def current(self, code: str, field: str = "close") -> float:
        if self._data is None:
            raise RuntimeError("strategy data portal is not bound")
        return self._data.current(code, field)

    def record(self, key: str, value: Any) -> None:
        """Record a strategy-specific diagnostic for the current date."""
        self._diagnostics.append({"date": self._current_date, "key": key, "value": value})

    def drain_diagnostics(self) -> list[dict[str, Any]]:
        rows, self._diagnostics = self._diagnostics, []
        return rows

    def get_factor(self, name: str, dt: date) -> pd.Series:
        """Return one point-in-time factor cross-section for exactly ``dt``."""
        if self._data is None:
            raise RuntimeError("strategy data portal is not bound")
        if self._current_date is None:
            raise RuntimeError("strategy context is not positioned on a trading date")
        if dt > self._current_date:
            raise ValueError("factor as_of date cannot be later than the current simulation date")
        return self._data.factor(name, dt)

    def log(self, message: str):
        self._logs.append(f"[{self._current_date}] {message}")

    def get_logs(self) -> list[str]:
        return list(self._logs)

    def clear_logs(self):
        self._logs.clear()
