"""数据存储层

Parquet 存行情/财务数据，SQLite 存元数据。
raw 目录写入后不可修改（immutable）。
"""
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional
import sqlite3
import os

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


@dataclass
class StockInfo:
    code: str
    name: str
    exchange: str          # "SSE" | "SZSE"
    board: str             # "主板" | "创业板" | "科创板"
    listed_date: date
    delisted_date: Optional[date] = None


class PriceStore:
    """日线行情 Parquet 存储

    目录结构: data/raw/daily/year=YYYY/quarter=Q/CODE.parquet
    """

    def __init__(self, base_dir: str | None = None):
        root = base_dir or os.getenv("QUANT_DATA_DIR", "data")
        self._base = Path(root).resolve() / "raw" / "daily"

    def write(self, code: str, df: pd.DataFrame):
        """写入日线数据 (追加模式, 覆盖已存在的日期)"""
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        df["year"] = df["date"].dt.year
        df["quarter"] = df["date"].dt.quarter

        for (year, quarter), group in df.groupby(["year", "quarter"]):
            out_dir = self._base / f"year={year}" / f"quarter={quarter}"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{code}.parquet"

            if out_path.exists():
                existing = pd.read_parquet(out_path)
                existing["date"] = pd.to_datetime(existing["date"])
                existing = existing[
                    ~existing["date"].isin(group["date"])
                ]
                combined = pd.concat([existing, group], ignore_index=True)
            else:
                combined = group

            combined = combined.drop(columns=["year", "quarter"])
            combined = combined.sort_values("date")
            combined.to_parquet(out_path, index=False)

    def read(self, code: str, dt: date, field: str) -> float:
        """读取单日单字段"""
        year, quarter = dt.year, (dt.month - 1) // 3 + 1
        path = (
            self._base / f"year={year}" / f"quarter={quarter}" /
            f"{code}.parquet"
        )
        if not path.exists():
            raise KeyError(
                f"No data for {code} on {dt} "
                f"(file not found: {path})"
            )

        df = pd.read_parquet(path, filters=[
            ("date", "=", pd.Timestamp(dt))
        ])
        if len(df) == 0:
            raise KeyError(f"No data for {code} on {dt}")
        return float(df[field].iloc[0])

    def read_range(
        self,
        codes: list[str],
        start: date,
        end: date,
        fields: list[str],
    ) -> pd.DataFrame:
        """批量读取多只股票的区间数据

        Returns:
            MultiIndex DataFrame (code, date) x fields
        """
        dfs = []
        for code in codes:
            parts = []
            for year in range(start.year, end.year + 1):
                for quarter in range(1, 5):
                    path = (
                        self._base / f"year={year}" /
                        f"quarter={quarter}" / f"{code}.parquet"
                    )
                    if path.exists():
                        part = pd.read_parquet(path)
                        part["date"] = pd.to_datetime(part["date"])
                        mask = (
                            (part["date"] >= pd.Timestamp(start)) &
                            (part["date"] <= pd.Timestamp(end))
                        )
                        parts.append(part[mask])
            if parts:
                df = pd.concat(parts)
                df["code"] = code
                dfs.append(df[["code", "date"] + fields])

        if not dfs:
            empty = pd.DataFrame(columns=fields, index=pd.MultiIndex.from_arrays(
                [[], []], names=["code", "date"]
            ))
            return empty

        result = pd.concat(dfs, ignore_index=True)
        result["date"] = pd.to_datetime(result["date"]).dt.date
        return result.set_index(["code", "date"]).sort_index()


class AdjustStore:
    """复权因子 / 送转 / 分红事件存储"""

    def __init__(self, base_dir: str | None = None):
        root = base_dir or os.getenv("QUANT_DATA_DIR", "data")
        self._base = Path(root) / "raw" / "adjust"

    def read_adjust_factors(self, code: str) -> pd.DataFrame:
        path = self._base / "adjust_factor.parquet"
        if not path.exists():
            return pd.DataFrame(columns=["date", "factor"])
        df = pd.read_parquet(path)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        code_df = df[df["code"] == code].copy()
        return code_df.sort_values("date")

    def write_adjust_factors(self, df: pd.DataFrame):
        self._base.mkdir(parents=True, exist_ok=True)
        path = self._base / "adjust_factor.parquet"
        df.to_parquet(path, index=False)

    def read_splits(self, code: str) -> pd.DataFrame:
        path = self._base / "splits.parquet"
        if not path.exists():
            return pd.DataFrame()
        df = pd.read_parquet(path)
        return df[df["code"] == code]

    def read_dividends(self, code: str) -> pd.DataFrame:
        path = self._base / "dividends.parquet"
        if not path.exists():
            return pd.DataFrame()
        df = pd.read_parquet(path)
        return df[df["code"] == code]


class MetaDB:
    """SQLite 元数据库"""

    def __init__(self, db_path: str | None = None):
        root = Path(os.getenv("QUANT_DATA_DIR", "data"))
        self._db_path = Path(db_path) if db_path else root / "meta.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_tables()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS stock_info (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    board TEXT NOT NULL,
                    listed_date TEXT,
                    delisted_date TEXT
                );
                CREATE TABLE IF NOT EXISTS data_versions (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS data_update_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    last_update TEXT DEFAULT (datetime('now')),
                    record_count INTEGER,
                    status TEXT
                );
            """)

    def upsert_stock(self, info: StockInfo):
        with self._connect() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO stock_info
                (code, name, exchange, board, listed_date, delisted_date)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                info.code, info.name, info.exchange, info.board,
                info.listed_date.isoformat() if info.listed_date else None,
                info.delisted_date.isoformat() if info.delisted_date else None,
            ))

    def _row_to_stockinfo(self, row) -> StockInfo:
        return StockInfo(
            code=row["code"], name=row["name"],
            exchange=row["exchange"], board=row["board"],
            listed_date=date.fromisoformat(row["listed_date"])
                if row["listed_date"] else None,
            delisted_date=date.fromisoformat(row["delisted_date"])
                if row["delisted_date"] else None,
        )

    def get_stock(self, code: str) -> Optional[StockInfo]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM stock_info WHERE code = ?", (code,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_stockinfo(row)

    def get_all_stocks(self) -> list[StockInfo]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM stock_info").fetchall()
        return [self._row_to_stockinfo(row) for row in rows]

    def set_data_version(self, key: str, value: str):
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO data_versions (key, value, updated_at) "
                "VALUES (?, ?, datetime('now'))", (key, value)
            )

    def get_data_version(self, key: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM data_versions WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def log_update(self, source: str, record_count: int, status: str):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO data_update_log (source, record_count, status) "
                "VALUES (?, ?, ?)", (source, record_count, status)
            )

    def get_last_update(self, source: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM data_update_log WHERE source = ? "
                "ORDER BY id DESC LIMIT 1", (source,)
            ).fetchone()
        return dict(row) if row else None
