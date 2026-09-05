"""Cached A-share security master with an explicit upstream boundary."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import threading
from typing import Any, Callable, Optional

import pandas as pd

from quant_engine.data.fetcher.akshare_adapter import AKShareAdapter
from quant_engine.data.store import MetaDB, StockInfo


class SecurityMasterUnavailableError(RuntimeError):
    """Raised when neither the upstream nor the local security cache is usable."""

    def __init__(self, attempts: list[dict[str, str]]):
        self.attempts = attempts
        super().__init__("Security master unavailable")


class SecurityMasterProvider:
    """Synchronize and query the A-share security universe.

    The upstream fetch is bounded and cached in memory plus ``meta.db``.  A
    stale local cache is still labelled as such; no synthetic securities are
    created when every source is unavailable.
    """

    def __init__(
        self,
        fetcher: Optional[Callable[[], pd.DataFrame]] = None,
        db: Optional[MetaDB] = None,
        timeout_seconds: float = 15.0,
        cache_ttl_seconds: float = 300.0,
        minimum_snapshot_rows: int = 4000,
    ) -> None:
        if timeout_seconds <= 0 or cache_ttl_seconds <= 0 or minimum_snapshot_rows <= 0:
            raise ValueError("timeouts, cache TTL, and minimum snapshot rows must be positive")
        self._fetcher = fetcher or AKShareAdapter().fetch_stock_list
        self._db = db or MetaDB()
        self._timeout_seconds = timeout_seconds
        self._cache_ttl_seconds = cache_ttl_seconds
        self._minimum_snapshot_rows = minimum_snapshot_rows
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._executor_lock = threading.Lock()
        self._inflight = None
        self._closed = False
        self._snapshot_lock = threading.Lock()
        self._blocked_until: Optional[datetime] = None
        # A timed-out HTTP worker cannot be forcefully killed. Keep one
        # single-flight worker and fail fast during a cooldown instead of
        # replacing executors and accumulating unbounded stuck threads.
        self._timeout_cooldown_seconds = max(1.0, min(30.0, timeout_seconds))
        self._lock = threading.Lock()
        self._cache: list[dict[str, Any]] = []
        self._cache_at: Optional[datetime] = None
        self._cache_source_at: Optional[datetime] = None
        self._cache_stale = False
        self._health: dict[str, Any] = {
            "name": "akshare:stock_master",
            "status": "unknown",
            "last_attempt_at": None,
            "last_success_at": None,
            "last_error": None,
            "cached_count": 0,
        }

    def snapshot(self, force: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        now = datetime.now(timezone.utc)
        with self._lock:
            if (
                not force
                and self._cache
                and self._cache_at is not None
                and (now - self._cache_at).total_seconds() < self._cache_ttl_seconds
            ):
                return list(self._cache), self._meta(
                    "cache:memory", self._cache_source_at or self._cache_at,
                    stale=self._cache_stale,
                )

        # Serialize refresh, validation and persistence as one unit. This is
        # separate from the cache lock so readers remain non-blocking while a
        # network call is in progress.
        with self._snapshot_lock:
            now = datetime.now(timezone.utc)
            with self._lock:
                if (
                    not force
                    and self._cache
                    and self._cache_at is not None
                    and (now - self._cache_at).total_seconds() < self._cache_ttl_seconds
                ):
                    return list(self._cache), self._meta(
                        "cache:memory", self._cache_source_at or self._cache_at,
                        stale=self._cache_stale,
                    )
            try:
                frame = self._fetch_with_timeout()
                rows = self._normalize(frame)
                self._validate_snapshot(rows)
                self._persist(rows)
                self._record_success(now, len(rows))
                with self._lock:
                    self._cache = rows
                    self._cache_at = now
                    self._cache_source_at = now
                    self._cache_stale = False
                return list(rows), self._meta("akshare:stock_master", now)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                self._record_failure(now, message)
                local = self._load_local()
                if local:
                    with self._lock:
                        self._cache = local
                        self._cache_at = now
                        self._cache_source_at = self._local_updated_at()
                        self._cache_stale = True
                    return list(local), self._meta("meta:stock_master", self._local_updated_at(), stale=True)
                raise SecurityMasterUnavailableError([{
                    "source": "akshare:stock_master",
                    "error": message,
                }]) from exc

    def health(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._health)

    def close(self) -> None:
        with self._executor_lock:
            executor = self._executor
            self._closed = True
        executor.shutdown(wait=False, cancel_futures=True)

    def _fetch_with_timeout(self) -> pd.DataFrame:
        now = datetime.now(timezone.utc)
        with self._executor_lock:
            if self._closed:
                raise RuntimeError("security master provider is closed")
            if self._blocked_until and now < self._blocked_until:
                remaining = (self._blocked_until - now).total_seconds()
                raise TimeoutError(
                    f"provider cooldown active for {remaining:.1f}s after timeout"
                )
            if self._inflight is not None:
                if not self._inflight.done():
                    raise TimeoutError("provider request already in flight")
                self._inflight = None
            future = self._executor.submit(self._fetcher)
            self._inflight = future
        try:
            frame = future.result(timeout=self._timeout_seconds)
        except FutureTimeoutError as exc:
            future.cancel()
            with self._executor_lock:
                self._blocked_until = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=self._timeout_cooldown_seconds)
                )
            raise TimeoutError(
                f"provider timed out after {self._timeout_seconds:.1f}s"
            ) from exc
        else:
            with self._executor_lock:
                self._blocked_until = None
                if self._inflight is future:
                    self._inflight = None
        if frame is None or not isinstance(frame, pd.DataFrame):
            raise ValueError("provider did not return a DataFrame")
        return frame

    def _normalize(self, frame: pd.DataFrame) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for _, row in frame.iterrows():
            raw_code = self._first_value(row, ("full_code", "code", "代码"))
            if raw_code is None:
                continue
            code = str(raw_code).strip().upper()
            if "." in code:
                symbol, exchange = code.rsplit(".", 1)
                exchange = self._normalize_exchange(exchange) or self._normalize_exchange(
                    self._first_value(row, ("exchange", "交易所"))
                )
            else:
                symbol = code
                exchange = self._normalize_exchange(
                    self._first_value(row, ("exchange", "交易所"))
                )
            symbol = symbol.zfill(6)
            # The upstream adapter historically labelled some BSE symbols as
            # SSE because it only knew the 6/9 prefix rule.  Prefer the
            # unambiguous BSE prefixes so a 920xxx/8xxxxx listing cannot be
            # silently routed to the wrong exchange.
            if symbol.startswith(("4", "8", "92")):
                exchange = "BJ"
            elif not exchange:
                exchange = self._exchange_for(symbol)
            if len(symbol) != 6 or not symbol.isdigit() or exchange not in {"SH", "SZ", "BJ"}:
                continue
            name = str(self._first_value(row, ("name", "名称")) or "").strip()
            if not name:
                continue
            rows.append({
                "code": f"{symbol}.{exchange}",
                "name": name,
                "exchange": exchange,
                "board": self._board_for(symbol, exchange),
                "listed_date": self._optional_date(row.get("listed_date")),
                "delisted_date": self._optional_date(row.get("delisted_date")),
            })
        return sorted({row["code"]: row for row in rows}.values(), key=lambda item: item["code"])

    def _persist(self, rows: list[dict[str, Any]]) -> None:
        infos = [StockInfo(
                code=row["code"], name=row["name"],
                exchange=row["exchange"], board=row["board"],
                listed_date=self._date_value(row.get("listed_date")),
                delisted_date=self._date_value(row.get("delisted_date")),
            ) for row in rows]
        replace_stocks = getattr(self._db, "replace_stocks", None)
        if replace_stocks:
            replace_stocks(infos)
        elif (bulk_upsert := getattr(self._db, "upsert_stocks", None)):
            bulk_upsert(infos)
        else:
            for info in infos:
                self._db.upsert_stock(info)
        self._db.set_data_version("security_master", datetime.now(timezone.utc).isoformat())
        self._db.log_update("akshare:stock_master", len(rows), "ok")

    def _validate_snapshot(self, rows: list[dict[str, Any]]) -> None:
        if len(rows) < self._minimum_snapshot_rows:
            raise ValueError(
                f"provider returned only {len(rows)} securities; "
                f"minimum is {self._minimum_snapshot_rows}"
            )
        previous = self._load_local()
        if previous and len(rows) < max(
            self._minimum_snapshot_rows, int(len(previous) * 0.8)
        ):
            raise ValueError(
                f"provider snapshot shrank from {len(previous)} to {len(rows)} securities"
            )
        if self._minimum_snapshot_rows >= 1000:
            exchanges = {row["exchange"] for row in rows}
            boards = {row["board"] for row in rows}
            if not {"SH", "SZ"}.issubset(exchanges):
                raise ValueError("provider snapshot is missing SH or SZ securities")
            if not {"主板", "创业板", "科创板"}.issubset(boards):
                raise ValueError("provider snapshot is missing a required board")

    def _load_local(self) -> list[dict[str, Any]]:
        try:
            stocks = self._db.get_all_stocks()
            if not stocks:
                return []
            # Re-run legacy rows through the same normalizer. Older pipeline
            # versions persisted SSE/SZSE and sometimes omitted the suffix.
            return self._normalize(pd.DataFrame(asdict(stock) for stock in stocks))
        except Exception:
            return []

    def _local_updated_at(self) -> Optional[datetime]:
        try:
            value = self._db.get_data_version("security_master")
            return datetime.fromisoformat(value) if value else None
        except (AttributeError, TypeError, ValueError):
            return None

    def _record_success(self, now: datetime, count: int) -> None:
        stamp = now.isoformat()
        with self._lock:
            self._health.update(
                status="ok", last_attempt_at=stamp,
                last_success_at=stamp, last_error=None, cached_count=count,
            )

    def _record_failure(self, now: datetime, error: str) -> None:
        with self._lock:
            self._health.update(
                status="unavailable", last_attempt_at=now.isoformat(),
                last_error=error,
            )

    @staticmethod
    def _meta(source: str, at: Optional[datetime], stale: bool = False) -> dict[str, Any]:
        return {
            "source": source,
            "updated_at": at.isoformat() if at else None,
            "freshness": "stale" if stale else "fresh",
        }

    @staticmethod
    def _optional_date(value: Any) -> Optional[str]:
        if value is None:
            return None
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
        if isinstance(value, str) and value.strip().lower() in {"nat", "nan", "none"}:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _normalize_exchange(value: Any) -> Optional[str]:
        if value is None:
            return None
        exchange = str(value).strip().upper()
        return {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get(
            exchange, exchange if exchange in {"SH", "SZ", "BJ"} else None
        )

    @staticmethod
    def _first_value(row: pd.Series, keys: tuple[str, ...]) -> Any:
        for key in keys:
            value = row.get(key)
            if value is None:
                continue
            try:
                if pd.isna(value):
                    continue
            except (TypeError, ValueError):
                pass
            return value
        return None

    @staticmethod
    def _date_value(value: Any) -> Optional[date]:
        if value is None:
            return None
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None

    @staticmethod
    def _exchange_for(symbol: str) -> str:
        if symbol.startswith(("6", "68")):
            return "SH"
        if symbol.startswith(("4", "8", "92")):
            return "BJ"
        if symbol.startswith("9"):
            return "SH"
        return "SZ"

    @classmethod
    def _board_for(cls, symbol: str, exchange: str) -> str:
        if exchange == "BJ":
            return "北交所"
        if symbol.startswith(("300", "301")):
            return "创业板"
        if symbol.startswith(("688", "689")):
            return "科创板"
        return "主板"
