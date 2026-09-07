"""Read-only Phase 3C operational checks and safe backup envelopes."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from server.config import settings
from server.services.live_readiness import _control, list_audit_events, list_drafts
from server.models.schema import BrokerConnection

_BACKUP_SCHEMA_VERSION = 1
_SAFE_DRAFT_FIELDS = (
    "id", "connection_id", "paper_account_id", "code", "side", "quantity",
    "limit_price", "price_as_of", "price_freshness", "notional",
    "estimated_fee", "cash_before", "cash_after", "risk_status", "status",
    "confirmed_at", "created_at", "updated_at",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def operations_status(db: Session) -> dict[str, Any]:
    control = _control(db)
    connections = db.query(BrokerConnection).all()
    drafts = list_drafts(db, limit=200)
    audits = list_audit_events(db, limit=1)
    alerts: list[dict[str, str]] = []
    if not control.kill_switch_active:
        alerts.append({"code": "kill_switch_inactive", "severity": "warning",
                       "message": "Kill Switch 当前未开启；实盘仍受配置和执行开关阻断。"})
    if any(row.status in {"unavailable", "error"} for row in connections):
        alerts.append({"code": "connection_unavailable", "severity": "info",
                       "message": "存在不可用连接登记；请等待官方适配器和沙盒健康检查。"})
    if any(row["status"] == "blocked" for row in drafts):
        alerts.append({"code": "blocked_drafts_present", "severity": "info",
                       "message": "存在被风控或安全门阻断的订单草案。"})
    if not audits:
        alerts.append({"code": "audit_empty", "severity": "info",
                       "message": "尚无审计事件；首次操作后会自动记录。"})
    if not settings.operator_token:
        alerts.append({"code": "operator_auth_unconfigured", "severity": "info",
                       "message": "未配置操作员令牌；请保持 API 仅绑定 loopback。"})
    return {
        "status": "degraded" if any(item["severity"] == "warning" for item in alerts) else "ok",
        "generated_at": _now(),
        "alerts": alerts,
        "counts": {
            "connections": len(connections),
            "enabled_connections": sum(1 for row in connections if row.enabled),
            "drafts": len(drafts),
            "blocked_drafts": sum(1 for row in drafts if row["status"] == "blocked"),
            "audit_events_observed": len(audits),
        },
        "safety": {
            "kill_switch_active": bool(control.kill_switch_active),
            "live_enabled": bool(control.live_enabled),
            "credentials_in_backup": False,
            "restore_api_enabled": False,
            "operator_auth_configured": bool(settings.operator_token),
        },
    }


def _backup_payload(db: Session) -> dict[str, Any]:
    control = _control(db)
    connections = db.query(BrokerConnection).order_by(BrokerConnection.created_at.asc()).all()
    drafts = list_drafts(db, limit=200)
    audits = list_audit_events(db, limit=500)
    return {
        "schema_version": _BACKUP_SCHEMA_VERSION,
        "created_at": _now(),
        "control": {
            "live_enabled": bool(control.live_enabled),
            "kill_switch_active": bool(control.kill_switch_active),
            "reason_present": bool(control.reason),
            "updated_by": control.updated_by,
            "updated_at": control.updated_at,
        },
        "connections": [
            {
                "id": row.id,
                "provider": row.provider,
                "account_ref": row.account_ref,
                "mode": row.mode,
                "status": row.status,
                "enabled": bool(row.enabled),
                "credential_ref_configured": bool(row.credential_ref),
                "last_error_present": bool(row.last_error),
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
            for row in connections
        ],
        # A draft contains caller-controlled strings (for example
        # ``price_source`` and ``idempotency_key``).  Keep only the fields
        # required to understand the safety state; do not put free-form input
        # into a backup envelope.
        "drafts": [{field: draft[field] for field in _SAFE_DRAFT_FIELDS} for draft in drafts],
        "audit": {
            "latest_id": audits[0]["id"] if audits else None,
            "observed_count": len(audits),
        },
        "safety": {
            "credentials_included": False,
            "restore_supported": False,
        },
    }


def create_backup(db: Session) -> dict[str, Any]:
    payload = _backup_payload(db)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "algorithm": "sha256",
        "checksum": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "payload": payload,
    }


def verify_backup(backup: Any) -> dict[str, Any]:
    if not isinstance(backup, dict) or not isinstance(backup.get("payload"), dict):
        raise ValueError("backup payload is required")
    if backup.get("algorithm") != "sha256" or not isinstance(backup.get("checksum"), str):
        raise ValueError("backup checksum envelope is invalid")
    payload = backup["payload"]
    if payload.get("schema_version") != _BACKUP_SCHEMA_VERSION:
        raise ValueError("unsupported backup schema_version")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {"valid": backup["checksum"] == expected, "algorithm": "sha256",
            "expected_checksum": expected, "restore_supported": False}
