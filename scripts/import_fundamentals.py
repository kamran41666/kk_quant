"""Import point-in-time fundamental observations without network access.

Accepted input formats are CSV/Parquet tables or JSON lists with columns
``code,report_date,announce_date,field,value``.  Optional ``source`` and
``received_at`` columns are preserved for audit.  ``--dry-run`` performs all
storage validation without changing the database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from quant_engine.data.store import MetaDB


REQUIRED_COLUMNS = {"code", "report_date", "announce_date", "field", "value"}


def _read_records(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        return frame.to_dict(orient="records")
    if suffix in {".parquet", ".pq"}:
        return pd.read_parquet(path).to_dict(orient="records")
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
            payload = payload["rows"]
        if isinstance(payload, list):
            return payload
    raise ValueError("input must be a .csv, .parquet, .pq or .json file")


def load_rows(path: str | Path, source: str = "import:fundamentals") -> list[dict[str, Any]]:
    input_path = Path(path).resolve()
    if not input_path.exists() or not input_path.is_file():
        raise ValueError(f"input file does not exist: {input_path}")
    records = _read_records(input_path)
    if not records:
        raise ValueError("input file contains no rows")
    missing = REQUIRED_COLUMNS - set(records[0])
    if missing:
        raise ValueError(f"input is missing required columns: {', '.join(sorted(missing))}")
    rows: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("each fundamental row must be an object")
        row = dict(record)
        row["source"] = str(record.get("source") or source)
        if "received_at" in record and record.get("received_at") not in (None, ""):
            row["received_at"] = record["received_at"]
        rows.append(row)
    return rows


def import_fundamentals(path: str | Path, *, dry_run: bool = False,
                        source: str = "import:fundamentals") -> dict[str, Any]:
    input_path = Path(path).resolve()
    digest_before = hashlib.sha256(input_path.read_bytes()).hexdigest()
    rows = load_rows(input_path, source=source)
    digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
    if digest != digest_before:
        raise ValueError("input file changed during import validation; retry with a stable file")
    db = MetaDB()
    validation = db.validate_fundamentals(rows)
    if not dry_run:
        db.replace_fundamentals_batch(rows, data_versions={
            "fundamentals_last_sha256": digest,
            "fundamentals_last_import": str(input_path),
        })
    return {
        "status": "validated" if dry_run else "ok",
        "dry_run": dry_run,
        "input": str(input_path),
        "sha256": digest,
        **validation,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="CSV, Parquet or JSON fundamental observations")
    parser.add_argument("--dry-run", action="store_true", help="validate without writing")
    parser.add_argument("--source", default="import:fundamentals", help="audit source label")
    args = parser.parse_args()
    try:
        print(json.dumps(import_fundamentals(args.input, dry_run=args.dry_run, source=args.source),
                         ensure_ascii=False, indent=2))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
