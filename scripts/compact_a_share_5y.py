"""Compact raw East Money A-share responses into query-friendly Parquet files.

The downloader keeps one raw JSON response per security and adjustment mode so
an interrupted run can resume. This command is intentionally separate: it can
be rerun without making any network requests.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


FIELDS = [
    "date", "open", "close", "high", "low", "volume", "amount",
    "amplitude", "pct_change", "change", "turnover_rate",
]


def load_rows(path: Path, code: str) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    klines = (((payload.get("data") or {}).get("klines")) or [])
    rows: list[dict] = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < len(FIELDS):
            continue
        row = dict(zip(FIELDS, parts[: len(FIELDS)]))
        row["code"] = code
        rows.append(row)
    return rows


def compact(dataset: Path, modes: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    none_frame: pd.DataFrame | None = None
    universe_path = dataset / "universe.csv"
    if universe_path.exists():
        universe = pd.read_csv(universe_path, dtype={"symbol": str, "listed_date": str})
        universe.to_parquet(dataset / "universe.parquet", index=False)
    for mode in modes:
        source_dir = dataset / "raw" / mode
        output_dir = dataset / "parquet" / mode
        output_dir.mkdir(parents=True, exist_ok=True)
        frames: list[pd.DataFrame] = []
        for path in sorted(source_dir.glob("*.json")):
            code = path.stem
            rows = load_rows(path, code)
            if rows:
                frames.append(pd.DataFrame(rows))
        if not frames:
            counts[mode] = 0
            continue
        frame = pd.concat(frames, ignore_index=True)
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        numeric = [field for field in FIELDS if field != "date"]
        for field in numeric:
            frame[field] = pd.to_numeric(frame[field], errors="coerce")
        frame = frame.dropna(subset=["code", "date", "open", "high", "low", "close"])
        frame = frame.drop_duplicates(["code", "date"]).sort_values(["code", "date"])
        frame.to_parquet(output_dir / "daily.parquet", index=False)
        if mode == "none":
            none_frame = frame
        counts[mode] = len(frame)
    if none_frame is not None:
        calendar = pd.DataFrame({
            "date": pd.Series(none_frame["date"].drop_duplicates().sort_values().to_numpy()),
            "is_trading_day": True,
            "source": "eastmoney:union_of_a_share_daily",
        })
        calendar.to_parquet(dataset / "trading_calendar.parquet", index=False)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="data/market_a_share_5y")
    args = parser.parse_args()
    dataset = Path(args.dataset).resolve()
    counts = compact(dataset, ["none", "qfq", "hfq"])
    manifest = dataset / "manifest.json"
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
        data["parquet_rows"] = counts
        data["compacted_at"] = pd.Timestamp.utcnow().isoformat()
        manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(counts, ensure_ascii=False))


if __name__ == "__main__":
    main()
