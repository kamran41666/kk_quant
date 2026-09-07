"""数据管道: Fetch -> Clean -> Validate -> Write"""
from datetime import date
import logging

import numpy as np
import pandas as pd

from quant_engine.data.fetcher.base import DataSource
from quant_engine.data.store import PriceStore, MetaDB, StockInfo

logger = logging.getLogger(__name__)


class DataPipeline:
    def __init__(self, source: DataSource, store: PriceStore, meta: MetaDB):
        self.source = source
        self.store = store
        self.meta = meta

    def sync_stock_list(self):
        logger.info("Fetching stock list from %s...", self.source.source_name)
        try:
            df = self.source.fetch_stock_list()
        except Exception:
            self.meta.log_update(self.source.source_name, 0, "failed")
            raise

        source_meta = dict(getattr(df, "attrs", {}).get("source_meta", {}))
        source = source_meta.get("source", self.source.source_name)
        if df.empty or source_meta.get("status") == "failed":
            self.meta.log_update(source, 0, "failed")
            logger.warning("No usable stock list returned")
            return

        count = 0
        for _, row in df.iterrows():
            code = row.get("full_code", row["code"])
            if not code:
                continue
            self.meta.upsert_stock(StockInfo(
                code=code,
                name=row.get("name", "Unknown"),
                exchange=row.get("exchange", "SZSE"),
                board="主板",
                listed_date=date(2000, 1, 1),
                delisted_date=None,
            ))
            count += 1

        status = source_meta.get("status", "success")
        self.meta.log_update(source, count, status)
        logger.info("Synced %d stocks", count)

    def sync_daily(self, codes: list[str], start: date, end: date):
        logger.info(
            "Fetching daily data for %d codes from %s to %s",
            len(codes), start, end
        )
        try:
            raw = self.source.fetch_daily(codes, start, end)
        except Exception:
            self.meta.log_update(self.source.source_name, 0, "failed")
            raise
        source_meta = dict(getattr(raw, "attrs", {}).get("source_meta", {}))
        source = source_meta.get("source", self.source.source_name)
        batch_status = source_meta.get("status")
        if batch_status == "failed":
            self.meta.log_update(source, 0, "failed")
            logger.warning("Daily source marked batch as failed; refusing to write rows")
            return
        required = {"code", "date", "open", "high", "low", "close", "volume"}
        missing = sorted(required - set(raw.columns))
        if missing:
            self.meta.log_update(source, 0, "failed")
            raise ValueError("daily data is missing required columns: " + ", ".join(missing))
        if raw.empty:
            logger.warning("No daily data returned")
            self.meta.log_update(source, 0, batch_status or "failed")
            return

        try:
            cleaned = self._clean(raw)
            self._validate(cleaned)
        except Exception:
            self.meta.log_update(source, 0, "failed")
            raise

        batches: dict[str, pd.DataFrame] = {}
        for code, group in cleaned.groupby("code"):
            cols = {
                "date", "open", "high", "low", "close", "volume",
                "amount", "turnover_rate",
            }
            available = list(cols & set(group.columns))
            if "date" not in available:
                available.append("date")
            batches[code] = group[available]

        try:
            writer = getattr(self.store, "write_batch", None)
            if writer is not None:
                writer(batches)
            else:
                for code, frame in batches.items():
                    self.store.write(code, frame)
        except Exception:
            self.meta.log_update(source, 0, "failed")
            raise

        # Preserve partial/failed state from the adapter.  A partial batch may
        # still be useful for cache warming, but it must never be recorded as a
        # complete success that a backtest can silently trust.
        self.meta.log_update(source, len(cleaned), batch_status or "success")
        logger.info("Synced %d daily records", len(cleaned))

    def _clean(self, df: pd.DataFrame) -> pd.DataFrame:
        if {"code", "date"}.issubset(df.columns):
            duplicates = int(df.duplicated(subset=["code", "date"]).sum())
            if duplicates:
                raise ValueError(f"duplicate code/date rows: {duplicates}")
        df = df.copy()
        price_cols = ["open", "high", "low", "close"]
        available = [c for c in price_cols if c in df.columns]
        if available:
            df = df.dropna(subset=available, how="all")
        return df

    def _validate(self, df: pd.DataFrame):
        violations = []
        required_numeric = [column for column in ("open", "high", "low", "close", "volume") if column in df.columns]
        for column in required_numeric:
            values = pd.to_numeric(df[column], errors="coerce")
            invalid = int((values.isna() | ~np.isfinite(values)).sum())
            if invalid:
                violations.append(f"{column} non-numeric ({invalid})")
            df[column] = values
        if "close" in df.columns:
            neg = (df["close"] <= 0).sum()
            if neg > 0:
                violations.append(f"close<=0 ({neg})")
        if "high" in df.columns and "low" in df.columns:
            wrong = (df["high"] < df["low"]).sum()
            if wrong > 0:
                violations.append(f"high<low ({wrong})")
            if "open" in df.columns:
                wrong_open = ((df["high"] < df["open"]) | (df["low"] > df["open"])).sum()
                if wrong_open > 0:
                    violations.append(f"open outside high/low ({wrong_open})")
            if "close" in df.columns:
                wrong_close = ((df["high"] < df["close"]) | (df["low"] > df["close"])).sum()
                if wrong_close > 0:
                    violations.append(f"close outside high/low ({wrong_close})")
        if "volume" in df.columns:
            negative_volume = (df["volume"] < 0).sum()
            if negative_volume > 0:
                violations.append(f"volume<0 ({negative_volume})")
        if {"code", "date"}.issubset(df.columns):
            duplicate_rows = int(df.duplicated(subset=["code", "date"]).sum())
            if duplicate_rows:
                violations.append(f"duplicate code/date ({duplicate_rows})")
        if violations:
            raise ValueError("invalid daily data: " + ", ".join(violations))
