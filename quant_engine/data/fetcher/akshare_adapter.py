from __future__ import annotations

from datetime import date, datetime, timezone
from threading import Event, Lock, Thread
from typing import Any, Callable, Sequence
import time
import warnings

import numpy as np
import pandas as pd
import akshare as ak

from quant_engine.data.fetcher.base import DataSource


class HistoricalConstituentsUnavailableError(RuntimeError):
    """Raised when AKShare cannot provide index membership at a requested date."""


class AKShareAdapter(DataSource):
    """Bounded AKShare adapter used by the historical-data pipeline.

    AKShare functions ultimately perform network I/O but do not expose a
    consistent timeout argument.  Calls therefore run in a small daemon
    worker and are observed with a deadline.  The public ``DataSource``
    methods still return DataFrames for backwards compatibility; callers that
    need observability can inspect ``last_attempts`` or ``health()``.
    """

    def __init__(self, timeout_seconds: float = 8.0, batch_timeout_seconds: float = 120.0) -> None:
        if timeout_seconds <= 0 or batch_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        self._timeout_seconds = float(timeout_seconds)
        self._batch_timeout_seconds = float(batch_timeout_seconds)
        self._lock = Lock()
        self._inflight_lock = Lock()
        self._inflight: dict[str, Event] = {}
        self._closed = False
        self._last_attempts: list[dict[str, Any]] = []
        self._health: dict[str, Any] = {
            "name": "akshare",
            "status": "unknown",
            "last_attempt_at": None,
            "last_success_at": None,
            "last_error": None,
            "last_operation": None,
        }

    @property
    def source_name(self) -> str:
        return "akshare"

    @property
    def last_attempts(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._last_attempts]

    def health(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._health)

    def close(self) -> None:
        """Prevent new calls; an already running third-party call is daemonized."""
        with self._inflight_lock:
            self._closed = True

    def fetch_stock_list(self) -> pd.DataFrame:
        operation = "stock_master"
        try:
            df = self._call(operation, ak.stock_info_a_code_name)
            if not isinstance(df, pd.DataFrame):
                raise TypeError("stock list provider did not return a DataFrame")
            if not {"code", "name"}.issubset(df.columns):
                raise ValueError("stock list is missing code/name columns")
            df = df.copy()
            df["code"] = df["code"].astype(str).str.zfill(6)
            df["exchange"] = df["code"].apply(
                lambda x: "BJ" if x.startswith(("4", "8", "92"))
                else "SH" if x.startswith(("6", "9")) else "SZ"
            )
            df["full_code"] = df.apply(
                lambda r: f"{r['code']}.{r['exchange']}",
                axis=1
            )
            if df.empty:
                raise ValueError("stock list provider returned no rows")
            self._record_success(operation)
            df.attrs["source_meta"] = {
                "source": "akshare:stock_master",
                "received_at": datetime.now(timezone.utc).isoformat(),
                "returned_count": len(df),
                "status": "ok",
            }
            return df
        except Exception as e:
            self._record_failure(operation, e)
            warnings.warn(f"AKShare stock list fetch failed: {e}")
            frame = pd.DataFrame(columns=["code", "name", "exchange", "full_code"])
            frame.attrs["source_meta"] = {
                "source": "akshare:stock_master",
                "received_at": datetime.now(timezone.utc).isoformat(),
                "returned_count": 0,
                "status": "failed",
                "error": f"{type(e).__name__}: {e}",
            }
            return frame

    def fetch_daily(
        self, codes: str | Sequence[str], start: date, end: date
    ) -> pd.DataFrame:
        if start > end:
            raise ValueError("start date must not be after end date")
        if isinstance(codes, str):
            codes = [codes]
        requested: list[str] = []
        for code in codes:
            canonical = self._canonical_code(code)
            if canonical not in requested:
                requested.append(canonical)
        if not requested:
            return self._empty_daily(
                requested, [], [], [], status="failed",
                error="at least one stock code is required",
            )

        frames = []
        attempts: list[dict[str, Any]] = []
        failed_codes: list[str] = []
        blocked_codes: list[str] = []
        empty_codes: list[str] = []
        invalid_row_count = 0
        out_of_range_count = 0
        duplicate_count = 0
        not_attempted_codes: list[str] = []
        batch_started = time.monotonic()
        for code in requested:
            if time.monotonic() - batch_started >= self._batch_timeout_seconds:
                not_attempted_codes.append(code)
                attempts.append({
                    "source": "akshare:daily", "code": code,
                    "status": "not_attempted_deadline",
                })
                continue
            operation = "daily"
            try:
                symbol = self._symbol(code)
                df = self._call(
                    operation,
                    ak.stock_zh_a_hist,
                    symbol=symbol,
                    period="daily",
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                    adjust="",
                )
                if df is None or not isinstance(df, pd.DataFrame):
                    raise TypeError("daily provider did not return a DataFrame")
                required = {"日期", "开盘", "收盘", "最高", "最低", "成交量"}
                if not required.issubset(df.columns):
                    raise ValueError("daily data is missing required OHLCV columns")
                if len(df) == 0:
                    empty_codes.append(code)
                    attempts.append({"source": "akshare:daily", "code": code, "status": "empty"})
                    continue
                df = df.copy()
                df["code"] = code
                df, invalid, out_of_range, duplicates = self._clean_daily_frame(
                    df, start, end
                )
                invalid_row_count += invalid
                out_of_range_count += out_of_range
                duplicate_count += duplicates
                if df.empty:
                    empty_codes.append(code)
                    attempts.append({
                        "source": "akshare:daily", "code": code,
                        "status": "invalid", "invalid_rows": invalid,
                    })
                else:
                    frames.append(df)
                    attempts.append({
                        "source": "akshare:daily", "code": code,
                        "status": "ok", "invalid_rows": invalid,
                    })
            except Exception as exc:
                blocked = isinstance(exc, TimeoutError) and "already in flight" in str(exc)
                if blocked:
                    blocked_codes.append(code)
                else:
                    failed_codes.append(code)
                attempts.append({
                    "source": "akshare:daily", "code": code,
                    "status": "blocked_by_inflight" if blocked else "error",
                    "error": f"{type(exc).__name__}: {exc}",
                })
                continue

        self._set_attempts(attempts)
        if not frames:
            self._record_failure(
                "daily",
                RuntimeError("no usable daily rows returned"), attempts=attempts,
            )
            return self._empty_daily(
                requested, [], failed_codes, empty_codes,
                status="failed", attempts=attempts,
                blocked_codes=blocked_codes,
                not_attempted_codes=not_attempted_codes,
                invalid_row_count=invalid_row_count,
                out_of_range_count=out_of_range_count,
                duplicate_count=duplicate_count,
            )

        result = pd.concat(frames, ignore_index=True)
        result = result.rename(columns={
            "日期": "date", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume",
            "成交额": "amount", "换手率": "turnover_rate",
            "涨跌幅": "pct_change", "涨跌额": "change",
        })
        result = result.sort_values(["code", "date"]).reset_index(drop=True)
        received_at = datetime.now(timezone.utc).isoformat()
        returned_codes = sorted(set(result["code"].astype(str)))
        status = "ok" if (
            not failed_codes and not blocked_codes and not empty_codes
            and invalid_row_count == 0 and out_of_range_count == 0
            and not_attempted_codes == []
            and duplicate_count == 0
        ) else "partial"
        result["source"] = "akshare:daily"
        result["received_at"] = received_at
        result.attrs["source_meta"] = {
            "source": "akshare:daily",
            "received_at": received_at,
            "requested_codes": requested,
            "returned_codes": returned_codes,
            "failed_codes": failed_codes,
            "blocked_codes": blocked_codes,
            "not_attempted_codes": not_attempted_codes,
            "empty_codes": empty_codes,
            "invalid_row_count": invalid_row_count,
            "out_of_range_count": out_of_range_count,
            "duplicate_count": duplicate_count,
            "status": status,
            "attempts": attempts,
        }
        self._record_success("daily", attempts=attempts)
        if status == "partial":
            with self._lock:
                self._health["status"] = "degraded"
        return result

    @staticmethod
    def _empty_daily(
        requested: list[str], returned: list[str], failed: list[str],
        empty: list[str], *, status: str, attempts: list[dict[str, Any]] | None = None,
        blocked_codes: list[str] | None = None,
        not_attempted_codes: list[str] | None = None,
        invalid_row_count: int = 0, out_of_range_count: int = 0,
        duplicate_count: int = 0, error: str | None = None,
    ) -> pd.DataFrame:
        frame = pd.DataFrame()
        frame.attrs["source_meta"] = {
            "source": "akshare:daily",
            "received_at": datetime.now(timezone.utc).isoformat(),
            "requested_codes": requested,
            "returned_codes": returned,
            "failed_codes": failed,
            "blocked_codes": blocked_codes or [],
            "not_attempted_codes": not_attempted_codes or [],
            "empty_codes": empty,
            "invalid_row_count": invalid_row_count,
            "out_of_range_count": out_of_range_count,
            "duplicate_count": duplicate_count,
            "status": status,
            "attempts": attempts or [],
        }
        if error:
            frame.attrs["source_meta"]["error"] = error
        return frame

    @staticmethod
    def _clean_daily_frame(
        frame: pd.DataFrame, start: date, end: date
    ) -> tuple[pd.DataFrame, int, int, int]:
        frame = frame.rename(columns={"日期": "date"}).copy()
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        numeric = ["开盘", "收盘", "最高", "最低", "成交量"]
        for column in numeric:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        bad = frame["date"].isna()
        for column in numeric:
            bad |= frame[column].isna() | ~np.isfinite(frame[column])
        bad |= frame["开盘"] <= 0
        bad |= frame["收盘"] <= 0
        bad |= frame["最高"] <= 0
        bad |= frame["最低"] <= 0
        bad |= frame["最高"] < frame[["开盘", "收盘"]].max(axis=1)
        bad |= frame["最低"] > frame[["开盘", "收盘"]].min(axis=1)
        bad |= frame["成交量"] < 0
        invalid = int(bad.sum())
        frame = frame.loc[~bad].copy()
        if frame.empty:
            return frame, invalid, 0, 0
        out_of_range_mask = (
            (frame["date"].dt.date < start) | (frame["date"].dt.date > end)
        )
        out_of_range = int(out_of_range_mask.sum())
        frame = frame.loc[~out_of_range_mask].copy()
        duplicate_mask = frame.duplicated(subset=["date"], keep="last")
        duplicates = int(duplicate_mask.sum())
        frame = frame.loc[~duplicate_mask].copy()
        return frame, invalid, out_of_range, duplicates

    def fetch_index_components(
        self, index_code: str, dt: date
    ) -> pd.DataFrame:
        # AKShare's CSIndex endpoint exposes the current constituent snapshot;
        # it does not accept a historical date. Returning it for ``dt`` would
        # introduce look-ahead bias into backtests, so fail closed instead.
        self._record_failure(
            "index_components",
            HistoricalConstituentsUnavailableError(
                "AKShare constituent endpoint has no historical-date contract"
            ),
        )
        raise HistoricalConstituentsUnavailableError(
            f"historical index constituents unavailable for {index_code} at {dt}"
        )

    def fetch_current_index_components(self, index_code: str) -> pd.DataFrame:
        """Fetch a *current* snapshot for explicit archival by a caller.

        This method intentionally has no historical-date argument.  Consumers
        must choose and persist the effective date themselves; the ordinary
        ``fetch_index_components(index_code, dt)`` method remains fail-closed.
        """
        normalized = str(index_code).strip().upper().replace(".SH", "")
        if normalized not in {"000300", "000905"}:
            raise ValueError(f"unsupported index: {index_code}")
        frame = self._call(
            "index_components_current",
            ak.index_stock_cons_weight_csindex,
            normalized,
        )
        if not isinstance(frame, pd.DataFrame):
            raise TypeError("index component provider did not return a DataFrame")
        if not {"成分券代码", "成分券名称"}.issubset(frame.columns):
            raise ValueError("index components are missing code/name columns")
        result = frame.rename(columns={
            "成分券代码": "code", "成分券名称": "name", "权重": "weight",
        }).copy()
        if result.empty:
            error = ValueError("current index component provider returned no rows")
            self._record_failure("index_components_current", error)
            raise error
        result["code"] = result["code"].map(self._canonical_code)
        result["source"] = "akshare:index_components_current"
        result["received_at"] = datetime.now(timezone.utc).isoformat()
        result.attrs["source_meta"] = {
            "source": "akshare:index_components_current",
            "received_at": result["received_at"].iloc[0] if not result.empty else datetime.now(timezone.utc).isoformat(),
            "index_code": normalized,
            "status": "ok",
            "as_of": None,
        }
        self._record_success("index_components_current")
        return result

    def _call(self, operation: str, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run an AKShare call with a deadline without blocking process exit."""
        done = Event()
        result: dict[str, Any] = {}
        with self._inflight_lock:
            if self._closed:
                raise RuntimeError("AKShare adapter is closed")
            previous = self._inflight.get(operation)
            if previous is not None and not previous.is_set():
                raise TimeoutError(f"AKShare {operation} request already in flight")
            self._inflight[operation] = done

        def worker() -> None:
            try:
                result["value"] = func(*args, **kwargs)
            except Exception as exc:  # re-raise in caller thread
                result["error"] = exc
            finally:
                done.set()
                with self._inflight_lock:
                    if self._inflight.get(operation) is done:
                        self._inflight.pop(operation, None)

        Thread(target=worker, name=f"akshare-{operation}", daemon=True).start()
        if not done.wait(self._timeout_seconds):
            raise TimeoutError(f"AKShare {operation} timed out after {self._timeout_seconds:.1f}s")
        if "error" in result:
            raise result["error"]
        return result.get("value")

    @staticmethod
    def _symbol(code: str) -> str:
        return AKShareAdapter._canonical_code(code).split(".", 1)[0]

    @classmethod
    def _canonical_code(cls, code: str) -> str:
        value = str(code).strip().upper()
        if "." in value:
            symbol, exchange = value.rsplit(".", 1)
            exchange = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get(exchange, exchange)
        else:
            symbol = value
            exchange = "BJ" if symbol.startswith(("4", "8", "92")) else "SH" if symbol.startswith(("6", "9")) else "SZ"
        if len(symbol) != 6 or not symbol.isdigit():
            raise ValueError(f"invalid A-share code: {code}")
        if exchange not in {"SH", "SZ", "BJ"}:
            raise ValueError(f"unsupported exchange suffix: {code}")
        if symbol.startswith(("4", "8", "92")):
            expected = "BJ"
        elif symbol.startswith(("6", "9")):
            expected = "SH"
        elif symbol.startswith(("0", "2", "3")):
            expected = "SZ"
        else:
            raise ValueError(f"unsupported A-share code prefix: {code}")
        if exchange != expected:
            raise ValueError(f"exchange suffix conflicts with code prefix: {code}")
        return f"{symbol}.{exchange}"

    def _set_attempts(self, attempts: list[dict[str, Any]]) -> None:
        with self._lock:
            self._last_attempts = [dict(item) for item in attempts]

    def _record_success(self, operation: str, attempts: list[dict[str, Any]] | None = None) -> None:
        stamp = datetime.now(timezone.utc).isoformat()
        with self._lock:
            if attempts is not None:
                self._last_attempts = [dict(item) for item in attempts]
            self._health.update(
                status="ok", last_attempt_at=stamp, last_success_at=stamp,
                last_error=None, last_operation=operation,
            )

    def _record_failure(
        self,
        operation: str,
        error: Exception,
        attempts: list[dict[str, Any]] | None = None,
    ) -> None:
        stamp = datetime.now(timezone.utc).isoformat()
        with self._lock:
            if attempts is not None:
                self._last_attempts = [dict(item) for item in attempts]
            self._health.update(
                status="unavailable", last_attempt_at=stamp,
                last_error=f"{type(error).__name__}: {error}",
                last_operation=operation,
            )
