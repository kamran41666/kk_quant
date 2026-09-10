"""Portable, content-addressed backup/restore for the manual domain tables."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.models.schema import (
    DailyDecision,
    ManualAccount,
    ManualAccountSnapshot,
    ManualBackupRecord,
    ManualCashEvent,
    ManualCohort,
    ManualCorporateActionFact,
    ManualDailyJob,
    ManualDailyReview,
    ManualExecutionAuthorization,
    ManualExecutionConfirmation,
    ManualExecutionEvent,
    ManualExecutionItem,
    ManualExecutionPlan,
    ManualLedgerEvent,
    ManualPositionLot,
    ManualPositionSnapshot,
    ManualPilotObservation,
    ManualProspectivePilot,
    ManualReconciliation,
    ManualValuation,
    ResearchHoldoutAccess,
    ResearchHoldoutWindow,
    ResearchEvidenceArtifact,
    ResearchRevision,
    StrategyPromotionEvaluation,
    StrategyRelease,
)


class ManualBackupError(ValueError):
    pass


MANUAL_MODELS = (
    ManualAccount, StrategyRelease, ResearchEvidenceArtifact, StrategyPromotionEvaluation, ManualExecutionAuthorization,
    ResearchHoldoutWindow, ResearchHoldoutAccess, DailyDecision, ManualCohort,
    ManualExecutionPlan, ManualExecutionItem, ManualExecutionEvent, ManualExecutionConfirmation, ManualCashEvent,
    ManualLedgerEvent, ManualPositionLot, ManualAccountSnapshot, ManualPositionSnapshot,
    ManualReconciliation, ManualDailyJob, ManualValuation, ManualDailyReview,
    ResearchRevision, ManualCorporateActionFact, ManualProspectivePilot,
    ManualPilotObservation,
)
SCHEMA_VERSION = "manual-backup-v2"


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_value).encode("utf-8")


def _table_rows(db: Session, model: type[Any]) -> list[dict[str, Any]]:
    columns = [column.name for column in model.__table__.columns]
    result = []
    for row in db.scalars(select(model)).all():
        result.append({name: _json_value(getattr(row, name)) for name in columns})
    result.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str))
    return result


def build_manual_backup(db: Session, *, account_id: str | None = None) -> dict[str, Any]:
    if account_id is not None:
        raise ManualBackupError("account_scoped_backup_not_implemented")
    tables = {model.__tablename__: _table_rows(db, model) for model in MANUAL_MODELS}
    row_counts = {name: len(rows) for name, rows in tables.items()}
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "scope": {"type": "all_manual_domain"},
        "tables": tables,
        "row_counts": row_counts,
    }
    payload["content_hash"] = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    return payload


def _write_atomic(path: Path, data: bytes) -> None:
    if path.exists() and path.is_dir():
        raise ManualBackupError("backup_path_must_be_file")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_manual_backup(db: Session, path: str | Path, *, account_id: str | None = None) -> dict[str, Any]:
    target = Path(path)
    payload = build_manual_backup(db, account_id=account_id)
    _write_atomic(target, _canonical_bytes(payload) + b"\n")
    existing = db.scalars(select(ManualBackupRecord).where(ManualBackupRecord.content_hash == payload["content_hash"])).first()
    if existing is None:
        existing = ManualBackupRecord(
            account_id=account_id, backup_path=str(target), content_hash=payload["content_hash"],
            schema_version=SCHEMA_VERSION, row_counts=json.dumps(payload["row_counts"], sort_keys=True),
        )
        db.add(existing)
        db.commit()
    return {"path": str(target), "content_hash": payload["content_hash"], "row_counts": payload["row_counts"]}


def load_manual_backup(source: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(source, Mapping):
        payload = dict(source)
    else:
        try:
            payload = json.loads(Path(source).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManualBackupError("backup_read_failed") from exc
    if payload.get("schema_version") != SCHEMA_VERSION or not isinstance(payload.get("tables"), dict):
        raise ManualBackupError("unsupported_manual_backup_schema")
    expected_hash = payload.get("content_hash")
    without_hash = {key: value for key, value in payload.items() if key != "content_hash"}
    actual_hash = hashlib.sha256(_canonical_bytes(without_hash)).hexdigest()
    if expected_hash != actual_hash:
        raise ManualBackupError("manual_backup_hash_mismatch")
    return payload


def restore_manual_backup(db: Session, source: str | Path | Mapping[str, Any]) -> dict[str, int]:
    payload = load_manual_backup(source)
    existing_count = sum(db.query(model).count() for model in MANUAL_MODELS)
    if existing_count:
        raise ManualBackupError("manual_restore_requires_empty_database")
    table_by_name = {model.__tablename__: model for model in MANUAL_MODELS}
    inserted: dict[str, int] = {}
    try:
        for table_name, rows in payload["tables"].items():
            model = table_by_name.get(table_name)
            if model is None or not isinstance(rows, list):
                raise ManualBackupError(f"manual_backup_unknown_table:{table_name}")
            columns = {column.name for column in model.__table__.columns}
            for values in rows:
                if not isinstance(values, dict) or set(values) - columns:
                    raise ManualBackupError(f"manual_backup_invalid_row:{table_name}")
                db.add(model(**values))
            inserted[table_name] = len(rows)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return inserted


def verify_manual_backup(source: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    payload = load_manual_backup(source)
    return {
        "schema_version": payload["schema_version"],
        "content_hash": payload["content_hash"],
        "row_counts": payload["row_counts"],
        "verified": True,
    }


__all__ = [
    "ManualBackupError", "MANUAL_MODELS", "build_manual_backup", "write_manual_backup",
    "load_manual_backup", "restore_manual_backup", "verify_manual_backup",
]
