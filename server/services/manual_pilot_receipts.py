"""Server-owned daily close receipts for the evidence-bound manual pilot.

This module is deliberately separate from the legacy pilot observation API.
The only caller supplied values are the operator identity and an idempotency
key.  The session date, universe, provider, timestamps and all market rows are
owned by this service.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.config import settings
from server.models.database import ensure_savepoint_transaction
from server.models.schema import (
    ManualPilotBinding,
    ManualPilotMarketReceipt,
    ManualProspectivePilot,
    StrategyRelease,
)
from server.services.manual_evidence import holdout_commit_snapshot
from server.services.manual_pilot_admission import verify_pilot_binding
from server.services.research_holdout import _lock_registry

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
PROVIDER = "baostock:query_history_k_data_plus"
FIELDS = (
    "date", "code", "open", "high", "low", "close", "preclose", "volume",
    "amount", "turn", "tradestatus", "pctChg", "isST",
)
MAIN_BOARD_RE = re.compile(r"^(?:(?:600|601|603|605)\d{3}\.SH|(?:000|001|002)\d{3}\.SZ)$")
BAO_RE = re.compile(r"^(sh|sz)\.(\d{6})$", re.IGNORECASE)


class PilotReceiptError(ValueError):
    """Safe, deterministic receipt rejection."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _sha256_json(value: Any) -> str:
    body = _canonical(value).encode("utf-8")
    return _sha256_bytes(body)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise PilotReceiptError("receipt_timestamp_requires_timezone")
    return value.astimezone(UTC).isoformat()


def _canonical_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    match = BAO_RE.fullmatch(text)
    if match:
        text = f"{match.group(2)}.{match.group(1).upper()}"
    if not MAIN_BOARD_RE.fullmatch(text):
        raise PilotReceiptError("pilot_receipt_code_invalid")
    return text


def _protocol(binding: ManualPilotBinding) -> dict[str, Any]:
    try:
        payload = json.loads(binding.protocol_json or "{}")
    except (TypeError, ValueError) as exc:
        raise PilotReceiptError("pilot_binding_protocol_invalid") from exc
    if not isinstance(payload, dict):
        raise PilotReceiptError("pilot_binding_protocol_invalid")
    return payload


def _admission_snapshot(db: Session, pilot_id: str) -> dict[str, Any]:
    try:
        result = verify_pilot_binding(db, pilot_id)
    except Exception as exc:
        raise PilotReceiptError(str(exc) or "pilot_binding_rejected") from exc
    if not isinstance(result, dict) or result.get("verified") is not True:
        raise PilotReceiptError("pilot_binding_admission_metadata_invalid")
    if result.get("pilot_id") != pilot_id or not result.get("binding_id"):
        raise PilotReceiptError("pilot_binding_admission_identity_invalid")
    binding = db.get(ManualPilotBinding, str(result["binding_id"]))
    pilot = db.get(ManualProspectivePilot, pilot_id)
    if binding is None or pilot is None or binding.pilot_id != pilot_id:
        raise PilotReceiptError("pilot_binding_not_found")
    release = db.get(StrategyRelease, binding.release_id)
    if release is None or pilot.release_id != binding.release_id:
        raise PilotReceiptError("pilot_binding_parent_not_found")
    if result.get("binding_hash") is not None and result.get("binding_hash") != binding.binding_hash:
        raise PilotReceiptError("pilot_binding_admission_hash_mismatch")
    return {
        "admission": json.loads(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        "binding_id": binding.id,
        "binding_hash": binding.binding_hash,
        "protocol": _protocol(binding),
        "pilot_id": pilot.id,
        "pilot_status": pilot.status,
        "pilot_data_mode": pilot.data_mode,
        "pilot_release_id": pilot.release_id,
        "pilot_created_at": pilot.created_at,
        "release_id": release.id,
        "release_hash": release.release_hash,
        "release_status": release.status,
    }


def _db_snapshot(db: Session, pilot_id: str) -> str:
    """Capture only DB parent facts; this is safe to run under the short write lock."""
    db.expire_all()
    pilot = db.get(ManualProspectivePilot, pilot_id)
    if pilot is None:
        raise PilotReceiptError("pilot_receipt_parent_not_found")
    binding = db.scalars(select(ManualPilotBinding).where(ManualPilotBinding.pilot_id == pilot_id)).first()
    release = db.get(StrategyRelease, pilot.release_id)
    if binding is None or release is None or binding.release_id != release.id:
        raise PilotReceiptError("pilot_receipt_parent_not_found")

    def row_payload(row: Any) -> dict[str, Any]:
        return {column.name: getattr(row, column.name) for column in row.__table__.columns}

    payload = {
        "pilot": row_payload(pilot), "binding": row_payload(binding), "release": row_payload(release),
        "holdout_parent": holdout_commit_snapshot(db, release.id),
    }
    return _canonical(payload)


def _parse_timestamp(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise PilotReceiptError(f"{field}_invalid") from exc
    if parsed.tzinfo is None:
        raise PilotReceiptError(f"{field}_requires_timezone")
    return parsed.astimezone(UTC)


def _parse_day(value: Any, field: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise PilotReceiptError(f"{field}_invalid")
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise PilotReceiptError(f"{field}_invalid") from exc


def _decimal_text(value: Any, field: str, *, positive: bool = False, allow_null: bool = False, allow_negative: bool = False) -> str | None:
    if value is None or str(value).strip() == "":
        if allow_null:
            return None
        raise PilotReceiptError(f"{field}_missing")
    from decimal import Decimal, InvalidOperation
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PilotReceiptError(f"{field}_invalid") from exc
    if not result.is_finite():
        raise PilotReceiptError(f"{field}_non_finite")
    if not allow_negative and result < 0:
        raise PilotReceiptError(f"{field}_out_of_range")
    if positive and result <= 0:
        raise PilotReceiptError(f"{field}_out_of_range")
    return format(result, "f")


def _volume(value: Any, field: str, *, allow_null: bool = False) -> int | None:
    text = _decimal_text(value, field, allow_null=allow_null)
    if text is None:
        return None
    from decimal import Decimal
    result = Decimal(text)
    if result != result.to_integral_value():
        raise PilotReceiptError(f"{field}_must_be_integer")
    return int(result)


def _binary(value: Any, field: str) -> int:
    text = str(value).strip()
    if text not in {"0", "1"}:
        raise PilotReceiptError(f"{field}_must_be_0_or_1")
    return int(text)


def _raw_result(result: Any) -> tuple[list[str], list[dict[str, Any]]]:
    """Convert the SDK result into stable field-labelled raw rows."""
    if isinstance(result, Mapping):
        if "fields" not in result or "rows" not in result:
            raise PilotReceiptError("provider_fields_or_rows_missing")
        fields = list(result["fields"])
        rows = result.get("rows")
    else:
        raise PilotReceiptError("provider_result_shape_invalid")
    if tuple(fields) != FIELDS:
        raise PilotReceiptError("provider_fields_mismatch")
    output: list[dict[str, Any]] = []
    for row in rows or []:
        if isinstance(row, Mapping):
            if set(FIELDS) - set(row):
                raise PilotReceiptError("provider_row_fields_missing")
            output.append({field: row[field] for field in FIELDS})
        else:
            values = list(row)
            if len(values) != len(FIELDS):
                raise PilotReceiptError("provider_row_width_mismatch")
            output.append(dict(zip(FIELDS, values)))
    return list(FIELDS), output


def _read_result(result: Any) -> tuple[list[str], list[dict[str, Any]]]:
    """Read all SDK pages and reject an unverified full terminal page."""
    required_attrs = ("error_code", "error_msg", "fields", "data", "cur_row_num", "next", "get_row_data")
    if any(not hasattr(result, name) for name in required_attrs):
        raise PilotReceiptError("provider_sdk_result_incomplete")
    error_code = str(result.error_code)
    if error_code != "0":
        raise PilotReceiptError(f"provider_error:{error_code}:{result.error_msg}")
    fields = list(result.fields)
    if tuple(fields) != FIELDS:
        raise PilotReceiptError("provider_fields_mismatch")
    rows: list[dict[str, Any]] = []
    while result.next():
        values = list(result.get_row_data())
        if len(values) != len(fields):
            raise PilotReceiptError("provider_row_width_mismatch")
        rows.append(dict(zip(fields, values)))
    if str(result.error_code) != "0":
        raise PilotReceiptError(f"provider_pagination_error:{result.error_code}")
    page = result.data
    if len(page) == 2000 and result.cur_row_num >= len(page):
        raise PilotReceiptError("provider_pagination_terminal_page_unverified")
    return fields, rows


def _fetch_provider_rows(codes: list[str], session_date: date) -> dict[str, Any]:
    """Fetch exact unadjusted daily rows from the fixed BaoStock provider."""
    try:
        import baostock as bs
    except ImportError as exc:  # pragma: no cover - production dependency
        raise PilotReceiptError("baostock_unavailable") from exc
    login_result = None
    try:
        login_result = bs.login()
        if login_result is None or not hasattr(login_result, "error_code") or str(login_result.error_code) != "0":
            raise PilotReceiptError(f"provider_login_failed:{getattr(login_result, 'error_code', '')}")
        raw_rows: list[dict[str, Any]] = []
        fields: list[str] = []
        for code in codes:
            number, exchange = code.split(".")
            result = bs.query_history_k_data_plus(
                f"{exchange.lower()}.{number}",
                fields=",".join(FIELDS),
                start_date=session_date.isoformat(),
                end_date=session_date.isoformat(),
                frequency="d",
                adjustflag="3",
            )
            current_fields, current_rows = _read_result(result)
            if not fields:
                fields = current_fields
            elif fields != current_fields:
                raise PilotReceiptError("provider_fields_mismatch")
            raw_rows.extend(current_rows)
        return {"fields": fields, "rows": raw_rows}
    except PilotReceiptError:
        raise
    except Exception as exc:
        raise PilotReceiptError(f"provider_fetch_failed:{type(exc).__name__}") from exc
    finally:
        try:
            bs.logout()
        except (AttributeError, OSError, RuntimeError):
            pass


def _normalize_rows(raw: dict[str, Any], codes: list[str], session_date: date, received_at: str) -> list[dict[str, Any]]:
    _fields, rows = _raw_result(raw)
    expected = set(codes)
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        code = _canonical_code(row.get("code"))
        if code not in expected or code in seen:
            raise PilotReceiptError("provider_code_duplicate_or_unexpected")
        if _parse_day(row.get("date"), "provider_row_date") != session_date:
            raise PilotReceiptError("provider_row_date_mismatch")
        trade_status = _binary(row.get("tradestatus"), "tradestatus")
        is_st = _binary(row.get("isST"), "isST")
        suspended = trade_status == 0
        numeric: dict[str, Any] = {}
        price_fields = {"open", "high", "low", "close", "preclose"}
        for field in price_fields | {"amount", "turn"}:
            numeric[field] = _decimal_text(
                row.get(field), field, positive=(field in price_fields and not suspended), allow_null=suspended,
            )
        numeric["volume"] = _volume(row.get("volume"), "volume", allow_null=suspended)
        numeric["pctChg"] = _decimal_text(row.get("pctChg"), "pctChg", allow_null=suspended, allow_negative=True)
        if not suspended and any(numeric[field] is None for field in (*price_fields, "volume", "amount", "turn", "pctChg")):
            raise PilotReceiptError("provider_numeric_field_missing")
        if not suspended:
            from decimal import Decimal
            prices = [Decimal(numeric[field]) for field in ("open", "high", "low", "close")]
            if Decimal(numeric["high"]) < max(prices) or Decimal(numeric["low"]) > min(prices):
                raise PilotReceiptError("provider_ohlc_inconsistent")
        seen.add(code)
        normalized.append({
            "code": code, "date": session_date.isoformat(),
            **numeric, "tradestatus": trade_status, "isST": is_st,
            "is_suspended": suspended, "source": PROVIDER, "received_at": received_at,
        })
    if seen != expected:
        raise PilotReceiptError("provider_partial_or_missing_codes")
    return sorted(normalized, key=lambda item: item["code"])


def _result_root() -> Path:
    configured = Path(settings.result_dir)
    if configured.exists() and configured.is_symlink():
        raise PilotReceiptError("pilot_receipt_result_root_symlink")
    root = configured.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_atomic_attempt(root: Path, *, raw: dict[str, Any], normalized: list[dict[str, Any]], metadata: dict[str, Any]) -> tuple[str, str, str]:
    parent = root / "manual-pilot-receipts"
    if parent.exists() and parent.is_symlink():
        raise PilotReceiptError("pilot_receipt_parent_symlink")
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".receipt-staging-", dir=parent))
    final = parent / f"{metadata['pilot_id']}-{metadata['session_date']}-{uuid4().hex}"
    try:
        raw_payload = {"schema_version": "manual-pilot-raw-v1", **metadata, "fields": list(FIELDS), "rows": raw["rows"]}
        normalized_payload = {"schema_version": "manual-pilot-normalized-v1", **metadata, "rows": normalized}
        raw_bytes = json.dumps(raw_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        normalized_bytes = json.dumps(normalized_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        raw_path, normalized_path = staging / "raw.json", staging / "normalized.json"
        raw_path.write_bytes(raw_bytes); normalized_path.write_bytes(normalized_bytes)
        for path in (raw_path, normalized_path):
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        files = {
            "raw.json": {"size": len(raw_bytes), "sha256": _sha256_bytes(raw_bytes)},
            "normalized.json": {"size": len(normalized_bytes), "sha256": _sha256_bytes(normalized_bytes)},
        }
        content_hash = _sha256_json({"metadata": metadata, "files": files})
        manifest_payload = {"schema_version": "manual-pilot-receipt-v1", **metadata, "files": files, "content_hash": content_hash}
        manifest_bytes = json.dumps(manifest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        manifest_path = staging / "manifest.json"
        manifest_path.write_bytes(manifest_bytes)
        with manifest_path.open("rb") as handle:
            os.fsync(handle.fileno())
        staging_fd = os.open(staging, os.O_RDONLY)
        try:
            os.fsync(staging_fd)
        finally:
            os.close(staging_fd)
        if final.exists():
            raise PilotReceiptError("pilot_receipt_attempt_exists")
        os.replace(staging, final)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return final.relative_to(root).as_posix() + "/manifest.json", _sha256_bytes(manifest_bytes), content_hash
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _check_admission(snapshot: dict[str, Any], *, now: datetime) -> tuple[date, list[str], datetime]:
    protocol = snapshot["protocol"]
    if snapshot["pilot_data_mode"] != "real_forward":
        raise PilotReceiptError("pilot_binding_requires_real_forward")
    if snapshot["release_status"] != "paper_observing":
        raise PilotReceiptError("release_requires_paper_observing")
    if snapshot["pilot_status"] != "observing":
        raise PilotReceiptError("pilot_requires_observing")
    if not isinstance(protocol.get("expected_dates"), list) or "started_at" not in protocol or "universe" not in protocol or "close_capture_not_before" not in protocol:
        raise PilotReceiptError("pilot_binding_protocol_fields_missing")
    dates = [_parse_day(value, "expected_date") for value in protocol["expected_dates"]]
    if not dates:
        raise PilotReceiptError("pilot_expected_dates_missing")
    current_day = now.astimezone(SHANGHAI_TZ).date()
    if current_day not in dates:
        raise PilotReceiptError("pilot_session_date_not_expected")
    started = _parse_timestamp(protocol["started_at"], "started_at")
    if current_day <= started.astimezone(SHANGHAI_TZ).date():
        raise PilotReceiptError("pilot_session_date_not_after_started")
    cutoff = str(protocol["close_capture_not_before"])
    try:
        cutoff_time = time.fromisoformat(cutoff)
    except ValueError as exc:
        raise PilotReceiptError("pilot_close_capture_window_invalid") from exc
    if now.astimezone(SHANGHAI_TZ).time() < cutoff_time:
        raise PilotReceiptError("pilot_close_capture_window_not_open")
    universe = [_canonical_code(value) for value in protocol["universe"]]
    if not universe or len(set(universe)) != len(universe):
        raise PilotReceiptError("pilot_universe_invalid")
    return current_day, sorted(universe), started


def capture_pilot_market_receipt(db: Session, *, pilot_id: str, actor: str, idempotency_key: str) -> ManualPilotMarketReceipt:
    actor = str(actor).strip(); idempotency_key = str(idempotency_key).strip()
    if not actor or not idempotency_key:
        raise PilotReceiptError("pilot_receipt_actor_and_request_key_required")
    request_hash = _sha256_json({"pilot_id": pilot_id, "actor": actor, "request_key": idempotency_key})
    existing = db.scalars(select(ManualPilotMarketReceipt).where(ManualPilotMarketReceipt.request_key == idempotency_key)).first()
    if existing:
        if existing.request_hash != request_hash:
            raise PilotReceiptError("pilot_receipt_idempotency_conflict")
        verify_pilot_market_receipt(db, existing.id)
        return existing
    before = _admission_snapshot(db, pilot_id)
    before_db = _db_snapshot(db, pilot_id)
    capture_started = _utc_now()
    session_date, codes, _started = _check_admission(before, now=capture_started)
    same_day = db.scalars(select(ManualPilotMarketReceipt).where(
        ManualPilotMarketReceipt.pilot_id == pilot_id,
        ManualPilotMarketReceipt.session_date == session_date.isoformat(),
    )).first()
    if same_day:
        raise PilotReceiptError("pilot_receipt_session_already_captured")
    raw = _fetch_provider_rows(codes, session_date)
    received = _utc_now()
    if received.astimezone(SHANGHAI_TZ).date() != session_date:
        raise PilotReceiptError("pilot_receipt_capture_crossed_session")
    if received < capture_started or received.astimezone(SHANGHAI_TZ).date() != capture_started.astimezone(SHANGHAI_TZ).date():
        raise PilotReceiptError("pilot_receipt_capture_time_invalid")
    normalized = _normalize_rows(raw, codes, session_date, _iso(received))
    after = _admission_snapshot(db, pilot_id)
    if after != before:
        raise PilotReceiptError("pilot_receipt_parent_changed")
    metadata = {"pilot_id": pilot_id, "binding_id": before["binding_id"], "binding_hash": before["binding_hash"], "session_date": session_date.isoformat(), "provider": PROVIDER, "capture_started_at": _iso(capture_started), "received_at": _iso(received), "created_by": actor, "request_key": idempotency_key, "request_hash": request_hash, "collector_code_hash": _sha256_bytes(Path(__file__).read_bytes())}
    manifest_path, manifest_sha, content_hash = _write_atomic_attempt(_result_root(), raw=raw, normalized=normalized, metadata=metadata)
    try:
        ensure_savepoint_transaction(db)
        _lock_registry(db)
        fresh_db = _db_snapshot(db, pilot_id)
        if fresh_db != before_db:
            raise PilotReceiptError("pilot_receipt_parent_changed")
        if db.scalars(select(ManualPilotMarketReceipt).where(
            ManualPilotMarketReceipt.pilot_id == pilot_id,
            ManualPilotMarketReceipt.session_date == session_date.isoformat(),
        )).first() is not None:
            raise PilotReceiptError("pilot_receipt_session_already_captured")
        if db.scalars(select(ManualPilotMarketReceipt).where(ManualPilotMarketReceipt.request_key == idempotency_key)).first() is not None:
            raise PilotReceiptError("pilot_receipt_idempotency_conflict")
        row = ManualPilotMarketReceipt(
            pilot_id=pilot_id, binding_hash=before["binding_hash"], session_date=session_date.isoformat(), provider=PROVIDER,
            capture_started_at=_iso(capture_started), received_at=_iso(received), created_by=actor,
            request_key=idempotency_key, request_hash=request_hash, manifest_path=manifest_path,
            manifest_sha256=manifest_sha, content_hash=content_hash,
        )
        db.add(row)
        db.commit(); db.refresh(row)
    except Exception as exc:
        db.rollback()
        if isinstance(exc, PilotReceiptError):
            raise
        if isinstance(exc, IntegrityError):
            raise PilotReceiptError("pilot_receipt_concurrent_conflict") from exc
        raise PilotReceiptError("pilot_receipt_persist_failed") from exc
    return row


def _safe_file(root: Path, relative: str) -> Path:
    candidate = root / relative
    if any(part.is_symlink() for part in (root, *candidate.parents, candidate)):
        raise PilotReceiptError("pilot_receipt_symlink_forbidden")
    path = candidate.resolve()
    if root not in path.parents:
        raise PilotReceiptError("pilot_receipt_path_escape")
    if path.is_symlink() or not path.is_file():
        raise PilotReceiptError("pilot_receipt_file_invalid")
    return path


def verify_pilot_market_receipt(db: Session, receipt_id: str) -> dict[str, Any]:
    row = db.get(ManualPilotMarketReceipt, receipt_id)
    if row is None:
        raise PilotReceiptError("pilot_receipt_not_found")
    admission = _admission_snapshot(db, row.pilot_id)
    binding = db.get(ManualPilotBinding, admission["binding_id"])
    if binding is None:
        raise PilotReceiptError("pilot_binding_not_found")
    pilot = db.get(ManualProspectivePilot, row.pilot_id)
    release = db.get(StrategyRelease, binding.release_id)
    if pilot is None or release is None:
        raise PilotReceiptError("pilot_receipt_parent_not_found")
    protocol = _protocol(binding)
    if row.binding_hash != binding.binding_hash or pilot.release_id != binding.release_id:
        raise PilotReceiptError("pilot_receipt_binding_changed")
    root = _result_root()
    manifest_path = _safe_file(root, row.manifest_path)
    if _sha256_bytes(manifest_path.read_bytes()) != row.manifest_sha256:
        raise PilotReceiptError("pilot_receipt_manifest_hash_mismatch")
    attempt = manifest_path.parent
    names = {item.name for item in attempt.iterdir()}
    if names != {"raw.json", "normalized.json", "manifest.json"}:
        raise PilotReceiptError("pilot_receipt_files_mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw_path = _safe_file(root, (attempt / "raw.json").relative_to(root).as_posix())
    normalized_path = _safe_file(root, (attempt / "normalized.json").relative_to(root).as_posix())
    raw = json.loads(raw_path.read_text(encoding="utf-8")); normalized_payload = json.loads(normalized_path.read_text(encoding="utf-8"))
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise PilotReceiptError("pilot_receipt_manifest_files_missing")
    for name, path in (("raw.json", raw_path), ("normalized.json", normalized_path)):
        item = files.get(name)
        if not isinstance(item, Mapping) or item.get("size") != path.stat().st_size or item.get("sha256") != _sha256_bytes(path.read_bytes()):
            raise PilotReceiptError("pilot_receipt_file_hash_mismatch")
    metadata_keys = ("pilot_id", "binding_id", "binding_hash", "session_date", "provider", "capture_started_at", "received_at", "created_by", "request_key", "request_hash", "collector_code_hash")
    metadata = {key: manifest.get(key) for key in metadata_keys}
    if manifest.get("content_hash") != row.content_hash or _sha256_json({"metadata": metadata, "files": files}) != row.content_hash:
        raise PilotReceiptError("pilot_receipt_content_hash_mismatch")
    if raw.get("rows") is None or normalized_payload.get("rows") is None:
        raise PilotReceiptError("pilot_receipt_rows_missing")
    current_day = _parse_day(row.session_date, "session_date")
    expected_identity = {
        "pilot_id": row.pilot_id, "binding_id": binding.id, "binding_hash": row.binding_hash,
        "session_date": row.session_date, "provider": row.provider,
        "capture_started_at": row.capture_started_at, "received_at": row.received_at,
        "created_by": row.created_by, "request_key": row.request_key, "request_hash": row.request_hash,
        "collector_code_hash": _sha256_bytes(Path(__file__).read_bytes()),
    }
    if any(manifest.get(key) != value for key, value in expected_identity.items()):
        raise PilotReceiptError("pilot_receipt_manifest_identity_mismatch")
    if _sha256_json({"pilot_id": row.pilot_id, "actor": row.created_by, "request_key": row.request_key}) != row.request_hash:
        raise PilotReceiptError("pilot_receipt_request_hash_mismatch")
    if manifest.get("schema_version") != "manual-pilot-receipt-v1" or raw.get("schema_version") != "manual-pilot-raw-v1" or normalized_payload.get("schema_version") != "manual-pilot-normalized-v1":
        raise PilotReceiptError("pilot_receipt_schema_version_invalid")
    for payload in (raw, normalized_payload):
        if any(payload.get(key) != manifest.get(key) for key in metadata_keys):
            raise PilotReceiptError("pilot_receipt_payload_metadata_mismatch")
    if raw.get("fields") != list(FIELDS) or raw.get("pilot_id") != row.pilot_id or raw.get("binding_id") != binding.id or raw.get("created_by") != row.created_by or raw.get("request_key") != row.request_key:
        raise PilotReceiptError("pilot_receipt_raw_metadata_mismatch")
    if normalized_payload.get("pilot_id") != row.pilot_id or normalized_payload.get("binding_id") != binding.id or normalized_payload.get("created_by") != row.created_by or normalized_payload.get("request_key") != row.request_key:
        raise PilotReceiptError("pilot_receipt_normalized_metadata_mismatch")
    capture = _parse_timestamp(row.capture_started_at, "capture_started_at"); received = _parse_timestamp(row.received_at, "received_at")
    if received < capture or capture.astimezone(SHANGHAI_TZ).date() != current_day or received.astimezone(SHANGHAI_TZ).date() != current_day:
        raise PilotReceiptError("pilot_receipt_timestamp_window_invalid")
    if received > _utc_now() + timedelta(seconds=5):
        raise PilotReceiptError("pilot_receipt_received_at_in_future")
    if "started_at" not in protocol or "close_capture_not_before" not in protocol:
        raise PilotReceiptError("pilot_binding_protocol_fields_missing")
    started = _parse_timestamp(protocol["started_at"], "started_at")
    if capture < started or current_day <= started.astimezone(SHANGHAI_TZ).date():
        raise PilotReceiptError("pilot_receipt_started_at_window_invalid")
    try:
        cutoff = time.fromisoformat(str(protocol["close_capture_not_before"]))
    except ValueError as exc:
        raise PilotReceiptError("pilot_close_capture_window_invalid") from exc
    if capture.astimezone(SHANGHAI_TZ).time() < cutoff:
        raise PilotReceiptError("pilot_receipt_close_capture_window_invalid")
    if not isinstance(protocol.get("expected_dates"), list) or not isinstance(protocol.get("universe"), list):
        raise PilotReceiptError("pilot_binding_protocol_fields_missing")
    dates = [_parse_day(value, "expected_date") for value in protocol["expected_dates"]]
    if current_day not in dates:
        raise PilotReceiptError("pilot_receipt_session_date_not_expected")
    codes = sorted(_canonical_code(value) for value in protocol["universe"])
    reconstructed = _normalize_rows({"fields": list(FIELDS), "rows": raw["rows"]}, codes, current_day, row.received_at)
    if reconstructed != normalized_payload.get("rows"):
        raise PilotReceiptError("pilot_receipt_normalized_rows_mismatch")
    return {
        "verified": True,
        "receipt_id": row.id, "id": row.id, "pilot_id": row.pilot_id, "binding_hash": row.binding_hash,
        "session_date": row.session_date, "provider": row.provider,
        "capture_started_at": row.capture_started_at, "received_at": row.received_at,
        "created_by": row.created_by, "request_key": row.request_key,
        "request_hash": row.request_hash, "manifest_path": row.manifest_path,
        "manifest_sha256": row.manifest_sha256, "content_hash": row.content_hash,
        "normalized_rows": reconstructed,
    }


__all__ = ["PilotReceiptError", "_fetch_provider_rows", "_utc_now", "capture_pilot_market_receipt", "verify_pilot_market_receipt"]
