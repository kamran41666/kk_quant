"""Market-specific rules for the persistent paper ledger.

The paper account is intentionally split by market.  This keeps quantities,
currencies, settlement rules, and quote semantics explicit instead of making
the caller infer them from a symbol.  The module has no network dependency;
it only validates and normalizes an order before the ledger mutates.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, timedelta
import calendar
from typing import Optional

from quant_engine.trading.effective_rules import legacy_paper_fee_for


A_SHARE = "a-share"
CN_FUND = "cn-fund"
US_EQUITY = "us-equity"
SUPPORTED_MARKETS = (A_SHARE, CN_FUND, US_EQUITY)
# Major index symbols are valid quote identifiers but are not directly
# tradeable securities.  ETFs and index-linked funds use their own fund/stock
# symbols and are intentionally unaffected by this guard.
NON_TRADABLE_INDEX_CODES = frozenset({
    "000001.SH", "399001.SZ", "399006.SZ", "000300.SH", "000016.SH", "000905.SH",
})


@dataclass(frozen=True)
class MarketRule:
    market: str
    currency: str
    asset_type: str
    display_name: str
    quantity_label: str
    quantity_unit: str
    min_quantity: float
    quantity_step: Optional[float]
    t_plus_one: bool
    valuation_label: str


RULES = {
    A_SHARE: MarketRule(
        market=A_SHARE, currency="CNY", asset_type="equity", display_name="A 股",
        quantity_label="股数", quantity_unit="股", min_quantity=100.0,
        quantity_step=100.0, t_plus_one=True, valuation_label="收盘价",
    ),
    CN_FUND: MarketRule(
        market=CN_FUND, currency="CNY", asset_type="fund", display_name="国内基金",
        quantity_label="份额", quantity_unit="份", min_quantity=0.01,
        quantity_step=0.01, t_plus_one=False, valuation_label="单位净值",
    ),
    US_EQUITY: MarketRule(
        market=US_EQUITY, currency="USD", asset_type="equity", display_name="美股",
        quantity_label="股数", quantity_unit="股", min_quantity=1.0,
        quantity_step=1.0, t_plus_one=False, valuation_label="最新价",
    ),
}

_A_SHARE_RE = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
_FUND_RE = re.compile(r"^(?:FUND:)?(\d{6})$", re.IGNORECASE)
_US_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,11}$")


def normalize_market(value: Optional[str]) -> str:
    market = str(value or A_SHARE).strip().lower()
    if market not in RULES:
        raise ValueError(f"unsupported_paper_market:{market}")
    return market


def rule_for(market: Optional[str]) -> MarketRule:
    return RULES[normalize_market(market)]


def normalize_symbol(market: str, code: str) -> str:
    market = normalize_market(market)
    symbol = str(code or "").strip().upper()
    if market == A_SHARE:
        if not _A_SHARE_RE.fullmatch(symbol):
            raise ValueError("A-share code must use six digits and .SH/.SZ/.BJ")
        return symbol
    if market == CN_FUND:
        match = _FUND_RE.fullmatch(symbol)
        if not match:
            raise ValueError("domestic fund code must use six digits")
        return match.group(1)
    if not _US_RE.fullmatch(symbol):
        raise ValueError("US equity symbol is invalid")
    return symbol


def is_index_symbol(market: str, code: str) -> bool:
    """Return whether a normalized A-share code is a research-only index."""
    return normalize_market(market) == A_SHARE and str(code).strip().upper() in NON_TRADABLE_INDEX_CODES


def validate_quantity(market: str, quantity: float) -> float:
    market = normalize_market(market)
    rule = rule_for(market)
    try:
        value = float(quantity)
    except (TypeError, ValueError) as exc:
        raise ValueError("quantity must be numeric") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError("quantity must be positive and finite")
    step = rule.quantity_step
    if step is not None:
        # Use a scaled integer check so 0.3 does not fail due to binary float
        # representation, while still rejecting 0.005 fund units.
        scaled = value / step
        # Equity markets communicate their lot rule even for values below one
        # lot (for example 0.5 US shares); funds communicate the minimum-unit
        # rule first so 0.001 is clearly below the 0.01 NAV precision.
        if (market != CN_FUND or value >= rule.min_quantity - 1e-9) and abs(scaled - round(scaled)) > 1e-8:
            if market == A_SHARE:
                raise ValueError("A-share paper orders must use 100-share lots")
            if market == US_EQUITY:
                raise ValueError("US equity paper orders must use whole shares")
            raise ValueError("domestic fund paper orders must use 0.01-share increments")
    if value < rule.min_quantity - 1e-9:
        raise ValueError(f"{market}_quantity_below_minimum")
    # Persist fund quantities at the displayed precision; keep integer markets
    # as integer-valued floats for one common ledger type.
    return round(value, 2) if market == CN_FUND else float(round(value))


def unlock_date(market: str, buy_date: date) -> date:
    """Return the first date a newly bought lot may be sold."""
    if normalize_market(market) != A_SHARE:
        return buy_date
    # Import lazily to avoid making the rule module depend on data providers.
    from quant_engine.data.calendar import TradingCalendar
    calendar_data = TradingCalendar(start_year=buy_date.year, end_year=buy_date.year + 1)
    # T+1 only needs a short forward window.  A one-year request would make a
    # valid September trade fail merely because the provider has not published
    # the following calendar year yet.
    report = calendar_data.ensure_coverage(buy_date, buy_date + timedelta(days=10))
    if not report.get("complete"):
        raise ValueError("trading_calendar_unavailable_for_t_plus_one")
    return calendar_data.next_trading_day(buy_date)


def _nth_weekday(year: int, month: int, weekday: int, ordinal: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + (ordinal - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed_fixed_holiday(year: int, month: int, day: int) -> set[date]:
    holiday = date(year, month, day)
    if holiday.weekday() == 5:
        return {holiday, holiday - timedelta(days=1)}
    if holiday.weekday() == 6:
        return {holiday, holiday + timedelta(days=1)}
    return {holiday}


def _us_market_holidays(year: int) -> set[date]:
    """NYSE full-day holidays without depending on an optional calendar package."""
    holidays = set()
    holidays |= _observed_fixed_holiday(year, 1, 1)
    holidays.add(_nth_weekday(year, 1, 0, 3))       # Martin Luther King Jr. Day
    holidays.add(_nth_weekday(year, 2, 0, 3))       # Presidents' Day
    # Good Friday: Easter Sunday minus two days (Gregorian computus).
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    h = (19 * a + b - d - ((b - ((b + 8) // 25) + 1) // 3) + 15) % 30
    i, k = divmod(c, 4)
    easter_weekday_offset = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * easter_weekday_offset) // 451
    month = (h + easter_weekday_offset - 7 * m + 114) // 31
    day = ((h + easter_weekday_offset - 7 * m + 114) % 31) + 1
    holidays.add(date(year, month, day) - timedelta(days=2))
    holidays.add(_last_weekday(year, 5, 0))       # Memorial Day
    if year >= 2022:
        holidays |= _observed_fixed_holiday(year, 6, 19)  # Juneteenth
    holidays |= _observed_fixed_holiday(year, 7, 4)
    holidays.add(_nth_weekday(year, 9, 0, 1))       # Labor Day
    holidays.add(_nth_weekday(year, 11, 3, 4))      # Thanksgiving
    holidays |= _observed_fixed_holiday(year, 12, 25)
    return holidays


def market_session_status(market: str, value: date) -> str:
    """Return ``open``, ``closed`` or ``calendar_unavailable`` for a session."""
    market = normalize_market(market)
    if not isinstance(value, date):
        raise ValueError("market date must be a date")
    if value.weekday() >= 5:
        return "closed"
    if market in {A_SHARE, CN_FUND}:
        # A-share and domestic-fund NAV calendars share the mainland session
        # calendar. A fallback workday approximation is never accepted for a
        # paper order: when the verified public calendar is unavailable this
        # function fails closed by returning False.
        from quant_engine.data.calendar import TradingCalendar
        calendar_data = TradingCalendar(start_year=value.year, end_year=value.year)
        report = calendar_data.ensure_coverage(value, value)
        if not report.get("complete"):
            return "calendar_unavailable"
        return "open" if calendar_data.is_trading_day(value) else "closed"
    holidays = (_us_market_holidays(value.year - 1)
                | _us_market_holidays(value.year)
                | _us_market_holidays(value.year + 1))
    return "open" if value not in holidays else "closed"


def is_market_trading_day(market: str, value: date) -> bool:
    """Return whether a paper order may be dated on this market session."""
    return market_session_status(market, value) == "open"


def accepts_quote_freshness(market: str, freshness: str) -> bool:
    """Market-specific quote policy used by paper valuation/order paths.

    Fund NAV is an end-of-day official value.  A stale label is therefore
    acceptable when its source timestamp is present; it is still surfaced in
    the response and never treated as realtime.  A-share and US equity orders
    continue to require the existing fresh/realtime/delayed policy.
    """
    normalized = str(freshness or "").strip().lower()
    # A manually entered price is an explicit user decision, not a claim
    # about feed freshness.  The caller still has to use
    # ``price_source=manual_input`` in ``_validate_quote``.
    if normalized == "manual":
        return True
    if normalize_market(market) == CN_FUND:
        return normalized in {"fresh", "realtime", "delayed", "stale"}
    return normalized in {"fresh", "realtime", "delayed"}


def fee_for(market: str, notional: float, side: str) -> tuple[float, float]:
    """Return (commission, stamp duty) in the account's base currency."""
    # Keep the persistent paper ledger on its historical schedule.  The new
    # manual-execution planner resolves effective-dated rules separately; it
    # must not rewrite existing paper rows or historical paper results.
    return legacy_paper_fee_for(normalize_market(market), notional, side)


def market_metadata(market: Optional[str]) -> dict[str, str]:
    rule = rule_for(market)
    return {"market": rule.market, "currency": rule.currency, "asset_type": rule.asset_type}
