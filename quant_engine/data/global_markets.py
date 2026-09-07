"""Bounded public-data providers for non-A-share research markets.

These providers deliberately live beside, not inside, the A-share Tencent /
AKShare chain.  Each provider returns the same ``MarketQuote`` contract while
keeping its own symbol rules, currency and source metadata.  They are for
low-frequency research and paper observation only; neither provider is a
brokerage execution source.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
import json
import math
import re
from threading import Lock
from typing import Any, Callable, Optional, Sequence
from urllib.parse import quote as url_quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from quant_engine.data.live import MarketDataUnavailableError, MarketQuote


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
US_EASTERN_TZ = ZoneInfo("America/New_York")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _freshness(source_time: Optional[datetime], received: datetime) -> str:
    if source_time is None:
        return "unknown"
    age = max(0.0, (received - source_time).total_seconds())
    if age <= 120:
        return "fresh"
    if age <= 900:
        return "delayed"
    return "stale"


def _number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _http_json(url: str, timeout: float) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "kk-quant-research/0.3"})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
    payload = json.loads(raw.decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise ValueError("provider response must be a JSON object")
    return payload


def _http_text(url: str, timeout: float) -> str:
    request = Request(url, headers={
        "User-Agent": "kk-quant-research/0.3",
        "Referer": "https://fund.eastmoney.com/",
    })
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8-sig", errors="replace")


class YahooUSMarketDataProvider:
    """Yahoo Finance chart endpoint for US equity research snapshots."""

    # ``fetch_quotes`` is a current snapshot API. ``fetch_daily`` supports
    # explicit history, but the scheduler must not confuse the two paths.
    supports_historical_dates = False

    # Yahoo uses a leading caret for broad indices (for example ``^NDX``).
    # Keep the rest of the symbol contract strict so arbitrary URL/path
    # fragments cannot be passed through to the public provider.
    _symbol_pattern = re.compile(r"^\^?[A-Z][A-Z0-9.\-]{0,11}$")
    _index_names = {
        "^NDX": "纳斯达克100",
        "^DJI": "道琼斯工业指数",
        "^GSPC": "标普500",
    }

    def __init__(self, timeout_seconds: float = 8.0,
                 fetcher: Optional[Callable[[str, float], dict[str, Any]]] = None,
                 text_fetcher: Optional[Callable[[str, float], str]] = None) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = float(timeout_seconds)
        self._fetcher = fetcher or _http_json
        self._text_fetcher = text_fetcher or _http_text
        self._lock = Lock()
        self._health = {
            "name": "yahoo:chart",
            "status": "unknown",
            "last_attempt_at": None,
            "last_success_at": None,
            "last_error": None,
        }

    @property
    def source_name(self) -> str:
        return "yahoo:chart"

    @classmethod
    def normalize_symbol(cls, symbol: str) -> str:
        normalized = str(symbol).strip().upper()
        if not cls._symbol_pattern.fullmatch(normalized):
            raise ValueError(f"invalid US symbol: {symbol}")
        return normalized

    def _chart(self, symbol: str, start: Optional[date] = None,
               end: Optional[date] = None, range_: str = "5d") -> dict[str, Any]:
        encoded = url_quote(symbol, safe=".-")
        if start is not None and end is not None:
            period1 = int(datetime.combine(start, time.min, timezone.utc).timestamp())
            period2 = int(datetime.combine(end, time.min, timezone.utc).timestamp()) + 86_400
            query = f"period1={period1}&period2={period2}&interval=1d&events=div%2Csplits"
        else:
            query = f"range={range_}&interval=1d&events=div%2Csplits"
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?{query}"
        payload = self._fetcher(url, self._timeout_seconds)
        chart = payload.get("chart")
        result = chart.get("result") if isinstance(chart, dict) else None
        if not isinstance(result, list) or not result or not isinstance(result[0], dict):
            error = chart.get("error") if isinstance(chart, dict) else None
            raise ValueError(f"Yahoo returned no chart result: {error or 'empty'}")
        return result[0]

    @staticmethod
    def _rows(result: dict[str, Any]) -> list[dict[str, Any]]:
        timestamps = result.get("timestamp") or []
        quotes = ((result.get("indicators") or {}).get("quote") or [{}])
        quote = quotes[0] if quotes and isinstance(quotes[0], dict) else {}
        rows: list[dict[str, Any]] = []
        for index, raw_timestamp in enumerate(timestamps):
            try:
                row_date = datetime.fromtimestamp(float(raw_timestamp), timezone.utc).date()
            except (TypeError, ValueError, OSError):
                continue
            values = {
                "open": _number((quote.get("open") or [])[index] if index < len(quote.get("open") or []) else None),
                "high": _number((quote.get("high") or [])[index] if index < len(quote.get("high") or []) else None),
                "low": _number((quote.get("low") or [])[index] if index < len(quote.get("low") or []) else None),
                "close": _number((quote.get("close") or [])[index] if index < len(quote.get("close") or []) else None),
                "volume": _number((quote.get("volume") or [])[index] if index < len(quote.get("volume") or []) else None),
            }
            if any(values[key] is None for key in ("open", "high", "low", "close")):
                continue
            rows.append({"date": row_date.isoformat(), **values})
        return sorted(rows, key=lambda item: item["date"])

    def fetch_quotes(self, symbols: Sequence[str]) -> list[MarketQuote]:
        requested = list(dict.fromkeys(self.normalize_symbol(symbol) for symbol in symbols))
        if not requested:
            raise ValueError("at least one US symbol is required")
        received = _now()
        attempts: list[dict[str, str]] = []
        collected: list[MarketQuote] = []
        for symbol in requested:
            try:
                result = self._chart(symbol)
                meta = result.get("meta") or {}
                rows = self._rows(result)
                price = _number(meta.get("regularMarketPrice"))
                if price is None and rows:
                    price = rows[-1]["close"]
                previous = _number(meta.get("previousClose")) or _number(meta.get("chartPreviousClose"))
                if previous is None and len(rows) >= 2:
                    previous = rows[-2]["close"]
                if price is None or price <= 0:
                    raise ValueError("provider returned no positive price")
                source_epoch = meta.get("regularMarketTime")
                source_time = None
                if source_epoch is not None:
                    source_time = datetime.fromtimestamp(float(source_epoch), timezone.utc)
                elif rows:
                    source_time = datetime.fromisoformat(rows[-1]["date"]).replace(tzinfo=timezone.utc)
                change_pct = ((price - previous) / previous * 100.0) if previous and previous > 0 else None
                collected.append(MarketQuote(
                    code=symbol,
                    name=self._index_names.get(symbol) or str(meta.get("longName") or meta.get("shortName") or symbol),
                    price=price,
                    change_pct=change_pct,
                    volume=_number(meta.get("regularMarketVolume")) or (rows[-1]["volume"] if rows else None),
                    amount=None,
                    source=self.source_name,
                    as_of=source_time.isoformat() if source_time else None,
                    received_at=received.isoformat(),
                    freshness=_freshness(source_time, received),
                    is_fallback=False,
                    asset_type="index" if symbol in self._index_names else "equity",
                    market="US",
                    currency=str(meta.get("currency") or "USD"),
                ))
            except Exception as exc:
                attempts.append({"source": self.source_name, "symbol": symbol,
                                 "error": f"{type(exc).__name__}: {exc}"})
        self._record_health(received, bool(collected), attempts[-1]["error"] if attempts and not collected else None)
        if not collected:
            raise MarketDataUnavailableError(attempts or [{"source": self.source_name, "error": "no_symbols"}])
        return collected

    def fetch_daily(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        if start > end:
            raise ValueError("start date must not be after end date")
        if (end - start).days + 1 > 2_000:
            raise ValueError("requested US history exceeds 2000 calendar days")
        symbol = self.normalize_symbol(symbol)
        received = _now()
        try:
            rows = self._rows(self._chart(symbol, start=start, end=end))
            rows = [row for row in rows if start.isoformat() <= row["date"] <= end.isoformat()]
            if not rows:
                raise ValueError("provider returned no usable daily rows")
            for row in rows:
                row.update({"code": symbol, "source": self.source_name, "adjust": "none"})
            self._record_health(received, True, None)
            return rows
        except MarketDataUnavailableError:
            raise
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            self._record_health(received, False, message)
            raise MarketDataUnavailableError([{"source": self.source_name, "symbol": symbol, "error": message}]) from exc

    def _record_health(self, attempted: datetime, success: bool, error: Optional[str]) -> None:
        with self._lock:
            self._health["last_attempt_at"] = attempted.isoformat()
            if success:
                self._health["status"] = "ok"
                self._health["last_success_at"] = attempted.isoformat()
                self._health["last_error"] = None
            else:
                self._health["status"] = "unavailable"
                self._health["last_error"] = error

    def health(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(self._health)]


class EastmoneyFundDataProvider:
    """Eastmoney historical NAV endpoint for domestic mutual funds."""

    # The quote method returns the latest published NAV, not a replayable
    # snapshot for an arbitrary accounting date. Historical runs must opt in
    # to a dedicated replay provider.
    supports_historical_dates = False

    _code_pattern = re.compile(r"^(?:FUND:)?(\d{6})$", re.IGNORECASE)
    _names = {
        "110022": "易方达消费行业股票",
        "161725": "招商中证白酒指数",
        "005827": "易方达蓝筹精选混合",
        "000300": "沪深300ETF（基金示例）",
    }

    def __init__(self, timeout_seconds: float = 8.0,
                 fetcher: Optional[Callable[[str, float], dict[str, Any]]] = None,
                 text_fetcher: Optional[Callable[[str, float], str]] = None) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = float(timeout_seconds)
        self._fetcher = fetcher or _http_json
        self._text_fetcher = text_fetcher or _http_text
        self._lock = Lock()
        self._health = {
            "name": "eastmoney:fund_nav",
            "status": "unknown",
            "last_attempt_at": None,
            "last_success_at": None,
            "last_error": None,
        }

    @property
    def source_name(self) -> str:
        return "eastmoney:fund_nav"

    @classmethod
    def normalize_symbol(cls, symbol: str) -> str:
        match = cls._code_pattern.fullmatch(str(symbol).strip().upper())
        if not match:
            raise ValueError(f"invalid domestic fund code: {symbol}")
        return match.group(1)

    def _history(self, code: str, page_size: int = 1000) -> list[dict[str, Any]]:
        url = (
            "https://api.fund.eastmoney.com/f10/lsjz?fundCode="
            f"{code}&pageIndex=1&pageSize={page_size}"
        )
        records: Any = None
        try:
            payload = self._fetcher(url, self._timeout_seconds)
            data = payload.get("Data")
            records = data.get("LSJZList") if isinstance(data, dict) else None
        except Exception:
            # Eastmoney occasionally blocks the JSON endpoint while keeping
            # the public per-fund chart script available.  The fallback is
            # still the same source and is parsed as data, never executed.
            records = None
        if not isinstance(records, list):
            chart_url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
            raw = self._text_fetcher(chart_url, self._timeout_seconds)
            match = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", raw, re.S)
            if not match:
                raise ValueError("fund provider returned no history")
            try:
                trend = json.loads(match.group(1))
            except json.JSONDecodeError as exc:
                raise ValueError("fund provider returned malformed history") from exc
            if not isinstance(trend, list):
                raise ValueError("fund provider returned no history")
            records = []
            previous_nav: Optional[float] = None
            for item in trend:
                if not isinstance(item, dict):
                    continue
                stamp = _number(item.get("x"))
                nav = _number(item.get("y"))
                if stamp is None or nav is None:
                    continue
                row_date = datetime.fromtimestamp(stamp / 1000, tz=SHANGHAI_TZ).date()
                if row_date > datetime.now(SHANGHAI_TZ).date():
                    continue
                change = _number(item.get("equityReturn"))
                if change is None and previous_nav not in (None, 0):
                    change = (nav / previous_nav - 1) * 100
                records.append({"FSRQ": row_date.isoformat(), "DWJZ": nav, "JZZZL": change})
                previous_nav = nav
        rows: list[dict[str, Any]] = []
        for item in records:
            if not isinstance(item, dict):
                continue
            try:
                row_date = date.fromisoformat(str(item.get("FSRQ")))
            except ValueError:
                continue
            # Public fund feeds can contain a pre-published valuation date.
            # Do not expose future NAV rows as if they were observable today;
            # keep this rule identical for the JSON and text-script paths.
            if row_date > datetime.now(SHANGHAI_TZ).date():
                continue
            nav = _number(item.get("DWJZ"))
            change_pct = _number(item.get("JZZZL"))
            if nav is None or nav <= 0:
                continue
            rows.append({"date": row_date.isoformat(), "nav": nav, "change_pct": change_pct})
        rows.sort(key=lambda item: item["date"])
        if not rows:
            raise ValueError("fund provider returned no usable NAV rows")
        return rows

    @staticmethod
    def _as_of(row_date: str) -> datetime:
        dt = datetime.combine(date.fromisoformat(row_date), time(15, 0), SHANGHAI_TZ)
        return dt.astimezone(timezone.utc)

    def fetch_quotes(self, symbols: Sequence[str]) -> list[MarketQuote]:
        requested = list(dict.fromkeys(self.normalize_symbol(symbol) for symbol in symbols))
        if not requested:
            raise ValueError("at least one domestic fund code is required")
        received = _now()
        attempts: list[dict[str, str]] = []
        collected: list[MarketQuote] = []
        for code in requested:
            try:
                row = self._history(code)[-1]
                source_time = self._as_of(row["date"])
                collected.append(MarketQuote(
                    code=code,
                    name=self._names.get(code, f"基金 {code}"),
                    price=row["nav"],
                    change_pct=row["change_pct"],
                    volume=None,
                    amount=None,
                    source=self.source_name,
                    as_of=source_time.isoformat(),
                    received_at=received.isoformat(),
                    freshness=_freshness(source_time, received),
                    is_fallback=False,
                    asset_type="fund",
                    market="CN-FUND",
                    currency="CNY",
                ))
            except Exception as exc:
                attempts.append({"source": self.source_name, "symbol": code,
                                 "error": f"{type(exc).__name__}: {exc}"})
        self._record_health(received, bool(collected), attempts[-1]["error"] if attempts and not collected else None)
        if not collected:
            raise MarketDataUnavailableError(attempts or [{"source": self.source_name, "error": "no_symbols"}])
        return collected

    def fetch_daily(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        if start > end:
            raise ValueError("start date must not be after end date")
        code = self.normalize_symbol(symbol)
        received = _now()
        try:
            records = self._history(code)
            rows = [item for item in records if start.isoformat() <= item["date"] <= end.isoformat()]
            if not rows:
                raise ValueError("fund provider returned no rows in requested range")
            result = [{
                "code": code,
                "date": item["date"],
                # NAV is a daily valuation, not an exchange OHLC bar.  Expose
                # it as a flat bar so the shared chart remains truthful.
                "open": item["nav"], "high": item["nav"],
                "low": item["nav"], "close": item["nav"],
                "volume": None,
                "source": self.source_name,
                "adjust": "none",
            } for item in rows]
            self._record_health(received, True, None)
            return result
        except MarketDataUnavailableError:
            raise
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            self._record_health(received, False, message)
            raise MarketDataUnavailableError([{"source": self.source_name, "symbol": code, "error": message}]) from exc

    def _record_health(self, attempted: datetime, success: bool, error: Optional[str]) -> None:
        with self._lock:
            self._health["last_attempt_at"] = attempted.isoformat()
            if success:
                self._health["status"] = "ok"
                self._health["last_success_at"] = attempted.isoformat()
                self._health["last_error"] = None
            else:
                self._health["status"] = "unavailable"
                self._health["last_error"] = error

    def health(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(self._health)]
