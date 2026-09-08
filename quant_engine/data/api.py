"""DataAPI — 统一数据查询接口

单例模式。上层代码只通过这个类访问数据，物理存储细节对外透明。
"""
from datetime import date
import hashlib
import json
import math
from typing import Optional

import pandas as pd

from quant_engine.data.calendar import TradingCalendar
from quant_engine.data.adjust import AdjustHandler
from quant_engine.data.store import PriceStore, MetaDB


class DataAPI:
    _instance: Optional["DataAPI"] = None

    def __new__(cls) -> "DataAPI":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._calendar = TradingCalendar()
        self._adjust = AdjustHandler(self._calendar)
        self._price_store = PriceStore()
        self._meta = MetaDB()
        self._initialized = True

    def get_trading_dates(self, start: date, end: date) -> list[date]:
        return self._calendar.get_trading_days(start, end)

    def is_trading_day(self, d: date) -> bool:
        return self._calendar.is_trading_day(d)

    def daily(
        self,
        codes: list[str],
        start: date,
        end: date,
        fields: Optional[list[str]] = None,
        adjust: str = "event_driven",
    ) -> pd.DataFrame:
        if fields is None:
            fields = ["open", "high", "low", "close", "volume"]

        df = self._price_store.read_range(codes, start, end, fields)

        if adjust == "none":
            return df
        if adjust == "event_driven":
            return self._apply_event_driven_adjust(df, fields)
        raise ValueError(f"Unknown adjust method: {adjust}")

    def daily_inventory(self) -> list[dict]:
        """Return the physical local daily-file inventory."""
        return self._price_store.inventory()

    def daily_coverage(
        self,
        codes: list[str],
        start: date,
        end: date,
        fields: Optional[list[str]] = None,
        adjust: str = "none",
        calendar: Optional[TradingCalendar] = None,
    ) -> dict:
        """Return an auditable coverage report for the local daily store.

        The report compares observed Parquet rows with the trading calendar
        and never treats the requested date range or security-master count as
        proof of data availability.  It is intentionally read-only and
        suitable for attaching to a backtest manifest.
        """
        if start > end:
            raise ValueError("start must not be after end")
        if adjust not in {"none", "event_driven"}:
            raise ValueError(f"Unknown adjust method: {adjust}")
        requested_codes = sorted(dict.fromkeys(
            str(code).strip().upper() for code in codes if str(code).strip()
        ))
        selected_fields = list(dict.fromkeys(fields or ["open", "high", "low", "close", "volume"]))
        # Execution constraints change fills even when OHLCV is identical.
        # Bind optional rule columns into the data identity without treating
        # their absence as verified historical exchange-rule coverage.
        execution_fields = ["up_limit", "down_limit", "is_suspended"]
        identity_fields = list(dict.fromkeys(selected_fields + execution_fields))
        # Backtests pass the exact calendar instance that drives their event
        # loop.  Falling back to the API singleton is retained for the
        # read-only HTTP report, but execution evidence must never be based on
        # a second potentially stale calendar cache.
        coverage_calendar = calendar or self._calendar
        calendar_evidence = coverage_calendar.coverage_report(start, end)
        trading_days = coverage_calendar.get_trading_days(start, end)
        expected = set(trading_days)
        items: list[dict] = []
        for code in requested_codes:
            read_error: Optional[str] = None
            try:
                frame = self._price_store.read_range([code], start, end, identity_fields)
                if adjust == "event_driven":
                    frame = self._apply_event_driven_adjust(frame, identity_fields)
            except Exception as exc:
                frame = pd.DataFrame(columns=selected_fields)
                read_error = f"{type(exc).__name__}: {exc}"
            observed_dates: list[date] = []
            if not frame.empty:
                raw_dates = frame.index.get_level_values("date") if "date" in frame.index.names else []
                for raw in raw_dates:
                    try:
                        observed_dates.append(pd.Timestamp(raw).date())
                    except (TypeError, ValueError):
                        continue
            observed_set = set(observed_dates) & expected
            unexpected_dates = sorted(set(observed_dates) - expected)
            duplicate_rows = max(0, len(observed_dates) - len(set(observed_dates)))
            field_valid_counts: dict[str, int] = {}
            invalid_field_rows = 0
            numeric_fields: dict[str, pd.Series] = {}
            valid_fields: dict[str, pd.Series] = {}
            for field in selected_fields:
                if field not in frame.columns:
                    valid_fields[field] = pd.Series(False, index=frame.index)
                    continue
                numeric = pd.to_numeric(frame[field], errors="coerce")
                numeric_fields[field] = numeric
                valid = numeric.notna() & numeric.map(lambda value: bool(pd.notna(value) and math.isfinite(float(value))))
                if field in {"open", "high", "low", "close"}:
                    valid &= numeric > 0
                elif field in {"volume", "amount", "turnover_rate"}:
                    valid &= numeric >= 0
                valid_fields[field] = valid
            # Finite prices alone are not valid bars. Check every available
            # pair so projected field requests retain their own guarantees.
            for lower, upper in (("low", "high"), ("low", "open"),
                                 ("low", "close"), ("open", "high"), ("close", "high")):
                if lower in numeric_fields and upper in numeric_fields:
                    ordered = numeric_fields[lower] <= numeric_fields[upper]
                    valid_fields[lower] &= ordered
                    valid_fields[upper] &= ordered
            field_valid_counts = {field: int(valid.sum()) for field, valid in valid_fields.items()}
            if selected_fields and not frame.empty:
                invalid_field_rows = int((~pd.concat(valid_fields.values(), axis=1).all(axis=1)).sum())
            missing_dates = sorted(expected - observed_set)
            content_hash = None
            if read_error is None and not frame.empty:
                # Hash canonical rows rather than filesystem bytes.  This
                # remains stable across Parquet metadata/compression changes
                # while changing whenever the values, dates, code or selected
                # adjustment surface changes.
                canonical = frame.reset_index()
                canonical["date"] = pd.to_datetime(canonical["date"], errors="coerce").dt.strftime("%Y-%m-%d")
                ordered = ["code", "date"] + identity_fields
                for field in identity_fields:
                    if field in canonical.columns:
                        # Preserve suspension value types: the consumer must
                        # not get identical evidence for integer 0 and string
                        # "0", which have different truth semantics.
                        values = (canonical[field] if field == "is_suspended"
                                  else pd.to_numeric(canonical[field], errors="coerce"))
                        canonical[field] = values.astype(object).where(values.notna(), None)
                records = canonical[[field for field in ordered if field in canonical.columns]].sort_values(
                    [field for field in ("code", "date") if field in canonical.columns]
                ).to_dict("records")
                encoded = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
                content_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            if not expected:
                status = "no_trading_days"
            elif (
                not frame.empty
                and not missing_dates
                and not unexpected_dates
                and duplicate_rows == 0
                and invalid_field_rows == 0
            ):
                status = "complete"
            elif frame.empty:
                status = "empty"
            else:
                status = "partial"
            items.append({
                "code": code,
                "status": status,
                "expected_trading_days": len(trading_days),
                "observed_trading_days": len(observed_set),
                "missing_count": len(missing_dates),
                "missing_dates": [value.isoformat() for value in missing_dates[:200]],
                "missing_dates_truncated": len(missing_dates) > 200,
                "unexpected_count": len(unexpected_dates),
                "unexpected_dates": [value.isoformat() for value in unexpected_dates[:200]],
                "unexpected_dates_truncated": len(unexpected_dates) > 200,
                "duplicate_rows": duplicate_rows,
                "invalid_field_rows": invalid_field_rows,
                "field_valid_counts": field_valid_counts,
                "execution_field_counts": {
                    field: int(frame[field].notna().sum()) if field in frame.columns else 0
                    for field in execution_fields
                },
                "first_observed": min(observed_set).isoformat() if observed_set else None,
                "last_observed": max(observed_set).isoformat() if observed_set else None,
                "read_error": read_error,
                "content_hash": content_hash,
            })
        payload = {
            "coverage_version": "daily-coverage-v1",
            "market": "a-share",
            "source": "local:parquet",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "expected_trading_days": len(trading_days),
            "requested_codes": requested_codes,
            "adjust": adjust,
            "calendar_version": calendar_evidence.get("calendar_version"),
            "calendar_source": calendar_evidence.get("source"),
            "calendar_content_hash": calendar_evidence.get("content_hash"),
            "calendar_coverage_start": calendar_evidence.get("coverage_start"),
            "calendar_coverage_end": calendar_evidence.get("coverage_end"),
            "calendar_verified": bool(calendar_evidence.get("verified")),
            "calendar_complete": bool(calendar_evidence.get("complete")),
            "items": items,
        }
        dataset_identity = [
            {"code": item["code"], "content_hash": item.get("content_hash")}
            for item in items
        ]
        dataset_identity.sort(key=lambda item: item["code"])
        dataset_encoded = json.dumps(
            {
                "coverage_version": payload["coverage_version"],
                "market": payload["market"],
                "source": payload["source"],
                "start_date": payload["start_date"],
                "end_date": payload["end_date"],
                "adjust": adjust,
                "items": dataset_identity,
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        payload["dataset_hash"] = hashlib.sha256(dataset_encoded.encode("utf-8")).hexdigest()
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {
            **payload,
            "complete": (
                bool(calendar_evidence.get("complete"))
                and bool(trading_days)
                and bool(items)
                and all(item["status"] == "complete" for item in items)
            ),
            "coverage_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        }

    @staticmethod
    def daily_dataset_hash(report: dict) -> str:
        """Recompute the content identity carried by a coverage report."""
        items = report.get("items") if isinstance(report, dict) else None
        identity = [
            {"code": item.get("code"), "content_hash": item.get("content_hash")}
            for item in items or [] if isinstance(item, dict)
        ]
        identity.sort(key=lambda item: str(item.get("code") or ""))
        encoded = json.dumps(
            {
                "coverage_version": report.get("coverage_version"),
                "market": report.get("market"),
                "source": report.get("source"),
                "start_date": report.get("start_date"),
                "end_date": report.get("end_date"),
                "adjust": report.get("adjust", "none"),
                "items": identity,
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def daily_coverage_hash(report: dict) -> str:
        """Recompute evidence identity without derived/HTTP presentation fields.

        The HTTP route appends ``meta`` after the report is hashed. Its
        explanatory labels are not execution evidence; dates, calendar,
        per-security checks and data identity remain covered by this hash.
        """
        payload = {
            key: value for key, value in report.items()
            if key not in {"complete", "coverage_hash", "meta"}
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _apply_event_driven_adjust(
        self, df: pd.DataFrame, fields: list[str]
    ) -> pd.DataFrame:
        price_fields = {"open", "high", "low", "close", "up_limit", "down_limit"} & set(fields)
        if not price_fields:
            return df

        result = df.copy()
        if result.empty:
            return result

        # Compute one aligned factor vector per instrument.  Avoid scalar .loc
        # writes, which are quadratic enough to make multi-year runs unusable.
        for code in result.index.get_level_values("code").unique():
            mask = result.index.get_level_values("code") == code
            dates = result.index.get_level_values("date")[mask]
            factors = self._adjust.factors_for_dates(code, dates).to_numpy()
            result.loc[mask, list(price_fields)] = (
                result.loc[mask, list(price_fields)].to_numpy() * factors[:, None]
            )
        return result

    def stock_list(self) -> list[dict]:
        stocks = self._meta.get_all_stocks()
        return [
            {
                "code": s.code, "name": s.name,
                "exchange": s.exchange, "board": s.board,
                "listed_date": s.listed_date,
            }
            for s in stocks
        ]

    def index_components(self, index_code: str, dt: date) -> list[str]:
        from quant_engine.data.fetcher.akshare_adapter import HistoricalConstituentsUnavailableError
        rows = self.index_snapshot(index_code, dt)
        if not rows:
            raise HistoricalConstituentsUnavailableError(
                f"no point-in-time index snapshot for {index_code} at {dt}"
            )
        return [row["code"] for row in rows]

    def index_snapshot(self, index_code: str, dt: date) -> list[dict]:
        """Return a dated snapshot, rejecting future as-of queries."""
        if dt > date.today():
            raise ValueError("index snapshot date cannot be in the future")
        return self._meta.get_index_components(index_code, dt)

    def archive_index_components(
        self,
        index_code: str,
        as_of: date,
        rows: list[dict],
        source: str = "manual:index_snapshot",
        received_at: Optional[str] = None,
    ) -> int:
        """Persist a dated constituent snapshot for point-in-time queries."""
        return self._meta.replace_index_components(
            index_code=index_code,
            as_of=as_of,
            rows=rows,
            source=source,
            received_at=received_at,
        )

    def archive_index_components_batch(
        self, snapshots: list[dict], data_versions: Optional[dict[str, str]] = None
    ) -> int:
        """Atomically archive multiple dated index snapshots."""
        return self._meta.replace_index_components_batch(snapshots, data_versions=data_versions)

    def validate_index_components_batch(self, snapshots: list[dict]) -> list[dict]:
        """Validate multiple snapshots without changing storage."""
        return self._meta.validate_index_components_batch(snapshots)

    def index_snapshot_coverage(self, index_code: str) -> list[dict]:
        """Return available dated index snapshot periods."""
        return self._meta.get_index_snapshot_coverage(index_code)

    def industry(self, code: str, dt: date) -> Optional[str]:
        from pathlib import Path
        path = Path("data/raw/industry/sw_industry.parquet")
        if not path.exists():
            return None
        df = pd.read_parquet(path)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        match = df[(df["code"] == code) & (df["date"] <= dt)]
        if match.empty:
            return None
        return match.sort_values("date").iloc[-1]["level1"]

    def fundamentals(
        self,
        codes: list[str],
        report_dates: Optional[list[str]] = None,
        fields: Optional[list[str]] = None,
        as_of: Optional[date] = None,
    ) -> pd.DataFrame:
        """Return point-in-time fundamental observations.

        ``as_of`` is the information date, not the report period.  When it is
        omitted the current date is used for interactive research.  Backtests
        should always pass their simulation date explicitly; the storage layer
        then filters by ``announce_date <= as_of`` before selecting the latest
        observation for each field.
        """
        return self._meta.get_fundamentals(
            codes=codes,
            as_of=as_of or date.today(),
            report_dates=report_dates,
            fields=fields,
        )

    def archive_fundamentals(
        self, rows: list[dict], data_versions: Optional[dict[str, str]] = None
    ) -> int:
        """Atomically persist imported point-in-time fundamental rows."""
        return self._meta.replace_fundamentals_batch(rows, data_versions=data_versions)

    def validate_fundamentals(self, rows: list[dict]) -> dict:
        """Validate fundamental import rows without changing storage."""
        return self._meta.validate_fundamentals(rows)

    def fundamental_coverage(self, code: Optional[str] = None) -> list[dict]:
        """Return imported fundamental periods and field coverage."""
        return self._meta.get_fundamental_coverage(code)
