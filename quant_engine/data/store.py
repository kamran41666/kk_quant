"""数据存储层

Parquet 存行情/财务数据，SQLite 存元数据。
raw 目录写入后不可修改（immutable）。
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
import sqlite3
import os
import shutil
import tempfile
import math
import re

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
        self.write_batch({code: df})

    def write_batch(self, frames: dict[str, pd.DataFrame]) -> None:
        """Atomically append/replace several securities.

        All affected parquet files are rendered in a temporary directory
        first.  Existing files are backed up before replacement, and a failed
        replacement restores the previous set.  This prevents a multi-code
        sync from exposing a half-written batch to a later backtest.
        """
        prepared: dict[Path, pd.DataFrame] = {}
        for code, raw in frames.items():
            if raw is None or raw.empty:
                continue
            df = raw.copy()
            required = {"date", "open", "high", "low", "close", "volume"}
            missing = sorted(required - set(df.columns))
            if missing:
                raise ValueError(
                    f"daily frame for {code} is missing required columns: {', '.join(missing)}"
                )
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            if df["date"].isna().any():
                raise ValueError(f"daily frame for {code} contains invalid dates")
            df["year"] = df["date"].dt.year
            df["quarter"] = df["date"].dt.quarter
            for (year, quarter), group in df.groupby(["year", "quarter"], sort=True):
                out_path = self._base / f"year={year}" / f"quarter={quarter}" / f"{code}.parquet"
                group = group.drop(columns=["year", "quarter"])
                if out_path.exists():
                    existing = pd.read_parquet(out_path)
                    existing["date"] = pd.to_datetime(existing["date"])
                    existing = existing[~existing["date"].isin(group["date"])]
                    group = pd.concat([existing, group], ignore_index=True)
                prepared[out_path] = group.sort_values("date").reset_index(drop=True)

        if not prepared:
            return
        self._base.parent.mkdir(parents=True, exist_ok=True)
        temp_root = Path(tempfile.mkdtemp(prefix="daily-batch-", dir=str(self._base.parent)))
        backup_root = temp_root / "backup"
        staged: list[tuple[Path, Path]] = []
        backups: dict[Path, Path] = {}
        replaced: list[Path] = []
        try:
            for target, frame in prepared.items():
                relative = target.relative_to(self._base)
                staged_path = temp_root / "staged" / relative
                staged_path.parent.mkdir(parents=True, exist_ok=True)
                frame.to_parquet(staged_path, index=False)
                staged.append((target, staged_path))
            for target, _ in staged:
                if target.exists():
                    backup = backup_root / target.relative_to(self._base)
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup)
                    backups[target] = backup
            for target, staged_path in staged:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged_path, target)
                replaced.append(target)
        except Exception:
            for target in reversed(replaced):
                backup = backups.get(target)
                if backup and backup.exists():
                    os.replace(backup, target)
                elif target.exists():
                    target.unlink()
            raise
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

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
                # Execution metadata is optional in older OHLCV archives.
                # Preserve any recorded values and represent absent evidence
                # explicitly; required research fields remain strict below.
                for field in set(fields) & {"up_limit", "down_limit", "is_suspended"}:
                    if field not in df.columns:
                        df[field] = float("nan")
                dfs.append(df[["code", "date"] + fields])

        if not dfs:
            empty = pd.DataFrame(columns=fields, index=pd.MultiIndex.from_arrays(
                [[], []], names=["code", "date"]
            ))
            return empty

        result = pd.concat(dfs, ignore_index=True)
        result["date"] = pd.to_datetime(result["date"]).dt.date
        return result.set_index(["code", "date"]).sort_index()

    def inventory(self) -> list[dict]:
        """Inspect the daily Parquet files that physically exist in this store.

        A file only contributes rows and dates after its ``date`` column has
        been read and validated.  Unreadable files remain visible in the file
        count and carry an error, so this inventory cannot be mistaken for
        proof of complete market coverage.
        """
        by_code: dict[str, dict] = {}
        for path in sorted(self._base.glob("year=*/quarter=*/*.parquet")):
            code = path.stem
            item = by_code.setdefault(code, {
                "code": code,
                "file_count": 0,
                "row_count": 0,
                "start_date": None,
                "end_date": None,
                "read_errors": [],
            })
            item["file_count"] += 1
            try:
                parquet_file = pq.ParquetFile(path)
                dates = parquet_file.read(columns=["date"]).column("date").to_pandas()
                parsed = pd.to_datetime(dates, errors="coerce")
                if parsed.isna().any():
                    raise ValueError("date column contains null or invalid values")
                item["row_count"] += int(parquet_file.metadata.num_rows)
                if len(parsed):
                    file_start = parsed.min().date().isoformat()
                    file_end = parsed.max().date().isoformat()
                    if item["start_date"] is None or file_start < item["start_date"]:
                        item["start_date"] = file_start
                    if item["end_date"] is None or file_end > item["end_date"]:
                        item["end_date"] = file_end
            except Exception as exc:  # noqa: BLE001 - corrupt Parquet may raise several backend errors
                relative = path.relative_to(self._base).as_posix()
                item["read_errors"].append(f"{relative}: {type(exc).__name__}: {exc}")
        return [by_code[code] for code in sorted(by_code)]


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

    def _migrate_index_components_schema(self, conn: sqlite3.Connection) -> None:
        """Preserve and isolate an incompatible legacy snapshot table."""
        table_info = conn.execute("PRAGMA table_info(index_components)").fetchall()
        columns = {row["name"] for row in table_info}
        column_types = {
            row["name"]: str(row["type"] or "").upper()
            for row in table_info
        }
        not_null = {
            row["name"] for row in table_info
            if row["notnull"] or row["pk"]
        }
        primary_key = [
            row["name"]
            for row in sorted(
                conn.execute("PRAGMA table_info(index_components)").fetchall(),
                key=lambda item: item["pk"],
            )
            if row["pk"]
        ]
        required = {
            "index_code", "as_of", "code", "name", "weight", "source", "received_at",
        }
        required_not_null = {"index_code", "as_of", "code", "source", "received_at"}
        expected_types = {
            "index_code": "TEXT", "as_of": "TEXT", "code": "TEXT",
            "name": "TEXT", "weight": "REAL", "source": "TEXT",
            "received_at": "TEXT",
        }
        if (
            required.issubset(columns)
            and required_not_null.issubset(not_null)
            and all(column_types.get(key) == value for key, value in expected_types.items())
            and primary_key == ["index_code", "as_of", "code"]
        ):
            return

        conn.execute("DROP INDEX IF EXISTS idx_index_components_lookup")
        legacy_name = "index_components_legacy"
        suffix = 1
        while conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (legacy_name,),
        ).fetchone():
            suffix += 1
            legacy_name = f"index_components_legacy_{suffix}"
        conn.execute(f'ALTER TABLE index_components RENAME TO "{legacy_name}"')
        conn.executescript("""
            CREATE TABLE index_components (
                index_code TEXT NOT NULL,
                as_of TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                weight REAL,
                source TEXT NOT NULL,
                received_at TEXT NOT NULL,
                PRIMARY KEY (index_code, as_of, code)
            );
            CREATE INDEX idx_index_components_lookup
                ON index_components(index_code, as_of);
        """)
        conn.execute(
            "INSERT INTO data_update_log (source, record_count, status) VALUES (?, ?, ?)",
            ("meta:migration:index_components", 0, "migrated"),
        )

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
                CREATE TABLE IF NOT EXISTS index_components (
                    index_code TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT,
                    weight REAL,
                    source TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    PRIMARY KEY (index_code, as_of, code)
                );
                CREATE TABLE IF NOT EXISTS fundamentals (
                    code TEXT NOT NULL,
                    report_date TEXT NOT NULL,
                    announce_date TEXT NOT NULL,
                    field TEXT NOT NULL,
                    value REAL,
                    source TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    PRIMARY KEY (code, report_date, announce_date, field)
                );
            """)
            self._migrate_index_components_schema(conn)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_index_components_lookup "
                "ON index_components(index_code, as_of)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fundamentals_pit_lookup "
                "ON fundamentals(code, report_date, field, announce_date)"
            )

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

    def upsert_stocks(self, infos: list[StockInfo]):
        """Upsert a security-master batch in one transaction."""
        if not infos:
            return
        rows = [(
            info.code, info.name, info.exchange, info.board,
            info.listed_date.isoformat() if info.listed_date else None,
            info.delisted_date.isoformat() if info.delisted_date else None,
        ) for info in infos]
        with self._connect() as conn:
            conn.executemany("""
                INSERT OR REPLACE INTO stock_info
                (code, name, exchange, board, listed_date, delisted_date)
                VALUES (?, ?, ?, ?, ?, ?)
            """, rows)

    def replace_stocks(self, infos: list[StockInfo]):
        """Atomically replace the source-of-truth security-master snapshot."""
        rows = [(
            info.code, info.name, info.exchange, info.board,
            info.listed_date.isoformat() if info.listed_date else None,
            info.delisted_date.isoformat() if info.delisted_date else None,
        ) for info in infos]
        with self._connect() as conn:
            conn.execute("DELETE FROM stock_info")
            if rows:
                conn.executemany("""
                    INSERT INTO stock_info
                    (code, name, exchange, board, listed_date, delisted_date)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, rows)

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

    def _prepare_index_components(
        self,
        index_code: str,
        as_of: date,
        rows: list[dict],
        source: str,
        received_at: Optional[str],
    ) -> tuple[str, str, str, list[tuple]]:
        """Validate and canonicalize one snapshot before opening a write transaction."""
        if not rows:
            raise ValueError("index snapshot must contain at least one constituent")
        if not isinstance(as_of, date) or isinstance(as_of, datetime):
            raise ValueError("index snapshot date must be a date")
        if as_of > date.today():
            raise ValueError("index snapshot date cannot be in the future")
        index_code = self._canonical_index_code(index_code)
        as_of_text = as_of.isoformat()
        if received_at:
            if not isinstance(received_at, str):
                raise ValueError("received_at must be an ISO-8601 string")
            try:
                parsed_received_at = datetime.fromisoformat(received_at)
            except ValueError as exc:
                raise ValueError("received_at must be an ISO-8601 timestamp") from exc
            if parsed_received_at.tzinfo is None:
                raise ValueError("received_at must include a timezone")
            stamp = parsed_received_at.astimezone(timezone.utc).isoformat()
        else:
            stamp = datetime.now(timezone.utc).isoformat()
        payload = []
        seen_codes: set[str] = set()
        for row in rows:
            code = str(row.get("code", "")).strip().upper()
            if not code:
                raise ValueError("index snapshot contains an empty constituent code")
            if code in seen_codes:
                continue
            code = self._canonical_security_code(code)
            seen_codes.add(code)
            weight = row.get("weight")
            if weight is not None:
                try:
                    weight = float(weight)
                except (TypeError, ValueError):
                    raise ValueError(f"invalid index weight for {code}")
                if not math.isfinite(weight) or weight < 0 or weight > 100:
                    raise ValueError(f"invalid index weight for {code}")
            payload.append((
                index_code, as_of_text, code,
                str(row.get("name", "")).strip() or None,
                weight, source, stamp,
            ))
        if not payload:
            raise ValueError("index snapshot contains no usable constituent codes")
        minimum = {"000300.SH": 240, "000905.SH": 400}.get(index_code, 1)
        if len(payload) < minimum:
            raise ValueError(
                f"index snapshot for {index_code} contains {len(payload)} rows; "
                f"at least {minimum} are required"
            )
        return index_code, as_of_text, stamp, payload

    @staticmethod
    def _replace_index_components_in_connection(
        conn: sqlite3.Connection,
        index_code: str,
        as_of_text: str,
        payload: list[tuple],
    ) -> None:
        conn.execute(
            "DELETE FROM index_components WHERE index_code = ? AND as_of = ?",
            (index_code, as_of_text),
        )
        conn.executemany(
            "INSERT INTO index_components "
            "(index_code, as_of, code, name, weight, source, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            payload,
        )

    def replace_index_components(
        self,
        index_code: str,
        as_of: date,
        rows: list[dict],
        source: str = "manual:index_snapshot",
        received_at: Optional[str] = None,
    ) -> int:
        """Atomically replace one dated index-constituent snapshot."""
        index_code, as_of_text, stamp, payload = self._prepare_index_components(
            index_code, as_of, rows, source, received_at
        )
        with self._connect() as conn:
            self._replace_index_components_in_connection(conn, index_code, as_of_text, payload)
            conn.execute(
                "INSERT INTO data_update_log (source, record_count, status) VALUES (?, ?, ?)",
                (source, len(payload), "success"),
            )
        return len(payload)

    def _prepare_index_components_batch(self, snapshots: list[dict]) -> list[tuple]:
        if not snapshots:
            raise ValueError("index snapshot batch must contain at least one snapshot")
        prepared = []
        seen_keys: set[tuple[str, str]] = set()
        for item in snapshots:
            if not isinstance(item, dict):
                raise ValueError("each index snapshot must be an object")
            index_code, as_of_text, stamp, payload = self._prepare_index_components(
                item.get("index_code"), item.get("as_of"), item.get("rows"),
                item.get("source", "manual:index_snapshot"), item.get("received_at"),
            )
            key = (index_code, as_of_text)
            if key in seen_keys:
                raise ValueError(f"duplicate index snapshot period: {index_code} {as_of_text}")
            seen_keys.add(key)
            prepared.append((index_code, as_of_text, stamp, payload))
        return prepared

    def validate_index_components_batch(self, snapshots: list[dict]) -> list[dict]:
        """Validate a batch without changing storage and return its counts."""
        prepared = self._prepare_index_components_batch(snapshots)
        return [
            {
                "index_code": index_code,
                "as_of": as_of_text,
                "source": payload[0][5],
                "constituent_count": len(payload),
            }
            for index_code, as_of_text, _stamp, payload in prepared
        ]

    def replace_index_components_batch(
        self,
        snapshots: list[dict],
        data_versions: Optional[dict[str, str]] = None,
    ) -> int:
        """Atomically replace several dated snapshots in one SQLite transaction.

        Each item must contain ``index_code``, ``as_of`` (a ``date``), ``rows``
        and may contain ``source``/``received_at``.  All items are validated
        before any row is deleted, so a malformed later period cannot leave a
        partially imported history.
        """
        prepared = self._prepare_index_components_batch(snapshots)

        total = 0
        with self._connect() as conn:
            for index_code, as_of_text, stamp, payload in prepared:
                self._replace_index_components_in_connection(conn, index_code, as_of_text, payload)
                source = payload[0][5]
                conn.execute(
                    "INSERT INTO data_update_log (source, record_count, status) VALUES (?, ?, ?)",
                    (source, len(payload), "success"),
                )
                total += len(payload)
            for key, value in (data_versions or {}).items():
                if not isinstance(key, str) or not key or not isinstance(value, str):
                    raise ValueError("data version keys and values must be non-empty strings")
                conn.execute(
                    "INSERT OR REPLACE INTO data_versions (key, value, updated_at) "
                    "VALUES (?, ?, datetime('now'))",
                    (key, value),
                )
        return total

    def get_index_components(self, index_code: str, dt: date) -> list[dict]:
        """Return the latest snapshot effective on or before ``dt``.

        No later snapshot is considered, which keeps backtests point-in-time
        safe.  An empty result means a snapshot must be imported first.
        """
        if not isinstance(dt, date) or isinstance(dt, datetime):
            raise ValueError("index snapshot query date must be a date")
        if dt > date.today():
            raise ValueError("index snapshot query date cannot be in the future")
        index_code = self._canonical_index_code(index_code)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(as_of) AS as_of FROM index_components "
                "WHERE index_code = ? AND as_of <= ?",
                (index_code, dt.isoformat()),
            ).fetchone()
            if row is None or row["as_of"] is None:
                return []
            rows = conn.execute(
                "SELECT index_code, as_of, code, name, weight, source, received_at "
                "FROM index_components WHERE index_code = ? AND as_of = ? "
                "ORDER BY code",
                (index_code, row["as_of"]),
            ).fetchall()
        return [dict(item) for item in rows]

    def get_index_snapshot_coverage(self, index_code: str) -> list[dict]:
        """Return available point-in-time periods and constituent counts."""
        index_code = self._canonical_index_code(index_code)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT as_of, COUNT(*) AS constituent_count "
                "FROM index_components WHERE index_code = ? "
                "GROUP BY as_of ORDER BY as_of",
                (index_code,),
            ).fetchall()
        return [dict(item) for item in rows]

    @staticmethod
    def _canonical_fundamental_field(field: str) -> str:
        value = str(field).strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
            raise ValueError(f"invalid fundamental field: {field}")
        return value

    @staticmethod
    def _coerce_fundamental_date(value: object, label: str) -> date:
        if isinstance(value, datetime):
            raise ValueError(f"{label} must be a date without time")
        if isinstance(value, date):
            result = value
        else:
            text = str(value).strip()
            try:
                result = date.fromisoformat(text)
            except ValueError as exc:
                parsed = pd.to_datetime(value, errors="coerce")
                if pd.isna(parsed):
                    raise ValueError(f"invalid {label}: {value}") from exc
                result = parsed.date()
        if result > date.today():
            raise ValueError(f"{label} cannot be in the future")
        return result

    @staticmethod
    def _normalize_received_at(value: object | None) -> str:
        if value is None or (isinstance(value, str) and not value.strip()):
            return datetime.now(timezone.utc).isoformat()
        parsed = pd.to_datetime(value, errors="coerce", utc=True)
        if pd.isna(parsed):
            raise ValueError(f"invalid received_at: {value}")
        return pd.Timestamp(parsed).isoformat()

    def _prepare_fundamental_rows(
        self, rows: list[dict], default_source: str = "manual:fundamentals",
        default_received_at: object | None = None,
    ) -> list[tuple]:
        if not isinstance(rows, list) or not rows:
            raise ValueError("fundamental rows must contain at least one row")
        if not isinstance(default_source, str) or not default_source.strip():
            raise ValueError("fundamental source must be non-empty")
        prepared: list[tuple] = []
        seen: set[tuple[str, str, str, str]] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("each fundamental row must be an object")
            code = self._canonical_security_code(str(row.get("code", "")).strip().upper())
            report_date = self._coerce_fundamental_date(row.get("report_date"), "report_date")
            announce_date = self._coerce_fundamental_date(row.get("announce_date"), "announce_date")
            if announce_date < report_date:
                raise ValueError("announce_date must be on or after report_date")
            field = self._canonical_fundamental_field(row.get("field", ""))
            key = (code, report_date.isoformat(), announce_date.isoformat(), field)
            if key in seen:
                raise ValueError(
                    f"duplicate fundamental row: {code} {report_date} {announce_date} {field}"
                )
            seen.add(key)
            raw_value = row.get("value")
            if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
                value = None
            else:
                try:
                    value = float(raw_value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"fundamental value must be numeric: {raw_value}") from exc
                if not math.isfinite(value):
                    raise ValueError("fundamental value must be finite")
            source = str(row.get("source") or default_source).strip()
            if not source:
                raise ValueError("fundamental source must be non-empty")
            received_at = self._normalize_received_at(
                row.get("received_at", default_received_at)
            )
            prepared.append((
                code, report_date.isoformat(), announce_date.isoformat(), field,
                value, source, received_at,
            ))
        return prepared

    def validate_fundamentals(self, rows: list[dict]) -> dict:
        """Validate an import batch without changing the database."""
        prepared = self._prepare_fundamental_rows(rows)
        return {
            "row_count": len(prepared),
            "codes": sorted({row[0] for row in prepared}),
            "report_dates": sorted({row[1] for row in prepared}),
            "fields": sorted({row[3] for row in prepared}),
        }

    def replace_fundamentals_batch(
        self, rows: list[dict], data_versions: Optional[dict[str, str]] = None,
    ) -> int:
        """Atomically upsert point-in-time fundamental observations."""
        prepared = self._prepare_fundamental_rows(rows)
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO fundamentals "
                "(code, report_date, announce_date, field, value, source, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                prepared,
            )
            sources: dict[str, int] = {}
            for row in prepared:
                sources[row[5]] = sources.get(row[5], 0) + 1
            for source, count in sources.items():
                conn.execute(
                    "INSERT INTO data_update_log (source, record_count, status) VALUES (?, ?, ?)",
                    (source, count, "success"),
                )
            for key, value in (data_versions or {}).items():
                if not isinstance(key, str) or not key or not isinstance(value, str):
                    raise ValueError("data version keys and values must be non-empty strings")
                conn.execute(
                    "INSERT OR REPLACE INTO data_versions (key, value, updated_at) "
                    "VALUES (?, ?, datetime('now'))",
                    (key, value),
                )
        return len(prepared)

    def get_fundamentals(
        self, codes: list[str], as_of: date,
        report_dates: Optional[list[date | str]] = None,
        fields: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """Return the latest announced observation for each PIT key.

        The returned tidy frame is indexed by ``(code, report_date, field)``
        and contains ``value``, ``announce_date``, ``source`` and
        ``received_at``.  Only observations with ``announce_date <= as_of``
        are eligible, so later restatements cannot leak into an older query.
        """
        if not isinstance(as_of, date) or isinstance(as_of, datetime):
            raise ValueError("fundamental as_of must be a date")
        if as_of > date.today():
            raise ValueError("fundamental as_of cannot be in the future")
        canonical_codes: list[str] = []
        for raw_code in codes:
            code = self._canonical_security_code(str(raw_code).strip().upper())
            if code not in canonical_codes:
                canonical_codes.append(code)
        if not canonical_codes:
            raise ValueError("at least one fundamental code is required")
        normalized_reports = None
        if report_dates is not None:
            normalized_reports = [
                self._coerce_fundamental_date(item, "report_date").isoformat()
                for item in report_dates
            ]
            if not normalized_reports:
                return self._empty_fundamentals()
        normalized_fields = None
        if fields is not None:
            normalized_fields = [self._canonical_fundamental_field(item) for item in fields]
            normalized_fields = list(dict.fromkeys(normalized_fields))
            if not normalized_fields:
                return self._empty_fundamentals()
        code_placeholders = ",".join("?" for _ in canonical_codes)
        params: list[object] = [*canonical_codes, as_of.isoformat()]
        clauses = [f"code IN ({code_placeholders})", "announce_date <= ?"]
        if normalized_reports is not None:
            clauses.append("report_date IN (" + ",".join("?" for _ in normalized_reports) + ")")
            params.extend(normalized_reports)
        if normalized_fields is not None:
            clauses.append("field IN (" + ",".join("?" for _ in normalized_fields) + ")")
            params.extend(normalized_fields)
        query = (
            "SELECT code, report_date, announce_date, field, value, source, received_at "
            "FROM fundamentals WHERE " + " AND ".join(clauses) + " "
            "ORDER BY code, report_date, field, announce_date DESC, received_at DESC"
        )
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        if not rows:
            return self._empty_fundamentals()
        frame = pd.DataFrame([dict(row) for row in rows])
        frame = frame.drop_duplicates(
            subset=["code", "report_date", "field"], keep="first"
        )
        frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
        frame["report_date"] = pd.to_datetime(frame["report_date"]).dt.date
        frame["announce_date"] = pd.to_datetime(frame["announce_date"]).dt.date
        return frame.set_index(["code", "report_date", "field"]).sort_index()

    @staticmethod
    def _empty_fundamentals() -> pd.DataFrame:
        index = pd.MultiIndex.from_arrays([[], [], []], names=["code", "report_date", "field"])
        return pd.DataFrame(
            columns=["value", "announce_date", "source", "received_at"], index=index
        )

    def get_fundamental_coverage(self, code: Optional[str] = None) -> list[dict]:
        """Return imported PIT periods and field counts for observability."""
        params: list[object] = []
        where = ""
        if code is not None:
            code = self._canonical_security_code(str(code).strip().upper())
            where = "WHERE code = ?"
            params.append(code)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT code, report_date, COUNT(DISTINCT field) AS field_count, "
                "COUNT(DISTINCT announce_date) AS announcement_versions "
                f"FROM fundamentals {where} GROUP BY code, report_date "
                "ORDER BY code, report_date",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _canonical_index_code(index_code: str) -> str:
        value = str(index_code).strip().upper()
        aliases = {"000300": "000300.SH", "000905": "000905.SH"}
        if value in aliases:
            return aliases[value]
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value):
            raise ValueError(f"invalid index code: {index_code}")
        return value

    @staticmethod
    def _canonical_security_code(code: str) -> str:
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
            raise ValueError(f"invalid constituent code: {code}")
        symbol, exchange = code.split(".", 1)
        if symbol.startswith(("4", "8", "92")):
            expected = "BJ"
        elif symbol.startswith(("6", "9")):
            expected = "SH"
        elif symbol.startswith(("0", "2", "3")):
            expected = "SZ"
        else:
            raise ValueError(f"unsupported constituent code: {code}")
        if exchange != expected:
            raise ValueError(f"exchange suffix conflicts with constituent code: {code}")
        return code
