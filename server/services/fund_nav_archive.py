"""Content-addressed archive for domestic-fund daily NAV history.

The live Eastmoney endpoint is useful for fetching data, but its response can
be corrected later.  This module turns one validated response into an
immutable local dataset.  Backtests can point to the dataset id and reproduce
the exact rows after a restart; a correction produces a different id.
"""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import math
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from quant_engine.data.live import SHANGHAI_TZ
from server.models.schema import FundNavDataset, FundNavDatasetRow
from server.services.paper_market_rules import CN_FUND, normalize_symbol

DATASET_VERSION = "fund-nav-dataset-v1"
DATA_SOURCE = "eastmoney:fund_nav"


def _today() -> date:
    return datetime.now(SHANGHAI_TZ).date()


def _canonical_rows(code: str, rows: Iterable[dict[str, Any]], start: date, end: date) -> list[dict[str, Any]]:
    if start > end:
        raise ValueError("start_date must not be after end_date")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        raw_date = raw.get("date") or raw.get("nav_date")
        try:
            nav_date = date.fromisoformat(str(raw_date))
        except (TypeError, ValueError):
            continue
        if nav_date < start or nav_date > end or nav_date > _today():
            continue
        if nav_date.isoformat() in seen:
            raise ValueError(f"duplicate fund NAV date: {nav_date.isoformat()}")
        raw_nav = raw.get("nav", raw.get("close"))
        try:
            nav = float(raw_nav)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(nav) or nav <= 0:
            continue
        raw_change = raw.get("change_pct")
        change: Optional[float]
        if raw_change in (None, ""):
            change = None
        else:
            try:
                change = float(raw_change)
            except (TypeError, ValueError):
                change = None
            if change is not None and not math.isfinite(change):
                change = None
        seen.add(nav_date.isoformat())
        result.append({"date": nav_date.isoformat(), "nav": nav, "change_pct": change})
    result.sort(key=lambda item: item["date"])
    if not result:
        raise ValueError("fund NAV dataset has no usable rows")
    return result


def _manifest(*, code: str, source: str, start: date, end: date, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "dataset_version": DATASET_VERSION,
        "market": CN_FUND,
        "code": code,
        "source": source,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "row_count": len(rows),
        "nav_rule": "published_nav_next_valid_day",
        "rows": rows,
    }


def archive_fund_nav_dataset(
    db: Session,
    *,
    code: str,
    rows: Iterable[dict[str, Any]],
    source: str = DATA_SOURCE,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> dict[str, Any]:
    """Persist an immutable dataset and return its manifest.

    Replaying the exact content is idempotent.  A changed NAV, source, or
    requested range changes the content hash and therefore creates a new
    dataset; no existing row is updated or deleted.
    """
    normalized_code = normalize_symbol(CN_FUND, code)
    normalized_source = str(source).strip()
    if normalized_source != DATA_SOURCE:
        raise ValueError("fund NAV archive requires source=eastmoney:fund_nav")
    raw_rows = list(rows)
    if not raw_rows:
        raise ValueError("fund NAV dataset has no rows")
    dates: list[date] = []
    for item in raw_rows:
        raw_date = item.get("date") if isinstance(item, dict) else None
        try:
            dates.append(date.fromisoformat(str(raw_date)))
        except (TypeError, ValueError):
            continue
    if not dates:
        raise ValueError("fund NAV dataset has no valid dates")
    start = start_date or min(dates)
    end = end_date or max(dates)
    canonical = _canonical_rows(normalized_code, raw_rows, start, end)
    payload = _manifest(code=normalized_code, source=normalized_source, start=start, end=end, rows=canonical)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    content_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    existing = db.query(FundNavDataset).filter(FundNavDataset.id == content_hash).first()
    if existing:
        return dataset_dict(existing, db=db)

    stamp = datetime.now(SHANGHAI_TZ).isoformat()
    dataset = FundNavDataset(
        id=content_hash,
        code=normalized_code,
        source=normalized_source,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        row_count=len(canonical),
        content_hash=content_hash,
        manifest=encoded,
        created_at=stamp,
    )
    db.add(dataset)
    db.flush()
    for item in canonical:
        db.add(FundNavDatasetRow(
            dataset_id=content_hash,
            code=normalized_code,
            nav_date=item["date"],
            nav=item["nav"],
            change_pct=item["change_pct"],
            created_at=stamp,
        ))
    db.commit()
    db.refresh(dataset)
    return dataset_dict(dataset, db=db)


def dataset_dict(dataset: FundNavDataset, *, db: Optional[Session] = None, include_rows: bool = False) -> dict[str, Any]:
    try:
        manifest = json.loads(dataset.manifest or "{}")
    except (TypeError, ValueError):
        manifest = {}
    result: dict[str, Any] = {
        "dataset_id": dataset.id,
        "market": CN_FUND,
        "code": dataset.code,
        "source": dataset.source,
        "start_date": dataset.start_date,
        "end_date": dataset.end_date,
        "row_count": dataset.row_count,
        "content_hash": dataset.content_hash,
        "dataset_version": (manifest or {}).get("dataset_version", DATASET_VERSION),
        "nav_rule": (manifest or {}).get("nav_rule", "published_nav_next_valid_day"),
        "created_at": dataset.created_at,
        "immutable": True,
    }
    if include_rows and db is not None:
        rows = (db.query(FundNavDatasetRow)
                .filter(FundNavDatasetRow.dataset_id == dataset.id)
                .order_by(FundNavDatasetRow.nav_date.asc()).all())
        result["rows"] = [{"date": row.nav_date, "nav": row.nav, "change_pct": row.change_pct} for row in rows]
    return result


def get_fund_nav_dataset(db: Session, dataset_id: str, *, include_rows: bool = False) -> dict[str, Any]:
    dataset = db.query(FundNavDataset).filter(FundNavDataset.id == str(dataset_id)).first()
    if not dataset:
        raise KeyError("fund NAV dataset not found")
    return dataset_dict(dataset, db=db, include_rows=include_rows)


def list_fund_nav_datasets(db: Session, *, code: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    query = db.query(FundNavDataset)
    if code:
        query = query.filter(FundNavDataset.code == normalize_symbol(CN_FUND, code))
    rows = query.order_by(FundNavDataset.created_at.desc()).limit(max(1, min(int(limit), 200))).all()
    return [dataset_dict(row) for row in rows]
