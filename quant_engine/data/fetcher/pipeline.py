"""数据管道: Fetch -> Clean -> Validate -> Write"""
from datetime import date
import logging

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
        df = self.source.fetch_stock_list()

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

        self.meta.log_update("stock_list", count, "success")
        logger.info("Synced %d stocks", count)

    def sync_daily(self, codes: list[str], start: date, end: date):
        logger.info(
            "Fetching daily data for %d codes from %s to %s",
            len(codes), start, end
        )
        raw = self.source.fetch_daily(codes, start, end)
        if raw.empty:
            logger.warning("No daily data returned")
            return

        cleaned = self._clean(raw)
        self._validate(cleaned)

        for code, group in cleaned.groupby("code"):
            cols = {
                "date", "open", "high", "low", "close", "volume",
                "amount", "turnover_rate",
            }
            available = list(cols & set(group.columns))
            if "date" not in available:
                available.append("date")
            self.store.write(code, group[available])

        self.meta.log_update("daily", len(cleaned), "success")
        logger.info("Synced %d daily records", len(cleaned))

    def _clean(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.drop_duplicates(subset=["code", "date"])
        price_cols = ["open", "high", "low", "close"]
        available = [c for c in price_cols if c in df.columns]
        if available:
            df = df.dropna(subset=available, how="all")
        return df

    def _validate(self, df: pd.DataFrame):
        if "close" in df.columns:
            neg = (df["close"] <= 0).sum()
            if neg > 0:
                logger.warning("Found %d rows with close <= 0", neg)
        if "high" in df.columns and "low" in df.columns:
            wrong = (df["high"] < df["low"]).sum()
            if wrong > 0:
                logger.warning("Found %d rows with high < low", wrong)
