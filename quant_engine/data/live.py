"""Live market data contracts and AKShare-backed provider.

The public AKShare endpoints do not provide an availability SLA.  This module
therefore keeps provenance on every quote, tries independent upstreams in a
defined order, and raises an explicit error when no usable quote is available.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime, time, timezone
import math
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any, Callable, Optional, Sequence
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


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
