"""Import the frozen COPA v1 A-share research dataset.

The command is cross-platform and resumable.  It archives unadjusted Tencent
daily bars, converts Sina's published backward-adjustment factor sequence into
incremental ex-date events, imports both into the project's normal stores, and
writes a content-addressed manifest.  Existing raw archives are never replaced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import akshare as ak
import numpy as np
import pandas as pd

from quant_engine.data.api import DataAPI
from quant_engine.data.calendar import TradingCalendar
from quant_engine.data.store import AdjustStore, MetaDB, PriceStore, StockInfo

DATASET_ID = "copa-price-only-v1-2018-2026-r2"
START_DATE = date(2018, 1, 2)
END_DATE = date(2026, 8, 31)
UNIVERSE = (
    StockInfo("600519.SH", "贵州茅台", "SSE", "主板", date(2001, 8, 27)),
    StockInfo("600036.SH", "招商银行", "SSE", "主板", date(2002, 4, 9)),
    StockInfo("600276.SH", "恒瑞医药", "SSE", "主板", date(2000, 10, 18)),
    StockInfo("601318.SH", "中国平安", "SSE", "主板", date(2007, 3, 1)),
    StockInfo("601398.SH", "工商银行", "SSE", "主板", date(2006, 10, 27)),
    StockInfo("600000.SH", "浦发银行", "SSE", "主板", date(1999, 11, 10)),
    StockInfo("600887.SH", "伊利股份", "SSE", "主板", date(1996, 3, 12)),
    StockInfo("601166.SH", "兴业银行", "SSE", "主板", date(2007, 2, 5)),
    StockInfo("000858.SZ", "五粮液", "SZSE", "主板", date(1998, 4, 27)),
    StockInfo("002594.SZ", "比亚迪", "SZSE", "主板", date(2011, 6, 30)),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_parquet(frame: pd.DataFrame, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return
    with tempfile.NamedTemporaryFile(
        prefix=target.stem + "-",
        suffix=".parquet",
        dir=target.parent,
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
    try:
        frame.to_parquet(temporary, index=False)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _provider_symbol(code: str) -> str:
    symbol, exchange = code.split(".")
    return ("sh" if exchange == "SH" else "sz") + symbol


def _normalize_daily(code: str, frame: pd.DataFrame, received_at: str) -> pd.DataFrame:
    required = {"date", "open", "high", "low", "close", "volume", "amount"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{code} daily response missing fields: {', '.join(missing)}")
    result = frame.copy()
    result["date"] = pd.to_datetime(result["date"], errors="coerce")
    for field in ("open", "high", "low", "close", "volume", "amount"):
        result[field] = pd.to_numeric(result[field], errors="coerce")
    if "turnover" in result:
        result["turnover_rate"] = pd.to_numeric(result["turnover"], errors="coerce") * 100.0
    result = result.dropna(subset=["date", "open", "high", "low", "close", "volume", "amount"])
    result = result[(result["date"].dt.date >= START_DATE) & (result["date"].dt.date <= END_DATE)]
    result = result.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    finite = np.isfinite(result[["open", "high", "low", "close", "volume", "amount"]]).all(axis=1)
    valid = (
        finite
        & (result[["open", "high", "low", "close"]] > 0).all(axis=1)
        & (result["high"] >= result[["open", "close", "low"]].max(axis=1))
        & (result["low"] <= result[["open", "close", "high"]].min(axis=1))
        & (result["volume"] >= 0)
        & (result["amount"] >= 0)
    )
    if not bool(valid.all()):
        raise ValueError(f"{code} daily response contains {int((~valid).sum())} invalid rows")
    result["code"] = code
    result["source"] = "akshare:tencent:stock_zh_a_hist_tx"
    result["received_at"] = received_at
    columns = [
        "code", "date", "open", "high", "low", "close", "volume", "amount",
        "turnover_rate", "source", "received_at",
    ]
    return result[[column for column in columns if column in result.columns]]


def _incremental_hfq_events(code: str, frame: pd.DataFrame, received_at: str) -> pd.DataFrame:
    if not {"date", "hfq_factor"}.issubset(frame.columns):
        raise ValueError(f"{code} factor response is missing date/hfq_factor")
    result = frame[["date", "hfq_factor"]].copy()
    result["date"] = pd.to_datetime(result["date"], errors="coerce")
    result["hfq_factor"] = pd.to_numeric(result["hfq_factor"], errors="coerce")
    result = result.dropna().drop_duplicates("date", keep="last").sort_values("date")
    result = result[(result["date"].dt.date <= END_DATE) & (result["hfq_factor"] > 0)]
    if result.empty:
        raise ValueError(f"{code} factor response is empty")
    result["factor"] = result["hfq_factor"] / result["hfq_factor"].shift(1)
    result.iloc[0, result.columns.get_loc("factor")] = float(result.iloc[0]["hfq_factor"])
    if not np.isfinite(result["factor"]).all() or (result["factor"] <= 0).any():
        raise ValueError(f"{code} factor response contains invalid ratios")
    result["code"] = code
    result["source"] = "akshare:sina:hfq-factor"
    result["received_at"] = received_at
    return result[["code", "date", "factor", "source", "received_at"]].reset_index(drop=True)


def _fetch_one(stock: StockInfo, raw_root: Path) -> dict[str, Any]:
    code = stock.code
    received_at = datetime.now(UTC).isoformat()
    daily_path = raw_root / "daily" / f"{code}.parquet"
    factor_path = raw_root / "factors" / f"{code}.parquet"
    if daily_path.exists():
        daily = pd.read_parquet(daily_path)
    else:
        daily = _normalize_daily(
            code,
            ak.stock_zh_a_hist_tx(
                symbol=_provider_symbol(code),
                start_date=START_DATE.strftime("%Y%m%d"),
                end_date=END_DATE.strftime("%Y%m%d"),
                adjust="",
                timeout=15,
            ),
            received_at,
        )
        _atomic_parquet(daily, daily_path)
    if factor_path.exists():
        factors = pd.read_parquet(factor_path)
    else:
        factors = _incremental_hfq_events(
            code,
            ak.stock_zh_a_daily(symbol=_provider_symbol(code), adjust="hfq-factor"),
            received_at,
        )
        _atomic_parquet(factors, factor_path)
    return {
        "code": code,
        "daily": daily,
        "factors": factors,
        "daily_path": daily_path,
        "factor_path": factor_path,
    }


def import_dataset(data_dir: Path, workers: int = 4) -> dict[str, Any]:
    dataset_root = data_dir / "research" / DATASET_ID
    raw_root = dataset_root / "raw"
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        jobs = {pool.submit(_fetch_one, stock, raw_root): stock.code for stock in UNIVERSE}
        for future in as_completed(jobs):
            code = jobs[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - aggregate provider failures by code
                errors.append({"code": code, "error": f"{type(exc).__name__}: {exc}"})
    if errors:
        raise RuntimeError("dataset fetch failed: " + json.dumps(errors, ensure_ascii=False))
    results.sort(key=lambda item: item["code"])

    PriceStore(str(data_dir)).write_batch({
        item["code"]: item["daily"].drop(columns=["code"])
        for item in results
    })
    factor_frame = pd.concat([item["factors"] for item in results], ignore_index=True)
    factor_store = AdjustStore(str(data_dir))
    factor_path = data_dir / "raw" / "adjust" / "adjust_factor.parquet"
    if factor_path.exists():
        existing = pd.read_parquet(factor_path)
        existing = existing[~existing["code"].isin([stock.code for stock in UNIVERSE])]
        factor_frame = pd.concat([existing, factor_frame], ignore_index=True)
    factor_store.write_adjust_factors(
        factor_frame.drop_duplicates(["code", "date"], keep="last").sort_values(["code", "date"])
    )
    meta = MetaDB(str(data_dir / "meta.db"))
    for stock in UNIVERSE:
        meta.upsert_stock(stock)

    os.environ["QUANT_DATA_DIR"] = str(data_dir)
    DataAPI._instance = None
    calendar = TradingCalendar(start_year=START_DATE.year, end_year=END_DATE.year)
    coverage = DataAPI().daily_coverage(
        [stock.code for stock in UNIVERSE],
        START_DATE,
        END_DATE,
        fields=["open", "high", "low", "close", "volume", "amount"],
        adjust="event_driven",
        calendar=calendar,
    )
    manifest = {
        "dataset_id": DATASET_ID,
        "created_at": datetime.now(UTC).isoformat(),
        "research_only": True,
        "selection_rule": "ten pre-registered, long-listed, liquid Shanghai/Shenzhen main-board securities; fixed before performance evaluation",
        "known_biases": [
            "fixed current research universe; not a historical index constituent universe",
            "delisted securities are absent, so survivorship bias is not eliminated",
            "public providers have no production SLA",
        ],
        "start_date": START_DATE.isoformat(),
        "end_date": END_DATE.isoformat(),
        "price_source": "akshare:tencent:stock_zh_a_hist_tx:unadjusted",
        "factor_source": "akshare:sina:hfq-factor:converted-to-incremental-events",
        "calendar": calendar.coverage_report(START_DATE, END_DATE),
        "universe": [
            {**asdict(stock), "listed_date": stock.listed_date.isoformat()}
            for stock in UNIVERSE
        ],
        "files": [
            {
                "code": item["code"],
                "daily_rows": len(item["daily"]),
                "daily_sha256": _sha256(item["daily_path"]),
                "factor_rows": len(item["factors"]),
                "factor_sha256": _sha256(item["factor_path"]),
            }
            for item in results
        ],
        "coverage": coverage,
    }
    manifest_path = dataset_root / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    manifest = import_dataset(args.data_dir.resolve(), workers=args.workers)
    print(json.dumps({
        "dataset_id": manifest["dataset_id"],
        "universe_count": len(manifest["universe"]),
        "coverage_complete": manifest["coverage"]["complete"],
        "coverage_hash": manifest["coverage"]["coverage_hash"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
