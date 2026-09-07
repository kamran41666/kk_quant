"""Import point-in-time index constituent snapshots without network access.

Accepted input formats are CSV/Parquet tables with columns
``index_code,as_of,code,name,weight`` and JSON containing either that same
row list or ``{"snapshots": [{"index_code", "as_of", "rows": [...]}, ...]}``.
Use ``--dry-run`` to perform all storage-layer checks without changing the
database.  A successful non-dry run validates the complete file first and
then writes all periods in one transaction.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from quant_engine.data.store import MetaDB


REQUIRED_COLUMNS = {"index_code", "as_of", "code"}


def _read_records(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        return frame.to_dict(orient="records")
    if suffix in {".parquet", ".pq"}:
        return pd.read_parquet(path).to_dict(orient="records")
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(payload, dict) and isinstance(payload.get("snapshots"), list):
            return _flatten_nested(payload["snapshots"])
        if isinstance(payload, list):
            return payload
    raise ValueError("input must be a .csv, .parquet, .pq or .json file")


def _flatten_nested(snapshots: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            raise ValueError("each JSON snapshot must be an object")
        index_code = snapshot.get("index_code")
        as_of = snapshot.get("as_of")
        members = snapshot.get("rows")
        source = snapshot.get("source")
        received_at = snapshot.get("received_at")
        if not index_code or not as_of or not isinstance(members, list):
            raise ValueError("nested JSON snapshots require index_code, as_of and rows")
        for member in members:
            if not isinstance(member, dict):
                raise ValueError("each constituent row must be an object")
            rows.append({
                **member, "index_code": index_code, "as_of": as_of,
                "source": source, "received_at": received_at,
            })
    return rows


def _as_date(value: Any) -> date:
    # Parquet readers commonly return pandas.Timestamp (a datetime subclass),
    # while the storage contract intentionally accepts date-only values.
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            raise ValueError(f"invalid snapshot date: {value}") from exc
        return parsed.date()


def _as_received_at(value: Any) -> str | None:
    """Normalize optional Parquet/JSON receipt timestamps to SQLite strings."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, (pd.Timestamp, datetime)):
        if value.tzinfo is None:
            value = value.tz_localize("UTC") if isinstance(value, pd.Timestamp) else value.replace(tzinfo=timezone.utc)
        elif isinstance(value, pd.Timestamp):
            value = value.tz_convert("UTC")
        else:
            value = value.astimezone(timezone.utc)
        return value.isoformat()
    text = str(value).strip()
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return text
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize("UTC")
    else:
        parsed = parsed.tz_convert("UTC")
    return parsed.isoformat()


def load_snapshots(path: str | Path, source: str = "import:index_snapshot") -> list[dict[str, Any]]:
    """Read and group an import file into the DataAPI batch contract."""
    input_path = Path(path).resolve()
    if not input_path.exists() or not input_path.is_file():
        raise ValueError(f"input file does not exist: {input_path}")
    records = _read_records(input_path)
    if not records:
        raise ValueError("input file contains no rows")
    missing = REQUIRED_COLUMNS - set(records[0])
    if missing:
        raise ValueError(f"input is missing required columns: {', '.join(sorted(missing))}")

    grouped: dict[tuple[str, date], dict[str, Any]] = {}
    for record in records:
        index_code = str(record.get("index_code", "")).strip().upper()
        as_of = _as_date(record.get("as_of"))
        key = (index_code, as_of)
        row_source = str(record.get("source") or source)
        row_received_at = _as_received_at(record.get("received_at"))
        bucket = grouped.setdefault(key, {
            "index_code": index_code, "as_of": as_of, "rows": [], "source": row_source,
            "received_at": row_received_at,
        })
        if bucket["source"] != row_source:
            raise ValueError(f"mixed sources in snapshot period: {index_code} {as_of}")
        if bucket["received_at"] and row_received_at and bucket["received_at"] != row_received_at:
            raise ValueError(f"mixed received_at values in snapshot period: {index_code} {as_of}")
        if not bucket["received_at"] and row_received_at:
            bucket["received_at"] = row_received_at
        row = {
            "code": str(record.get("code", "")).strip().upper(),
            "name": str(record.get("name", "")).strip() or None,
        }
        if "weight" in record and str(record.get("weight", "")).strip() != "":
            row["weight"] = record.get("weight")
        bucket["rows"].append(row)
    return list(grouped.values())


def import_snapshots(path: str | Path, *, dry_run: bool = False,
                     source: str = "import:index_snapshot") -> dict[str, Any]:
    input_path = Path(path).resolve()
    digest_before = hashlib.sha256(input_path.read_bytes()).hexdigest()
    snapshots = load_snapshots(input_path, source=source)
    digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
    if digest != digest_before:
        raise ValueError("input file changed during import validation; retry with a stable file")
    db = MetaDB()
    validation = db.validate_index_components_batch(snapshots)
    total = sum(item["constituent_count"] for item in validation)
    if not dry_run:
        total = db.replace_index_components_batch(
            snapshots,
            data_versions={
                "index_snapshot_last_sha256": digest,
                "index_snapshot_last_import": str(input_path),
            },
        )
    return {
        "status": "validated" if dry_run else "ok",
        "dry_run": dry_run,
        "input": str(input_path),
        "sha256": digest,
        "snapshots": validation,
        "total_constituents": total,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="CSV, Parquet or JSON snapshot file")
    parser.add_argument("--dry-run", action="store_true", help="validate without writing")
    parser.add_argument("--source", default="import:index_snapshot", help="audit source label")
    args = parser.parse_args()
    try:
        print(json.dumps(import_snapshots(args.input, dry_run=args.dry_run, source=args.source),
                         ensure_ascii=False, indent=2))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
