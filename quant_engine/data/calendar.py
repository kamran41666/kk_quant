"""交易日历模块

A 股交易日历基于中国金融期货交易所发布的交易日历。
使用 AKShare 获取官方日历数据，本地缓存到 Parquet。
"""
import warnings
import os
import hashlib
import json
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd


class TradingCalendar:
    """A 股交易日历"""

    def __init__(self, start_year: int = 2010, end_year: Optional[int] = None):
        self._start_year = start_year
        self._end_year = end_year or datetime.now().year + 1
        self._trading_dates: set[date] = set()
        self._sorted_dates: list[date] = []
        self._source = "unknown:local-cache"
        self._content_hash = ""
        self._data_start: date | None = None
        self._data_end: date | None = None
        self._load()

    def _data_path(self) -> Path:
        configured_root = os.getenv("QUANT_DATA_DIR")
        base = (Path(configured_root) if configured_root else Path(__file__).parent.parent.parent / "data") / "raw" / "calendar"
        base.mkdir(parents=True, exist_ok=True)
        return base / "trading_dates.parquet"

    def _load(self):
        path = self._data_path()
        if path.exists():
            df = pd.read_parquet(path)
        else:
            df = self._fetch_from_akshare()
            fetched_source = str(df["source"].dropna().iloc[0]) if "source" in df.columns and not df["source"].dropna().empty else ""
            # Never persist a weekday approximation.  Besides being
            # unsuitable for research, a fallback writer racing a verified
            # process could otherwise downgrade the shared cache.
            if fetched_source.startswith("akshare:"):
                self._write_cache(path, df)

        df = self._normalize_frame(df)
        sources = sorted({str(item) for item in df["source"].dropna().tolist()})
        if len(sources) == 1:
            self._source = sources[0]
        elif sources:
            self._source = "mixed:" + ",".join(sources)
        else:
            self._source = "unknown:local-cache"
        self._content_hash = self._hash_frame(df)
        self._data_start = min(df["date"].tolist()) if not df.empty else None
        self._data_end = max(df["date"].tolist()) if not df.empty else None

        mask = (df["date"] >= date(self._start_year, 1, 1)) & (
               df["date"] <= date(self._end_year, 12, 31))
        trading = df[mask & df["is_trading_day"]]
        self._trading_dates = set(trading["date"].tolist())
        self._sorted_dates = sorted(self._trading_dates)

    @staticmethod
    def _normalize_frame(df: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(df, pd.DataFrame) or "date" not in df.columns:
            raise ValueError("trading calendar must contain a date column")
        result = df.copy()
        result["date"] = pd.to_datetime(result["date"], errors="coerce").dt.date
        result = result[result["date"].notna()].copy()
        if "is_trading_day" not in result.columns:
            result["is_trading_day"] = True
        result["is_trading_day"] = result["is_trading_day"].astype(bool)
        if "exchange" not in result.columns:
            result["exchange"] = "SSE"
        result["exchange"] = result["exchange"].fillna("SSE").astype(str)
        if "source" not in result.columns:
            result["source"] = "unknown:local-cache"
        result["source"] = result["source"].fillna("unknown:local-cache").astype(str)
        return result[["date", "is_trading_day", "exchange", "source"]].drop_duplicates(
            subset=["date", "exchange"], keep="last"
        ).sort_values(["date", "exchange"]).reset_index(drop=True)

    @staticmethod
    def _hash_frame(df: pd.DataFrame) -> str:
        payload = [
            {
                "date": item["date"].isoformat(),
                "is_trading_day": bool(item["is_trading_day"]),
                "exchange": str(item["exchange"]),
                "source": str(item["source"]),
            }
            for item in df.to_dict("records")
        ]
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _write_cache(path: Path, df: pd.DataFrame) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        normalized = TradingCalendar._normalize_frame(df)
        with tempfile.NamedTemporaryFile(prefix="trading-calendar-", suffix=".parquet", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
        try:
            normalized.to_parquet(temporary, index=False)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _fetch_from_akshare(self) -> pd.DataFrame:
        import akshare as ak
        try:
            df = ak.tool_trade_date_hist_sina()
            df = df.rename(columns={"trade_date": "date"})
            df["date"] = pd.to_datetime(df["date"]).dt.date
            df["is_trading_day"] = True
            df["exchange"] = "SSE"
            df["source"] = "akshare:sina_trade_calendar"
            return df[["date", "is_trading_day", "exchange", "source"]]
        except Exception:
            warnings.warn(
                "AKShare 交易日历获取失败，回退到工作日历（不含节假日修正），"
                "请尽快手动更新交易日历"
            )
            dates = pd.date_range(
                f"{self._start_year}-01-01", f"{self._end_year}-12-31", freq="B"
            )
            return pd.DataFrame({
                "date": dates.date,
                "is_trading_day": True,
                "exchange": "SSE",
                "source": "fallback:business-days",
            })

    def coverage_report(self, start: date, end: date) -> dict:
        """Describe calendar coverage without treating weekdays as holidays.

        ``complete`` is deliberately false for an unverified/fallback calendar;
        research and production backtests must not silently use a workday
        approximation as an exchange calendar.
        """
        if start > end:
            raise ValueError("calendar coverage start must not be after end")
        # Bounds describe the dated source file, not the first/last *trading*
        # session in the requested year.  A range may legitimately begin on a
        # weekend or holiday and still be fully covered by the calendar.
        coverage_start = self._data_start
        coverage_end = self._data_end
        bounds_complete = bool(
            coverage_start is not None and coverage_end is not None
            and coverage_start <= start and coverage_end >= end
        )
        verified = self._source.startswith("akshare:")
        return {
            "calendar_version": "trading-calendar-v1",
            "source": self._source,
            "content_hash": self._content_hash,
            "coverage_start": coverage_start.isoformat() if coverage_start else None,
            "coverage_end": coverage_end.isoformat() if coverage_end else None,
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "trading_days": [day.isoformat() for day in self.get_trading_days(start, end)],
            "verified": verified,
            "complete": bounds_complete and verified,
        }

    def ensure_coverage(self, start: date, end: date) -> dict:
        """Refresh an unverified/stale cache and return a strict report.

        A failed refresh leaves the existing cache untouched and returns its
        evidence, allowing the caller to fail closed with an actionable error.
        """
        report = self.coverage_report(start, end)
        if report["complete"]:
            return report
        try:
            fetched = self._fetch_from_akshare()
            normalized = self._normalize_frame(fetched)
            fetched_source = str(normalized["source"].iloc[0]) if not normalized.empty else ""
            if fetched_source.startswith("akshare:"):
                self._write_cache(self._data_path(), normalized)
                self._load()
                report = self.coverage_report(start, end)
        except Exception as exc:
            report["refresh_error"] = f"{type(exc).__name__}: {exc}"
        return report

    def is_trading_day(self, d: date) -> bool:
        return d in self._trading_dates

    def get_trading_days(self, start: date, end: date) -> list[date]:
        return [d for d in self._sorted_dates if start <= d <= end]

    def next_trading_day(self, d: date) -> date:
        for td in self._sorted_dates:
            if td > d:
                return td
        raise ValueError(f"No trading day after {d}")

    def prev_trading_day(self, d: date) -> date:
        prev = None
        for td in self._sorted_dates:
            if td >= d:
                break
            prev = td
        if prev is None:
            raise ValueError(f"No trading day before {d}")
        return prev

    def count_trading_days(self, start: date, end: date) -> int:
        return len(self.get_trading_days(start, end))

    def nth_trading_day_of_month(self, year: int, month: int, n: int) -> date:
        days = [d for d in self._sorted_dates
                if d.year == year and d.month == month]
        if n == -1:
            return days[-1]
        return days[n - 1]

    @property
    def all_dates(self) -> list[date]:
        return list(self._sorted_dates)

    @property
    def source(self) -> str:
        return self._source

    @property
    def content_hash(self) -> str:
        return self._content_hash
