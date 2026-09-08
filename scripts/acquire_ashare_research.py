"""Acquire an immutable, resumable inception-universe A-share research archive.

Uses BaoStock's anonymous public API. Each response is saved before normalization;
no current-survivor filter or return-based security selection is performed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path("data/research/ashare-inception-2014-v1")
START = "2013-01-01"
END = "2026-08-31"
INCEPTION = "2014-01-02"
SEED = "kk-quant-ashare-inception-2014-v1"
FIELDS = "date,code,open,high,low,close,preclose,volume,amount,turn,tradestatus,pctChg,isST"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(temporary, path)


def login() -> None:
    import baostock as bs
    socket.setdefaulttimeout(20)
    result = bs.login()
    if result.error_code != "0":
        raise RuntimeError(f"BaoStock login: {result.error_code} {result.error_msg}")


def read_result(result) -> pd.DataFrame:
    if result.error_code != "0":
        raise RuntimeError(f"BaoStock {result.error_code}: {result.error_msg}")
    rows = []
    while result.next():
        rows.append(result.get_row_data())
    if result.error_code != "0":
        raise RuntimeError(f"BaoStock pagination {result.error_code}: {result.error_msg}")
    # BaoStock 0.9.3 next() returns False on an empty socket response without
    # changing error_code. A full, exhausted page is therefore not EOF: a
    # successful final request must replace it with an empty/short page.
    page = getattr(result, "data", [])
    if len(page) == 2000 and getattr(result, "cur_row_num", 0) >= len(page):
        raise RuntimeError("BaoStock pagination ended on a full page; terminal response unverified")
    return pd.DataFrame(rows, columns=result.fields)


def cached_response(path: Path, method: str, arguments: dict) -> pd.DataFrame:
    import baostock as bs
    metadata = path.with_suffix(".json")
    if path.exists() and metadata.exists():
        saved = json.loads(metadata.read_text())
        if saved["sha256"] != digest(path) or saved["arguments"] != arguments:
            raise ValueError(f"archive identity changed: {path}")
        return pd.read_parquet(path)
    frame = read_result(getattr(bs, method)(**arguments))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".parquet.tmp")
    frame.to_parquet(temporary, index=False)
    os.replace(temporary, path)
    write_json(metadata, {"source": f"baostock:{method}", "arguments": arguments,
                          "received_at": datetime.now(timezone.utc).isoformat(),
                          "rows": len(frame), "sha256": digest(path)})
    time.sleep(0.04)
    return frame


def canonical(code: str) -> str:
    exchange, number = code.split(".")
    return f"{number}.{exchange.upper()}"


def bootstrap(root: Path, size: int) -> dict:
    import baostock as bs
    target = root / "selection.json"
    if target.exists():
        existing = json.loads(target.read_text())
        if existing["requested_size"] != size:
            raise ValueError("selection already frozen with another size; choose a new root")
        return existing
    login()
    try:
        master = cached_response(root / "source/stock_basic.parquet", "query_stock_basic", {})
        inception = cached_response(root / "source/inception.parquet", "query_all_stock", {"day": INCEPTION})
        codes = [code for code in inception.code if re.fullmatch(r"(?:sh\.(?:600|601|603)\d{3}|sz\.(?:000|001|002)\d{3})", code)]
        stock_types = set(master.loc[master.type == "1", "code"])
        missing = sorted(set(codes) - stock_types)
        if missing:
            raise ValueError(f"historical stocks absent from all-status security master: {missing[:10]}")
        ranked = sorted(set(codes), key=lambda code: hashlib.sha256(f"{SEED}:{code}".encode()).hexdigest())
        selected = sorted(ranked[:size])
        payload = {"dataset_id": root.name, "inception": INCEPTION, "data_start": START,
                   "data_end": END, "requested_size": size, "seed": SEED,
                   "selection_rule": "sha256 sample from historical main-board inception universe; no current status filter",
                   "historical_eligible_count": len(ranked), "codes": selected,
                   "universe": [canonical(code) for code in selected],
                   "source_hashes": {"master": digest(root / "source/stock_basic.parquet"),
                                     "inception": digest(root / "source/inception.parquet")},
                   "created_at": datetime.now(timezone.utc).isoformat()}
        write_json(target, payload)
        for code in ("sh.000001", "sh.000300"):
            cached_response(root / f"source/benchmarks/{code}.parquet", "query_history_k_data_plus",
                            {"code": code, "fields": "date,code,open,high,low,close,preclose,volume,amount",
                             "start_date": START, "end_date": END, "frequency": "d", "adjustflag": "3"})
        return payload
    finally:
        bs.logout()


def fetch_security(root_text: str, code: str) -> dict:
    root = Path(root_text)
    base = root / "source" / "securities" / code
    result = {"code": code, "status": "ok"}
    try:
        daily = cached_response(base / "daily.parquet", "query_history_k_data_plus",
                                {"code": code, "fields": FIELDS, "start_date": START, "end_date": END,
                                 "frequency": "d", "adjustflag": "3"})
        cached_response(base / "factors.parquet", "query_adjust_factor",
                        {"code": code, "start_date": "1990-01-01", "end_date": END})
        for year in range(int(START[:4]), int(END[:4]) + 1):
            cached_response(base / f"dividends/{year}.parquet", "query_dividend_data",
                            {"code": code, "year": str(year), "yearType": "operate"})
        result["daily_rows"] = len(daily)
    except Exception as exc:
        result.update(status="error", error=f"{type(exc).__name__}: {exc}")
        # Reconnect the worker for its next code; failed responses are never
        # saved as valid empty datasets, so a later run resumes that request.
        try:
            login()
        except Exception:
            pass
    write_json(base / "status.json", result)
    return result


def acquire(root: Path, size: int, workers: int) -> dict:
    selection = bootstrap(root, size)
    results = []
    with ProcessPoolExecutor(max_workers=workers, initializer=login) as pool:
        tasks = {pool.submit(fetch_security, str(root), code): code for code in selection["codes"]}
        for future in as_completed(tasks):
            result = future.result()
            results.append(result)
            if len(results) % 10 == 0 or result["status"] != "ok":
                print(f"{len(results)}/{len(tasks)} completed; errors={sum(r['status'] != 'ok' for r in results)}; {result}", flush=True)
    manifest = {"selection": selection, "results": sorted(results, key=lambda row: row["code"]),
                "complete": all(row["status"] == "ok" for row in results)}
    write_json(root / "acquisition.json", manifest)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--size", type=int, default=500)
    parser.add_argument("--workers", type=int, default=3, choices=range(1, 5))
    args = parser.parse_args()
    result = acquire(args.root, args.size, args.workers)
    print(json.dumps({"complete": result["complete"], "securities": len(result["results"])}))
