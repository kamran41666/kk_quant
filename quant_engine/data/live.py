"""Live market data contracts and AKShare-backed provider.

The public AKShare endpoints do not provide an availability SLA.  This module
therefore keeps provenance on every quote, tries independent upstreams in a
defined order, and raises an explicit error when no usable quote is available.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timezone
import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any, Callable, Optional, Sequence
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_PUBLIC_INDEX_CODES = frozenset({
    "000001.SH", "000016.SH", "000300.SH", "000905.SH",
    "399001.SZ", "399006.SZ",
})


class MarketDataUnavailableError(RuntimeError):
    """Raised when every configured live quote source failed."""

    def __init__(self, attempts: list[dict[str, str]]):
        self.attempts = attempts
        sources = ", ".join(item["source"] for item in attempts)
        super().__init__(f"Live market data unavailable from: {sources}")


@dataclass(frozen=True)
class MarketQuote:
    code: str
    name: str
    price: Optional[float]
    change_pct: Optional[float]
    volume: Optional[float]
    amount: Optional[float]
    source: str
    as_of: Optional[str]
    received_at: str
    freshness: str
    is_fallback: bool
    # Optional cross-market descriptors.  Existing A-share callers retain
    # their original contract through defaults; foreign/fund providers fill
    # these fields explicitly so the UI never infers currency from a code.
    asset_type: str = "equity"
    market: str = "CN"
    currency: str = "CNY"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LiveMarketDataProvider(ABC):
    """Provider boundary for non-guaranteed, current market quotes."""

    # Live providers return current snapshots.  A provider that can safely
    # serve an as-of historical date must opt in explicitly.
    supports_historical_dates = False

    @abstractmethod
    def fetch_quotes(
        self, codes: Optional[Sequence[str]] = None
    ) -> list[MarketQuote]:
        """Return current quotes or raise :class:`MarketDataUnavailableError`."""
        ...

    @abstractmethod
    def health(self) -> list[dict[str, Any]]:
        """Return the latest observed state of each upstream provider."""
        ...


class AKShareLiveMarketDataProvider(LiveMarketDataProvider):
    """AKShare quote provider with East Money -> Sina failover.

    A successful HTTP response is not enough: an empty or un-normalizable data
    frame is treated as a provider failure.  Quotes without an upstream market
    timestamp deliberately report ``as_of=None`` and ``freshness='unknown'``.
    """

    def __init__(
        self,
        eastmoney_fetcher: Optional[Callable[[], pd.DataFrame]] = None,
        sina_fetcher: Optional[Callable[[], pd.DataFrame]] = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._fetchers: list[tuple[str, Callable[[], pd.DataFrame]]] = [
            ("akshare:eastmoney", eastmoney_fetcher or ak.stock_zh_a_spot_em),
            ("akshare:sina", sina_fetcher or ak.stock_zh_a_spot),
        ]
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds
        # One bounded worker per upstream prevents an uncooperative source from
        # starving the independent fallback source or spawning threads per poll.
        self._executors = {
            source: ThreadPoolExecutor(max_workers=1)
            for source, _ in self._fetchers
        }
        self._inflight: dict[str, Any] = {}
        self._inflight_lock = threading.Lock()
        self._lock = threading.Lock()
        self._health: dict[str, dict[str, Any]] = {
            name: {
                "name": name,
                "status": "unknown",
                "last_attempt_at": None,
                "last_success_at": None,
                "last_error": None,
            }
            for name, _ in self._fetchers
        }

    def fetch_quotes(
        self, codes: Optional[Sequence[str]] = None
    ) -> list[MarketQuote]:
        requested = {_canonical_code(code) for code in codes} if codes else None
        attempts: list[dict[str, str]] = []
        collected: dict[str, MarketQuote] = {}

        for index, (source, fetcher) in enumerate(self._fetchers):
            if requested is not None and requested.issubset(collected):
                break
            received = datetime.now(timezone.utc)
            try:
                frame = self._call_with_timeout(source, fetcher)
                if frame is None or frame.empty:
                    raise ValueError("provider returned no rows")
                needed = None if requested is None else requested - set(collected)
                quotes = self._normalize(
                    frame=frame,
                    source=source,
                    received=received,
                    requested=needed,
                    is_fallback=index > 0,
                )
                if not quotes:
                    raise ValueError("provider returned no usable requested quotes")
                for quote in quotes:
                    collected.setdefault(quote.code, quote)
                self._record_success(source, received)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                attempts.append({"source": source, "error": message})
                self._record_failure(source, received, message)

        if not collected:
            raise MarketDataUnavailableError(attempts)
        return list(collected.values())

    def _call_with_timeout(self, source: str, fetcher: Callable[[], pd.DataFrame]) -> pd.DataFrame:
        """Bound provider latency so one blocked upstream cannot hang the API."""
        with self._inflight_lock:
            previous = self._inflight.get(source)
            if previous is not None and not previous.done():
                raise TimeoutError(f"provider request already in flight for {source}")
            future = self._executors[source].submit(fetcher)
            self._inflight[source] = future
        try:
            return future.result(timeout=self._timeout_seconds)
        except FutureTimeoutError as exc:
            future.cancel()
            raise TimeoutError(
                f"provider timed out after {self._timeout_seconds:.1f}s"
            ) from exc
        finally:
            if future.done():
                with self._inflight_lock:
                    if self._inflight.get(source) is future:
                        self._inflight.pop(source, None)

    def health(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(self._health[name]) for name, _ in self._fetchers]

    def close(self) -> None:
        """Release bounded provider workers during application shutdown."""
        for executor in self._executors.values():
            executor.shutdown(wait=False, cancel_futures=True)

    def _record_success(self, source: str, now: datetime) -> None:
        stamp = now.isoformat()
        with self._lock:
            self._health[source].update(
                status="ok",
                last_attempt_at=stamp,
                last_success_at=stamp,
                last_error=None,
            )

    def _record_failure(self, source: str, now: datetime, error: str) -> None:
        with self._lock:
            self._health[source].update(
                status="unavailable",
                last_attempt_at=now.isoformat(),
                last_error=error,
            )

    def _normalize(
        self,
        frame: pd.DataFrame,
        source: str,
        received: datetime,
        requested: Optional[set[str]],
        is_fallback: bool,
    ) -> list[MarketQuote]:
        quotes: list[MarketQuote] = []
        received_at = received.isoformat()

        for _, row in frame.iterrows():
            raw_code = _first(row, ("代码", "code", "symbol", "股票代码"))
            if raw_code is None:
                continue
            code = _canonical_code(str(raw_code))
            if requested is not None and code not in requested:
                continue

            source_time = _source_timestamp(row, received)
            price = _number(_first(row, ("最新价", "trade", "price", "现价")))
            # Rows without a positive trade price are not usable quotes.  Do
            # not mark a provider successful merely because it returned rows.
            if price is None or price <= 0:
                continue
            quotes.append(MarketQuote(
                code=code,
                name=str(_first(row, ("名称", "name", "股票名称")) or ""),
                price=price,
                change_pct=_number(_first(row, ("涨跌幅", "changepercent", "change_pct"))),
                volume=_number(_first(row, ("成交量", "volume", "vol"))),
                amount=_number(_first(row, ("成交额", "amount", "turnover"))),
                source=source,
                as_of=source_time.isoformat() if source_time else None,
                received_at=received_at,
                freshness=_freshness(source_time, received),
                is_fallback=is_fallback,
            ))
        return quotes


class TencentLiveMarketDataProvider(LiveMarketDataProvider):
    """Public Tencent Finance quote source used as a lightweight fallback.

    The endpoint returns a GBK-encoded, tilde-separated snapshot and does not
    require an API key.  It is suitable for a beginner-facing watchlist, not a
    trading execution feed: every quote carries the upstream timestamp and is
    marked stale/unknown when that timestamp cannot be trusted.
    """

    def __init__(
        self,
        timeout_seconds: float = 8.0,
        fetcher: Optional[Callable[[str, float], bytes]] = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds
        self._fetcher = fetcher or self._fetch_http
        self._lock = threading.Lock()
        self._health: dict[str, Any] = {
            "name": "tencent:qt",
            "status": "unknown",
            "last_attempt_at": None,
            "last_success_at": None,
            "last_error": None,
        }

    @staticmethod
    def _fetch_http(url: str, timeout: float) -> bytes:
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; kk-quant/0.3)",
                "Referer": "https://gu.qq.com/",
            },
        )
        with urlopen(request, timeout=timeout) as response:
            return response.read()

    def fetch_quotes(
        self, codes: Optional[Sequence[str]] = None
    ) -> list[MarketQuote]:
        if not codes:
            raise MarketDataUnavailableError([{
                "source": "tencent:qt",
                "error": "codes_required_for_tencent_snapshot",
            }])
        requested = list(dict.fromkeys(_canonical_code(code) for code in codes))
        symbols = ",".join(_tencent_symbol(code) for code in requested)
        url = f"https://qt.gtimg.cn/q={symbols}"
        received = datetime.now(timezone.utc)
        try:
            payload = self._fetcher(url, self._timeout_seconds)
            if isinstance(payload, bytes):
                text = payload.decode("gbk", errors="replace")
            else:
                text = str(payload)
            quotes = self._parse(text, set(requested), received)
            if not quotes:
                raise ValueError("provider returned no usable requested quotes")
            self._record_success(received)
            return quotes
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            self._record_failure(received, message)
            raise MarketDataUnavailableError([{
                "source": "tencent:qt",
                "error": message,
            }]) from exc

    def health(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(self._health)]

    def _record_success(self, now: datetime) -> None:
        stamp = now.isoformat()
        with self._lock:
            self._health.update(
                status="ok", last_attempt_at=stamp,
                last_success_at=stamp, last_error=None,
            )

    def _record_failure(self, now: datetime, error: str) -> None:
        with self._lock:
            self._health.update(
                status="unavailable", last_attempt_at=now.isoformat(),
                last_error=error,
            )

    @staticmethod
    def _parse(
        text: str, requested: set[str], received: datetime
    ) -> list[MarketQuote]:
        quotes: list[MarketQuote] = []
        for chunk in text.split(";"):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            variable, raw = chunk.split("=", 1)
            symbol = variable.strip().removeprefix("v_").strip()
            fields = raw.strip().strip('"').split("~")
            if len(fields) < 34 or len(symbol) < 8:
                continue
            exchange = symbol.lower().replace("s_", "", 1)[:2].upper()
            if exchange not in {"SH", "SZ", "BJ"}:
                continue
            code = _canonical_code(f"{fields[2]}.{exchange}")
            if code not in requested:
                continue
            price = _number(fields[3])
            if price is None or price <= 0:
                continue
            source_time = _tencent_timestamp(fields[30], received)
            volume = _number(fields[6])
            amount = _number(fields[37]) if len(fields) > 37 else None
            quotes.append(MarketQuote(
                code=code,
                name=fields[1].strip(),
                price=price,
                change_pct=_number(fields[32]),
                volume=volume * 100 if volume is not None else None,
                amount=amount * 10_000 if amount is not None else None,
                source="tencent:qt",
                as_of=source_time.isoformat() if source_time else None,
                received_at=received.isoformat(),
                freshness=_freshness(source_time, received),
                is_fallback=False,
            ))
        return quotes


class TencentDailyKlineProvider:
    """Fetch recent daily K-lines from Tencent's public JSON endpoint."""

    def __init__(
        self,
        timeout_seconds: float = 10.0,
        fetcher: Optional[Callable[[str, float], bytes]] = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds
        self._fetcher = fetcher or TencentLiveMarketDataProvider._fetch_http

    def fetch_daily(
        self,
        code: str,
        start: date,
        end: date,
        adjust: str = "event_driven",
    ) -> list[dict[str, Any]]:
        if start > end:
            raise ValueError("start date must not be after end date")
        if (end - start).days + 1 > 1000:
            raise MarketDataUnavailableError([{
                "source": "tencent:kline",
                "error": "requested_range_exceeds_1000_calendar_days",
            }])
        canonical = _canonical_code(code)
        symbol = _tencent_symbol(canonical)
        # Tencent does not publish qfq/hfq series for these broad indexes;
        # requesting qfqday returns ``bad params``. Index levels have no
        # corporate-action adjustment, so use the raw day series.
        is_index = canonical in _PUBLIC_INDEX_CODES
        adjust_key = "day" if is_index else {
            "event_driven": "qfqday",
            "qfq": "qfqday",
            "hfq": "hfqday",
            "none": "day",
        }.get(adjust)
        if adjust_key is None:
            raise ValueError("unsupported Tencent daily adjustment")
        count = min(1000, max(320, (end - start).days + 40))
        adjust_param = "qfq" if adjust_key == "qfqday" else "hfq" if adjust_key == "hfqday" else ""
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{count},{adjust_param}"
        try:
            payload = self._fetcher(url, self._timeout_seconds)
            text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
            body = json.loads(text)
            rows = body.get("data", {}).get(symbol, {}).get(adjust_key, [])
            result: list[dict[str, Any]] = []
            for raw in rows:
                if not isinstance(raw, list) or len(raw) < 6:
                    continue
                try:
                    row_date = date.fromisoformat(str(raw[0]))
                except ValueError:
                    continue
                if not start <= row_date <= end:
                    continue
                values = [_number(item) for item in raw[1:6]]
                if any(value is None for value in values[:4]):
                    continue
                result.append({
                    "code": canonical,
                    "date": row_date.isoformat(),
                    "open": values[0],
                    "close": values[1],
                    "high": values[2],
                    "low": values[3],
                    "volume": values[4] * 100 if values[4] is not None else None,
                    "source": "tencent:kline",
                    "adjust": "none" if is_index else "qfq" if adjust_key == "qfqday" else "hfq" if adjust_key == "hfqday" else "none",
                })
            if not result:
                raise ValueError("provider returned no usable daily rows")
            return sorted(result, key=lambda item: item["date"])
        except MarketDataUnavailableError:
            raise
        except Exception as exc:
            raise MarketDataUnavailableError([{
                "source": "tencent:kline",
                "error": f"{type(exc).__name__}: {exc}",
            }]) from exc


class FallbackLiveMarketDataProvider(LiveMarketDataProvider):
    """Compose providers while preserving provenance and partial results."""

    def __init__(self, providers: Sequence[LiveMarketDataProvider]) -> None:
        if not providers:
            raise ValueError("at least one live market provider is required")
        self._providers = list(providers)

    def fetch_quotes(
        self, codes: Optional[Sequence[str]] = None
    ) -> list[MarketQuote]:
        requested = list(dict.fromkeys(_canonical_code(code) for code in codes)) if codes else None
        collected: dict[str, MarketQuote] = {}
        attempts: list[dict[str, str]] = []
        for index, provider in enumerate(self._providers):
            missing = None if requested is None else [code for code in requested if code not in collected]
            if requested is not None and not missing:
                break
            try:
                quotes = provider.fetch_quotes(missing)
            except MarketDataUnavailableError as exc:
                attempts.extend(exc.attempts)
                continue
            except Exception as exc:
                attempts.append({
                    "source": type(provider).__name__,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                continue
            for quote in quotes:
                if quote.code not in collected:
                    collected[quote.code] = replace(
                        quote, is_fallback=quote.is_fallback or index > 0
                    )
        if not collected:
            raise MarketDataUnavailableError(attempts or [{
                "source": type(self).__name__,
                "error": "no_provider_returned_quotes",
            }])
        if requested is None:
            return list(collected.values())
        return [collected[code] for code in requested if code in collected]

    def health(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for provider in self._providers:
            rows.extend(provider.health())
        return rows

    def close(self) -> None:
        for provider in self._providers:
            close = getattr(provider, "close", None)
            if close:
                close()


def _first(row: pd.Series, aliases: Sequence[str]) -> Any:
    for alias in aliases:
        if alias in row.index:
            value = row[alias]
            if not _missing(value):
                return value
    lowered = {str(column).lower(): column for column in row.index}
    for alias in aliases:
        column = lowered.get(alias.lower())
        if column is not None:
            value = row[column]
            if not _missing(value):
                return value
    return None


def _missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _number(value: Any) -> Optional[float]:
    if value is None or value == "-":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _canonical_code(code: str) -> str:
    cleaned = code.strip().upper()
    if "." in cleaned:
        symbol, exchange = cleaned.rsplit(".", 1)
        exchange = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get(
            exchange, exchange
        )
        return f"{symbol.zfill(6)}.{exchange}"
    symbol = cleaned.zfill(6)
    if symbol.startswith(("4", "8", "92")):
        exchange = "BJ"
    elif symbol.startswith(("6", "9")):
        exchange = "SH"
    else:
        exchange = "SZ"
    return f"{symbol}.{exchange}"


def _tencent_symbol(code: str) -> str:
    symbol, exchange = _canonical_code(code).split(".", 1)
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(exchange)
    if prefix is None:
        raise ValueError(f"unsupported Tencent exchange: {exchange}")
    return f"{prefix}{symbol}"


def _tencent_timestamp(value: str, received: datetime) -> Optional[datetime]:
    text = str(value or "").strip()
    if len(text) == 14 and text.isdigit():
        try:
            return datetime.strptime(text, "%Y%m%d%H%M%S").replace(
                tzinfo=SHANGHAI_TZ
            ).astimezone(timezone.utc)
        except ValueError:
            return None
    if len(text) != 8 or text.count(":") != 2:
        return None
    try:
        parsed_time = time.fromisoformat(text)
    except ValueError:
        return None
    local_received = received.astimezone(SHANGHAI_TZ)
    return datetime.combine(
        local_received.date(), parsed_time, tzinfo=SHANGHAI_TZ
    ).astimezone(timezone.utc)


def _source_timestamp(row: pd.Series, received: datetime) -> Optional[datetime]:
    value = _first(row, ("时间戳", "timestamp", "datetime", "更新时间", "time"))
    if value is None:
        return None
    text = str(value).strip()
    try:
        # Some Sina payloads expose only HH:MM:SS.
        if len(text) <= 8 and ":" in text:
            parsed_time = time.fromisoformat(text)
            local_received = received.astimezone(SHANGHAI_TZ)
            return datetime.combine(
                local_received.date(), parsed_time, tzinfo=SHANGHAI_TZ
            ).astimezone(timezone.utc)
        parsed = pd.Timestamp(text)
        if parsed.tzinfo is None:
            parsed = parsed.tz_localize(SHANGHAI_TZ)
        return parsed.to_pydatetime().astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _freshness(as_of: Optional[datetime], received: datetime) -> str:
    if as_of is None:
        return "unknown"
    age = (received - as_of).total_seconds()
    if age < -5:
        return "unknown"
    if age <= 60:
        return "fresh"
    if age <= 900:
        return "delayed"
    return "stale"
