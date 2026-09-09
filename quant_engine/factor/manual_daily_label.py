"""Pure T-close -> T+1-open -> T+2-close research label."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Mapping, Protocol

from quant_engine.trading.manual_protocol import MANUAL_DAILY_LABEL_ID, stable_hash


class TradingCalendarLike(Protocol):
    def next_trading_day(self, value: date) -> date: ...


@dataclass(frozen=True)
class ManualDailyLabelSpec:
    label_id: str = MANUAL_DAILY_LABEL_ID
    signal_phase: str = "close"
    entry_offset: int = 1
    entry_phase: str = "open"
    exit_offset: int = 2
    exit_phase: str = "close"
    adjusted_prices: bool = True

    def __post_init__(self) -> None:
        if self.label_id != MANUAL_DAILY_LABEL_ID:
            raise ValueError("unsupported manual daily label")
        if (self.signal_phase, self.entry_phase, self.exit_phase) != ("close", "open", "close"):
            raise ValueError("manual daily label phases must be close/open/close")
        if self.entry_offset != 1 or self.exit_offset != 2:
            raise ValueError("manual daily label offsets must be 1 and 2")

    def as_dict(self) -> dict[str, Any]:
        return {
            "label_id": self.label_id,
            "signal_phase": self.signal_phase,
            "entry_offset": self.entry_offset,
            "entry_phase": self.entry_phase,
            "exit_offset": self.exit_offset,
            "exit_phase": self.exit_phase,
            "adjusted_prices": self.adjusted_prices,
        }

    def dates(self, signal_date: date, calendar: TradingCalendarLike) -> tuple[date, date]:
        return label_dates(signal_date, calendar, self)


MANUAL_DAILY_LABEL_V1 = ManualDailyLabelSpec()


@dataclass(frozen=True)
class ManualDailyLabel:
    spec_id: str
    signal_date: date
    entry_date: date
    exit_date: date
    adjusted_open: Decimal
    adjusted_close: Decimal
    value: Decimal
    input_hash: str

    @property
    def return_value(self) -> Decimal:
        return self.value

    @property
    def label(self) -> Decimal:
        return self.value

    def as_dict(self) -> dict[str, Any]:
        return {
            "spec_id": self.spec_id,
            "signal_date": self.signal_date.isoformat(),
            "entry_date": self.entry_date.isoformat(),
            "exit_date": self.exit_date.isoformat(),
            "adjusted_open": str(self.adjusted_open),
            "adjusted_close": str(self.adjusted_close),
            "value": str(self.value),
            "input_hash": self.input_hash,
        }


def label_dates(
    signal_date: date,
    calendar: TradingCalendarLike,
    spec: ManualDailyLabelSpec = MANUAL_DAILY_LABEL_V1,
) -> tuple[date, date]:
    """Return entry and exit sessions using calendar steps only."""
    if not isinstance(signal_date, date):
        raise ValueError("signal_date must be a date")
    if not hasattr(calendar, "next_trading_day"):
        raise TypeError("calendar must provide next_trading_day")
    entry_date = calendar.next_trading_day(signal_date)
    exit_date = calendar.next_trading_day(entry_date)
    if entry_date <= signal_date or exit_date <= entry_date:
        raise ValueError("calendar returned non-forward trading dates")
    return entry_date, exit_date


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{field} must be finite and positive")
    return result


def _price_row(prices: Any, dt: date) -> Mapping[str, Any]:
    if isinstance(prices, Mapping):
        if dt in prices:
            row = prices[dt]
        elif dt.isoformat() in prices:
            row = prices[dt.isoformat()]
        else:
            raise ValueError(f"missing price row for {dt.isoformat()}")
        if not isinstance(row, Mapping):
            raise ValueError(f"price row for {dt.isoformat()} must be a mapping")
        return row
    # Optional pandas support stays input-only and does not add any I/O.  It
    # accepts a date-indexed frame with adjusted_open/adjusted_close or the
    # plain open/close names.
    if hasattr(prices, "loc"):
        try:
            row = prices.loc[dt]
        except (KeyError, TypeError, ValueError):
            try:
                row = prices.loc[dt.isoformat()]
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"missing price row for {dt.isoformat()}") from exc
        if hasattr(row, "to_dict"):
            return row.to_dict()
    raise TypeError("prices must be a date mapping or date-indexed frame")


def compute_manual_daily_label(
    signal_date: date,
    prices: Any,
    calendar: TradingCalendarLike,
    spec: ManualDailyLabelSpec = MANUAL_DAILY_LABEL_V1,
) -> ManualDailyLabel:
    """Compute ``adjusted_close[exit] / adjusted_open[entry] - 1``."""
    entry_date, exit_date = label_dates(signal_date, calendar, spec)
    entry_row = _price_row(prices, entry_date)
    exit_row = _price_row(prices, exit_date)
    entry_value = entry_row.get("adjusted_open", entry_row.get("open"))
    exit_value = exit_row.get("adjusted_close", exit_row.get("close"))
    adjusted_open = _decimal(entry_value, "adjusted_open")
    adjusted_close = _decimal(exit_value, "adjusted_close")
    value = adjusted_close / adjusted_open - Decimal("1")
    input_hash = stable_hash({
        "spec": spec.as_dict(),
        "signal_date": signal_date,
        "entry_date": entry_date,
        "exit_date": exit_date,
        "adjusted_open": adjusted_open,
        "adjusted_close": adjusted_close,
    })
    return ManualDailyLabel(
        spec_id=spec.label_id,
        signal_date=signal_date,
        entry_date=entry_date,
        exit_date=exit_date,
        adjusted_open=adjusted_open,
        adjusted_close=adjusted_close,
        value=value,
        input_hash=input_hash,
    )


def manual_daily_label_dates(signal_date: date, calendar: TradingCalendarLike) -> tuple[date, date]:
    return label_dates(signal_date, calendar)


def calculate_manual_daily_label(signal_date: date, prices: Any, calendar: TradingCalendarLike) -> ManualDailyLabel:
    return compute_manual_daily_label(signal_date, prices, calendar)


__all__ = [
    "TradingCalendarLike", "ManualDailyLabelSpec", "MANUAL_DAILY_LABEL_V1",
    "ManualDailyLabel", "label_dates", "manual_daily_label_dates",
    "compute_manual_daily_label", "calculate_manual_daily_label",
]
