"""H2d admission for a release-bound real-forward paper pilot.

This module is the only service which can open the explicit paper-observing
window.  The older generic pilot service remains useful for engineering
fixtures, but it is intentionally not a qualification path.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from quant_engine.data.calendar import TradingCalendar
from server.config import settings
from server.models.database import ensure_savepoint_transaction
from server.models.schema import (
    ManualPilotBinding,
    ManualProspectivePilot,
    ManualHoldoutBinding,
    ManualHoldoutEvaluation,
    ResearchEvidenceArtifact,
    StrategyPromotionEvaluation,
    StrategyRelease,
    manual_now_str,
    uuid4_str,
)
from server.services.manual_evidence import holdout_commit_snapshot
from server.services.manual_holdout_evaluation import verify_completed_holdout
from server.services.manual_holdout_promotion import resolve_holdout_passed
from server.services.manual_pilot import ManualPilotError
from server.services.research_holdout import _lock_registry


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
TARGET_DAYS = 30
CLOSE_CAPTURE_NOT_BEFORE = "16:30:00"
RESOLVER_VERSION = "manual-pilot-admission-v1"
MAIN_BOARD_RE = re.compile(r"^(?:(?:600|601|603|605)\d{3}\.SH|(?:000|001|002)\d{3}\.SZ)$")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    """Return the only production clock used by admission."""
    return datetime.now(timezone.utc)


def _obj(value: str | Mapping[str, Any], field: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ManualPilotError(f"{field}_invalid") from exc
    if not isinstance(parsed, dict):
        raise ManualPilotError(f"{field}_invalid")
    return parsed


def _decimal(value: Any, field: str, *, positive: bool = False, maximum: Decimal | None = None) -> Decimal:
    if isinstance(value, bool):
        raise ManualPilotError(f"{field}_must_be_ratio")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ManualPilotError(f"{field}_must_be_ratio") from exc
    if not result.is_finite() or (positive and result <= 0) or (not positive and result < 0):
        raise ManualPilotError(f"{field}_must_be_ratio")
    if maximum is not None and result > maximum:
        raise ManualPilotError(f"{field}_exceeds_limit")
    return result.normalize()


def _calendar_snapshot(start_date: date) -> dict[str, Any]:
    """Load and verify a real exchange calendar; weekday fallback is invalid."""
    calendar = TradingCalendar(start_year=start_date.year, end_year=start_date.year + 2)
    candidates = calendar.get_trading_days(start_date + timedelta(days=1), start_date + timedelta(days=366))
    if len(candidates) < TARGET_DAYS:
        raise ManualPilotError("TRADING_CALENDAR_UNAVAILABLE")
    last_day = candidates[TARGET_DAYS - 1]
    report = calendar.ensure_coverage(start_date, last_day)
    if not isinstance(report, dict) or report.get("complete") is not True or report.get("verified") is not True:
        raise ManualPilotError("TRADING_CALENDAR_UNAVAILABLE")
    raw_days = report.get("trading_days")
    if not isinstance(raw_days, list):
        raise ManualPilotError("TRADING_CALENDAR_UNAVAILABLE")
    try:
        days = [date.fromisoformat(str(value)) for value in raw_days]
    except ManualPilotError:
        raise
    except (TypeError, ValueError) as exc:
        raise ManualPilotError("TRADING_CALENDAR_UNAVAILABLE") from exc
    expected = [day for day in days if day > start_date][:TARGET_DAYS]
    if len(expected) != TARGET_DAYS or len(set(expected)) != TARGET_DAYS or expected != sorted(expected):
        raise ManualPilotError("TRADING_CALENDAR_UNAVAILABLE")
    if any(day <= start_date for day in expected):
        raise ManualPilotError("TRADING_CALENDAR_UNAVAILABLE")
    if expected[-1] != last_day:
        raise ManualPilotError("TRADING_CALENDAR_UNAVAILABLE")
    return {
        "report": dict(report),
        "dates": [day.isoformat() for day in expected],
        "source": str(report.get("source") or ""),
        "content_hash": str(report.get("content_hash") or ""),
        "coverage_start": report.get("coverage_start"),
        "coverage_end": report.get("coverage_end"),
    }


def _code_map() -> dict[str, str]:
    root = Path(__file__).resolve().parents[2]
    names = {
        "admission_service": root / "server/services/manual_pilot_admission.py",
        "manual_pilot_service": root / "server/services/manual_pilot.py",
        "holdout_promotion_service": root / "server/services/manual_holdout_promotion.py",
        "trading_calendar": root / "quant_engine/data/calendar.py",
    }
    result: dict[str, str] = {}
    try:
        for name, path in names.items():
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ManualPilotError("pilot_code_identity_unavailable") from exc
    return result


def _holdout_request_hash(release: StrategyRelease, evaluation: StrategyPromotionEvaluation, refs: Mapping[str, Any]) -> str:
    return _hash({
        "release_id": release.id,
        "release_hash": release.release_hash,
        "target_status": "holdout_passed",
        "evidence_refs": dict(refs),
        "actor": evaluation.actor,
    })


def _promotion_evaluation_hash(evaluation: StrategyPromotionEvaluation, checks: Mapping[str, bool], refs: Mapping[str, Any]) -> str:
    return _hash({
        "release_id": evaluation.release_id,
        "release_hash": evaluation.release_hash,
        "from_status": evaluation.from_status,
        "target_status": evaluation.target_status,
        "policy_hash": evaluation.policy_hash,
        "evidence_refs": dict(refs),
        "resolved_evidence_hash": evaluation.resolved_evidence_hash,
        "checks": dict(checks),
        "decision": evaluation.decision,
        "resolver_version": evaluation.resolver_version,
        "resolver_code_hash": evaluation.resolver_code_hash,
        "previous_evaluation_hash": evaluation.previous_evaluation_hash,
        "actor": evaluation.actor,
        "request_hash": evaluation.request_hash,
    })


def _verify_holdout_chain(
    db: Session,
    release: StrategyRelease,
    *,
    require_portfolio_status: bool,
) -> tuple[StrategyPromotionEvaluation, ResearchEvidenceArtifact, dict[str, Any]]:
    evaluations = db.scalars(select(StrategyPromotionEvaluation).where(
        StrategyPromotionEvaluation.release_id == release.id,
        StrategyPromotionEvaluation.target_status == "holdout_passed",
        StrategyPromotionEvaluation.decision == "passed",
    ).order_by(StrategyPromotionEvaluation.created_at.asc(), StrategyPromotionEvaluation.id.asc())).all()
    if len(evaluations) != 1:
        raise ManualPilotError("holdout_passed_evaluation_count_invalid")
    evaluation = evaluations[0]
    if evaluation.from_status != "portfolio_passed" or evaluation.release_hash != release.release_hash:
        raise ManualPilotError("holdout_passed_evaluation_chain_invalid")
    refs = _obj(evaluation.evidence_refs_json, "holdout_evaluation_refs")
    if set(refs) != {"holdout_artifact_id"}:
        raise ManualPilotError("holdout_evaluation_refs_invalid")
    artifact = db.get(ResearchEvidenceArtifact, refs["holdout_artifact_id"])
    if artifact is None or artifact.kind != "holdout_result" or artifact.status != "verified":
        raise ManualPilotError("holdout_result_artifact_invalid")
    verified = verify_completed_holdout(db, artifact.id)
    resolved = resolve_holdout_passed(
        db, release, refs, require_portfolio_status=require_portfolio_status,
    )
    if not resolved.passed or evaluation.resolved_evidence_hash != resolved.resolved_evidence_hash:
        raise ManualPilotError("holdout_evaluation_evidence_hash_mismatch")
    if evaluation.request_hash != _holdout_request_hash(release, evaluation, refs):
        raise ManualPilotError("holdout_evaluation_request_hash_mismatch")
    checks = _obj(evaluation.checks_json, "holdout_evaluation_checks")
    if not checks or any(type(value) is not bool or not value for value in checks.values()):
        raise ManualPilotError("holdout_evaluation_checks_invalid")
    if checks != dict(resolved.checks):
        raise ManualPilotError("holdout_evaluation_checks_mismatch")
    if evaluation.policy_hash != _hash(_obj(release.promotion_policy, "promotion_policy")):
        raise ManualPilotError("holdout_evaluation_policy_hash_mismatch")
    if verified.get("parent_portfolio_evaluation_id") != evaluation.previous_evaluation_id or verified.get("parent_portfolio_evaluation_hash") != evaluation.previous_evaluation_hash:
        raise ManualPilotError("holdout_previous_evaluation_chain_invalid")
    if evaluation.evaluation_hash != _promotion_evaluation_hash(evaluation, checks, refs):
        raise ManualPilotError("holdout_evaluation_hash_mismatch")
    if verified.get("artifact_id") != artifact.id or verified.get("evaluation_id") != _holdout_evaluation_id(artifact, evaluation):
        raise ManualPilotError("holdout_result_identity_mismatch")
    return evaluation, artifact, verified


def _holdout_evaluation_id(artifact: ResearchEvidenceArtifact, evaluation: StrategyPromotionEvaluation) -> str:
    # ``verify_completed_holdout`` returns the economic evaluation id, while
    # the promotion evaluation is the historical release-chain parent.
    return str(artifact.producer_entity_id)


def _universe_from_db(db: Session, evaluation: StrategyPromotionEvaluation, artifact: ResearchEvidenceArtifact) -> list[str]:
    from server.models.schema import ManualHoldoutEvaluation

    economic = db.get(ManualHoldoutEvaluation, artifact.producer_entity_id)
    if economic is None:
        raise ManualPilotError("holdout_evaluation_not_found")
    payload = _obj(economic.input_json, "holdout_evaluation_input")
    dataset = payload.get("dataset")
    if not isinstance(dataset, dict) or not isinstance(dataset.get("manifest_path"), str):
        raise ManualPilotError("holdout_dataset_manifest_missing")
    source_root = Path(settings.result_dir).resolve(strict=True).parent
    manifest_path = (source_root / dataset["manifest_path"]).resolve()
    try:
        manifest_path.relative_to(source_root)
        if str(dataset.get("manifest_sha256")) != hashlib.sha256(manifest_path.read_bytes()).hexdigest():
            raise ManualPilotError("holdout_dataset_manifest_changed")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ManualPilotError("holdout_dataset_manifest_invalid") from exc
    universe = manifest.get("universe") if isinstance(manifest, dict) else None
    if not isinstance(universe, list) or not universe:
        raise ManualPilotError("holdout_universe_unavailable")
    raw = [str(code).strip().upper() for code in universe]
    if not raw or any(not MAIN_BOARD_RE.fullmatch(code) for code in raw) or len(raw) != len(set(raw)):
        raise ManualPilotError("holdout_universe_invalid")
    return sorted(raw)


def _assert_forward_temporal(
    db: Session,
    release_id: str,
    holdout_promotion: StrategyPromotionEvaluation,
    start_date: date,
    started_at: str,
) -> None:
    bindings = db.scalars(select(ManualHoldoutBinding).where(ManualHoldoutBinding.release_id == release_id)).all()
    if not bindings:
        raise ManualPilotError("holdout_read_scope_missing")
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        if started < datetime.fromisoformat(holdout_promotion.created_at.replace("Z", "+00:00")):
            raise ManualPilotError("pilot_start_before_holdout_completion")
        for binding in bindings:
            evaluations = db.scalars(select(ManualHoldoutEvaluation).where(ManualHoldoutEvaluation.binding_id == binding.id)).all()
            if len(evaluations) != 1:
                raise ManualPilotError("holdout_read_scope_missing")
            economic = evaluations[0]
            if economic.status != "completed" or not economic.read_scope_start or not economic.read_scope_end or not economic.created_at or not economic.completed_at:
                raise ManualPilotError("holdout_read_scope_missing")
            if start_date <= date.fromisoformat(economic.read_scope_end):
                raise ManualPilotError("pilot_start_must_follow_holdout_scope")
            for value in (economic.created_at, economic.completed_at):
                if started < datetime.fromisoformat(value.replace("Z", "+00:00")):
                    raise ManualPilotError("pilot_start_before_holdout_completion")
    except ManualPilotError:
        raise
    except (TypeError, ValueError) as exc:
        raise ManualPilotError("holdout_timestamps_invalid") from exc


def _risk_policy(release: StrategyRelease, maximum_daily_loss: Any) -> tuple[dict[str, Any], Decimal]:
    daily_loss = _decimal(maximum_daily_loss, "maximum_daily_loss", positive=True, maximum=Decimal("1"))
    promotion = _obj(release.promotion_policy, "promotion_policy")
    if "minimum_paper_days" not in promotion or "maximum_stress_drawdown" not in promotion:
        raise ManualPilotError("unsupported_pilot_promotion_policy")
    minimum_paper_days = promotion["minimum_paper_days"]
    try:
        normalized_days = int(minimum_paper_days)
    except (TypeError, ValueError, OverflowError):
        raise ManualPilotError("unsupported_pilot_promotion_policy") from None
    if isinstance(minimum_paper_days, bool) or str(minimum_paper_days).strip() != str(normalized_days):
        raise ManualPilotError("unsupported_pilot_promotion_policy")
    if normalized_days != TARGET_DAYS:
        raise ManualPilotError("unsupported_pilot_promotion_policy")
    drawdown = _decimal(promotion["maximum_stress_drawdown"], "maximum_stress_drawdown", maximum=Decimal("0.20"))
    policy = {
        "policy_version": "manual-prospective-paper-v1",
        "target_days": TARGET_DAYS,
        "maximum_daily_loss": str(daily_loss),
        "maximum_daily_loss_breaches": 0,
        "maximum_drawdown": str(drawdown),
        "maximum_stress_drawdown": str(drawdown),
        "paper_only": True,
    }
    return policy, daily_loss


def _start_request(release: StrategyRelease, actor: str, request_key: str, daily_loss: Decimal) -> dict[str, Any]:
    return {
        "release_id": release.id,
        "release_hash": release.release_hash,
        "actor": actor,
        "request_key": request_key,
        "maximum_daily_loss": str(daily_loss),
    }


def _binding_view(db: Session, pilot: ManualProspectivePilot, binding: ManualPilotBinding) -> dict[str, Any]:
    protocol = _obj(binding.protocol_json, "pilot_binding_protocol")
    calendar = protocol.get("calendar") if isinstance(protocol.get("calendar"), dict) else {}
    hashes = {
        "binding_hash": binding.binding_hash,
        "protocol_hash": binding.protocol_hash,
        "release_hash": protocol.get("release", {}).get("release_hash"),
        "holdout_evaluation_hash": binding.holdout_evaluation_hash,
        "holdout_artifact_hash": binding.holdout_artifact_hash,
        "paper_observing_evaluation_hash": (
            db.get(StrategyPromotionEvaluation, protocol.get("paper_observing_evaluation_id")).evaluation_hash
            if db.get(StrategyPromotionEvaluation, protocol.get("paper_observing_evaluation_id")) is not None
            else None
        ),
    }
    return {
        "pilot_id": pilot.id,
        "binding_id": binding.id,
        "release_id": binding.release_id,
        "status": pilot.status,
        "release_status": db.get(StrategyRelease, binding.release_id).status,
        "data_mode": pilot.data_mode,
        "started_at": pilot.created_at,
        "start_date": pilot.start_date,
        "expected_dates": list(protocol.get("expected_dates", [])),
        "target_days": pilot.target_days,
        "capital": protocol.get("capital"),
        "policy": protocol.get("risk_policy", {}),
        "calendar": {
            "source": calendar.get("source"),
            "content_hash": calendar.get("content_hash"),
            "coverage_start": calendar.get("coverage_start"),
            "coverage_end": calendar.get("coverage_end"),
        },
        "universe": list(protocol.get("universe", [])),
        "close_capture_not_before": protocol.get("close_capture_not_before"),
        "hashes": hashes,
        "verified": True,
    }


def _paper_refs(binding: ManualPilotBinding, protocol: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "pilot_binding_id": binding.id,
        "pilot_binding_hash": binding.binding_hash,
        "holdout_evaluation_id": binding.holdout_evaluation_id,
        "holdout_evaluation_hash": binding.holdout_evaluation_hash,
        "holdout_artifact_id": binding.holdout_artifact_id,
        "holdout_artifact_hash": binding.holdout_artifact_hash,
        "protocol_hash": binding.protocol_hash,
    }


def _paper_checks() -> dict[str, bool]:
    return {
        "release_identity_valid": True,
        "holdout_chain_valid": True,
        "holdout_artifact_verified": True,
        "binding_frozen": True,
        "real_forward": True,
        "calendar_verified": True,
        "capital_inherited": True,
        "risk_policy_frozen": True,
    }


def _paper_resolved_hash(release: StrategyRelease, binding: ManualPilotBinding) -> str:
    return _hash({
        "release_id": release.id,
        "release_hash": release.release_hash,
        "target_status": "paper_observing",
        "binding_hash": binding.binding_hash,
        "protocol_hash": binding.protocol_hash,
        "holdout_evaluation_id": binding.holdout_evaluation_id,
        "holdout_evaluation_hash": binding.holdout_evaluation_hash,
        "holdout_artifact_hash": binding.holdout_artifact_hash,
    })


def _paper_request_hash(release: StrategyRelease, actor: str, refs: Mapping[str, Any]) -> str:
    return _hash({
        "release_id": release.id,
        "release_hash": release.release_hash,
        "target_status": "paper_observing",
        "evidence_refs": dict(refs),
        "actor": actor,
    })


def verify_pilot_binding(db: Session, pilot_id: str) -> dict[str, Any]:
    pilot = db.get(ManualProspectivePilot, pilot_id)
    if pilot is None:
        raise ManualPilotError("manual_pilot_not_found")
    bindings = db.scalars(select(ManualPilotBinding).where(ManualPilotBinding.pilot_id == pilot.id)).all()
    if len(bindings) != 1:
        raise ManualPilotError("manual_pilot_binding_count_invalid")
    binding = bindings[0]
    if binding is None:
        raise ManualPilotError("manual_pilot_binding_not_found")
    release = db.get(StrategyRelease, binding.release_id)
    if release is None:
        raise ManualPilotError("pilot_binding_release_not_found")
    if release.status not in {"paper_observing", "paper_passed", "manual_ready"}:
        raise ManualPilotError("pilot_release_not_observing")
    if pilot.data_mode != "real_forward" or pilot.status not in {"observing", "passed", "failed", "blocked"}:
        raise ManualPilotError("manual_pilot_not_observing")
    protocol = _obj(binding.protocol_json, "pilot_binding_protocol")
    if binding.protocol_hash != _hash(protocol):
        raise ManualPilotError("pilot_binding_protocol_hash_mismatch")
    required_protocol = {
        "binding_version", "pilot_id", "binding_id", "data_mode", "paper_observing_evaluation_id",
        "release", "holdout", "capital", "actor", "started_at", "start_date", "expected_dates",
        "calendar", "universe", "close_capture_not_before", "risk_policy", "code_hashes",
    }
    if set(protocol) != required_protocol or protocol.get("binding_version") != "manual-pilot-binding-v1":
        raise ManualPilotError("pilot_binding_protocol_version_invalid")
    if protocol.get("pilot_id") != pilot.id or protocol.get("binding_id") != binding.id or protocol.get("release", {}).get("id") != binding.release_id:
        raise ManualPilotError("pilot_binding_identity_mismatch")
    try:
        started = datetime.fromisoformat(str(protocol["started_at"]).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ManualPilotError("pilot_started_at_invalid") from exc
    if started.tzinfo is None or started.astimezone(timezone.utc).isoformat() != pilot.created_at:
        raise ManualPilotError("pilot_started_at_mismatch")
    if protocol.get("start_date") != pilot.start_date or started.astimezone(SHANGHAI_TZ).date().isoformat() != pilot.start_date:
        raise ManualPilotError("pilot_start_date_mismatch")
    if pilot.target_days != TARGET_DAYS or protocol.get("data_mode") != "real_forward" or pilot.data_mode != "real_forward":
        raise ManualPilotError("pilot_identity_invalid")
    if pilot.strategy_fingerprint != release.strategy_fingerprint or protocol.get("release", {}).get("strategy_core_hash") != pilot.strategy_fingerprint:
        raise ManualPilotError("pilot_strategy_fingerprint_mismatch")
    expected_required = _canonical({"policy": protocol["risk_policy"], "capital": protocol["capital"], "binding_hash": binding.binding_hash})
    if pilot.required_evidence != expected_required:
        raise ManualPilotError("pilot_required_evidence_mismatch")
    if protocol.get("code_hashes") != _code_map():
        raise ManualPilotError("pilot_code_identity_mismatch")
    if protocol.get("holdout", {}).get("evaluation_id") != binding.holdout_evaluation_id or protocol.get("holdout", {}).get("evaluation_hash") != binding.holdout_evaluation_hash:
        raise ManualPilotError("pilot_holdout_protocol_mismatch")
    if protocol.get("holdout", {}).get("artifact_id") != binding.holdout_artifact_id or protocol.get("holdout", {}).get("artifact_hash") != binding.holdout_artifact_hash:
        raise ManualPilotError("pilot_holdout_protocol_mismatch")
    if binding.created_by != protocol.get("actor") or binding.created_at != protocol.get("started_at"):
        raise ManualPilotError("pilot_binding_actor_time_mismatch")
    if pilot.release_id != binding.release_id:
        raise ManualPilotError("pilot_release_identity_mismatch")
    release_payload = protocol.get("release")
    if release_payload != {"id": release.id, "release_hash": release.release_hash, "bundle_hash": release.bundle_hash, "strategy_core_hash": release.strategy_fingerprint}:
        raise ManualPilotError("pilot_release_protocol_mismatch")
    calendar = protocol.get("calendar")
    if not isinstance(calendar, dict) or set(calendar) != {"source", "content_hash", "coverage_start", "coverage_end", "report"}:
        raise ManualPilotError("pilot_calendar_report_invalid")
    report = calendar.get("report")
    if not isinstance(report, dict) or report.get("complete") is not True or report.get("verified") is not True:
        raise ManualPilotError("pilot_calendar_report_invalid")
    if (
        report.get("source") != calendar.get("source")
        or report.get("content_hash") != calendar.get("content_hash")
        or report.get("coverage_start") != calendar.get("coverage_start")
        or report.get("coverage_end") != calendar.get("coverage_end")
    ):
        raise ManualPilotError("pilot_calendar_report_invalid")
    report_days = report.get("trading_days")
    if not isinstance(report_days, list) or [day for day in report_days if str(day) > pilot.start_date][:TARGET_DAYS] != protocol.get("expected_dates"):
        raise ManualPilotError("pilot_calendar_dates_invalid")
    universe = protocol.get("universe")
    if not isinstance(universe, list) or len(universe) != len(set(universe)) or universe != sorted(universe) or any(not isinstance(code, str) or not MAIN_BOARD_RE.fullmatch(code) for code in universe):
        raise ManualPilotError("pilot_universe_invalid")
    if binding.binding_hash != _hash({
        "binding_id": binding.id, "pilot_id": binding.pilot_id, "release_id": binding.release_id,
        "holdout_evaluation_id": binding.holdout_evaluation_id,
        "holdout_evaluation_hash": binding.holdout_evaluation_hash,
        "holdout_artifact_id": binding.holdout_artifact_id,
        "holdout_artifact_hash": binding.holdout_artifact_hash,
        "protocol_hash": binding.protocol_hash, "request_key": binding.request_key,
        "request_hash": binding.request_hash, "created_by": binding.created_by,
        "created_at": binding.created_at,
    }):
        raise ManualPilotError("pilot_binding_hash_mismatch")
    artifact = db.get(ResearchEvidenceArtifact, binding.holdout_artifact_id)
    if release is None or artifact is None or release.release_hash != protocol.get("release", {}).get("release_hash"):
        raise ManualPilotError("pilot_binding_release_identity_mismatch")
    holdout_eval = db.get(StrategyPromotionEvaluation, binding.holdout_evaluation_id)
    if holdout_eval is None or holdout_eval.evaluation_hash != binding.holdout_evaluation_hash:
        raise ManualPilotError("pilot_binding_holdout_evaluation_missing")
    if artifact.evidence_hash != binding.holdout_artifact_hash:
        raise ManualPilotError("pilot_binding_holdout_artifact_hash_mismatch")
    # Revalidate the complete parent chain with the release's current status;
    # the current status may be paper_observing or a later paper state.
    checked_holdout, checked_artifact, verified = _verify_holdout_chain(db, release, require_portfolio_status=False)
    if checked_holdout.id != holdout_eval.id or checked_artifact.id != artifact.id:
        raise ManualPilotError("pilot_binding_holdout_chain_mismatch")
    _assert_forward_temporal(db, release.id, holdout_eval, date.fromisoformat(pilot.start_date), protocol["started_at"])
    if verified.get("parent_portfolio_evaluation_id") != holdout_eval.previous_evaluation_id or verified.get("parent_portfolio_evaluation_hash") != holdout_eval.previous_evaluation_hash:
        raise ManualPilotError("pilot_holdout_previous_chain_invalid")
    paper_id = protocol.get("paper_observing_evaluation_id")
    paper_eval = db.get(StrategyPromotionEvaluation, paper_id)
    if paper_eval is None or paper_eval.target_status != "paper_observing" or paper_eval.decision != "passed":
        raise ManualPilotError("pilot_paper_observing_evaluation_missing")
    refs = _obj(paper_eval.evidence_refs_json, "paper_observing_evaluation_refs")
    if refs != _paper_refs(binding, protocol) or paper_eval.from_status != "holdout_passed" or paper_eval.release_id != release.id or paper_eval.release_hash != release.release_hash:
        raise ManualPilotError("pilot_paper_observing_evaluation_refs_invalid")
    if paper_eval.previous_evaluation_id != holdout_eval.id or paper_eval.previous_evaluation_hash != holdout_eval.evaluation_hash:
        raise ManualPilotError("pilot_paper_observing_previous_chain_invalid")
    if paper_eval.actor != protocol.get("actor") or paper_eval.idempotency_key != binding.request_key or paper_eval.policy_hash != _hash(_obj(release.promotion_policy, "promotion_policy")):
        raise ManualPilotError("pilot_paper_observing_evaluation_metadata_invalid")
    if paper_eval.resolver_version != RESOLVER_VERSION or paper_eval.resolver_code_hash != _hash(protocol["code_hashes"]):
        raise ManualPilotError("pilot_paper_observing_resolver_identity_invalid")
    if paper_eval.checks_json != _canonical(_paper_checks()):
        raise ManualPilotError("pilot_paper_observing_checks_invalid")
    if paper_eval.resolved_evidence_hash != _paper_resolved_hash(release, binding):
        raise ManualPilotError("pilot_paper_observing_resolved_hash_invalid")
    if paper_eval.request_hash != _paper_request_hash(release, paper_eval.actor, refs):
        raise ManualPilotError("pilot_paper_observing_request_hash_invalid")
    if paper_eval.evaluation_hash != _promotion_evaluation_hash(paper_eval, _paper_checks(), refs):
        raise ManualPilotError("pilot_paper_observing_evaluation_hash_invalid")
    paper_rows = db.scalars(select(StrategyPromotionEvaluation).where(
        StrategyPromotionEvaluation.release_id == release.id,
        StrategyPromotionEvaluation.target_status == "paper_observing",
        StrategyPromotionEvaluation.decision == "passed",
    )).all()
    if len(paper_rows) != 1 or paper_rows[0].id != paper_eval.id:
        raise ManualPilotError("pilot_paper_observing_evaluation_count_invalid")
    expected = protocol.get("expected_dates")
    if "dates" in protocol or not isinstance(expected, list) or len(expected) != TARGET_DAYS or len(set(expected)) != TARGET_DAYS:
        raise ManualPilotError("pilot_calendar_dates_invalid")
    try:
        parsed_expected = [date.fromisoformat(str(value)) for value in expected]
    except (TypeError, ValueError) as exc:
        raise ManualPilotError("pilot_calendar_dates_invalid") from exc
    if pilot.start_date >= expected[0] or expected != sorted(expected):
        raise ManualPilotError("pilot_calendar_dates_invalid")
    if protocol.get("risk_policy", {}).get("paper_only") is not True:
        raise ManualPilotError("pilot_risk_policy_invalid")
    risk_policy = protocol["risk_policy"]
    if set(risk_policy) != {"policy_version", "target_days", "maximum_daily_loss", "maximum_daily_loss_breaches", "maximum_drawdown", "maximum_stress_drawdown", "paper_only"}:
        raise ManualPilotError("pilot_risk_policy_invalid")
    if _decimal(risk_policy["maximum_daily_loss"], "maximum_daily_loss", positive=True, maximum=Decimal("1")) <= 0:
        raise ManualPilotError("pilot_risk_policy_invalid")
    if _decimal(risk_policy["maximum_drawdown"], "maximum_drawdown", maximum=Decimal("0.20")) != _decimal(risk_policy["maximum_stress_drawdown"], "maximum_stress_drawdown", maximum=Decimal("0.20")):
        raise ManualPilotError("pilot_risk_policy_invalid")
    if _decimal(protocol["capital"], "holdout_capital", positive=True) <= 0:
        raise ManualPilotError("pilot_capital_invalid")
    daily_loss = protocol["risk_policy"].get("maximum_daily_loss")
    expected_policy, _ = _risk_policy(release, daily_loss)
    if protocol["risk_policy"] != expected_policy:
        raise ManualPilotError("pilot_risk_policy_mismatch")
    if binding.request_hash != _hash(_start_request(release, binding.created_by, binding.request_key, _decimal(daily_loss, "maximum_daily_loss", positive=True, maximum=Decimal("1")))):
        raise ManualPilotError("pilot_binding_request_hash_invalid")
    from server.models.schema import ManualHoldoutEvaluation
    economic = db.get(ManualHoldoutEvaluation, artifact.producer_entity_id)
    runtime = _obj(economic.input_json, "holdout_evaluation_input") if economic else {}
    original = runtime.get("original")
    if not isinstance(original, dict) or protocol["capital"] != str(_decimal(original.get("capital"), "holdout_capital", positive=True)):
        raise ManualPilotError("pilot_capital_inheritance_invalid")
    if protocol["universe"] != _universe_from_db(db, holdout_eval, artifact):
        raise ManualPilotError("pilot_universe_inheritance_invalid")
    if protocol["close_capture_not_before"] != CLOSE_CAPTURE_NOT_BEFORE:
        raise ManualPilotError("pilot_close_capture_window_invalid")
    if any(day <= date.fromisoformat(pilot.start_date) for day in parsed_expected):
        raise ManualPilotError("pilot_calendar_dates_invalid")
    if calendar["coverage_start"] is None or calendar["coverage_end"] is None or calendar["coverage_start"] > pilot.start_date or calendar["coverage_end"] < parsed_expected[-1].isoformat():
        raise ManualPilotError("pilot_calendar_coverage_invalid")
    if not str(calendar["source"]).startswith("akshare:"):
        raise ManualPilotError("pilot_calendar_source_unverified")
    return _binding_view(db, pilot, binding)


def start_paper_observation(
    db: Session,
    *,
    release_id: str,
    actor: str,
    idempotency_key: str,
    maximum_daily_loss: Any,
) -> ManualProspectivePilot:
    actor = str(actor).strip()
    request_key = str(idempotency_key).strip()
    if not actor:
        raise ManualPilotError("pilot_actor_required")
    if not request_key:
        raise ManualPilotError("pilot_idempotency_key_required")
    release = db.get(StrategyRelease, release_id)
    if release is None:
        raise ManualPilotError("strategy_release_not_found")
    policy, daily_loss = _risk_policy(release, maximum_daily_loss)
    request = _start_request(release, actor, request_key, daily_loss)
    request_hash = _hash(request)
    existing_request = db.scalars(select(ManualPilotBinding).where(ManualPilotBinding.request_key == request_key)).first()
    if existing_request is not None:
        if existing_request.request_hash != request_hash or existing_request.release_id != release.id:
            raise ManualPilotError("pilot_idempotency_conflict")
        pilot = db.get(ManualProspectivePilot, existing_request.pilot_id)
        if pilot is None:
            raise ManualPilotError("manual_pilot_not_found")
        verify_pilot_binding(db, pilot.id)
        return pilot
    if db.scalars(select(ManualPilotBinding).where(ManualPilotBinding.release_id == release.id)).first() is not None:
        raise ManualPilotError("pilot_release_already_bound")
    if db.scalars(select(ManualProspectivePilot).where(
        ManualProspectivePilot.release_id == release.id,
        ManualProspectivePilot.data_mode == "real_forward",
    )).first() is not None:
        raise ManualPilotError("legacy_real_forward_pilot_exists")
    if release.status != "holdout_passed":
        raise ManualPilotError("release_must_be_holdout_passed")

    snapshot = holdout_commit_snapshot(db, release.id)
    holdout_eval, artifact, _ = _verify_holdout_chain(db, release, require_portfolio_status=False)
    universe = _universe_from_db(db, holdout_eval, artifact)
    from server.models.schema import ManualHoldoutEvaluation
    economic = db.get(ManualHoldoutEvaluation, artifact.producer_entity_id)
    runtime = _obj(economic.input_json, "holdout_evaluation_input") if economic else {}
    original = runtime.get("original")
    if not isinstance(original, dict) or original.get("capital") is None:
        raise ManualPilotError("holdout_capital_missing")
    capital = original["capital"]
    capital_decimal = _decimal(capital, "holdout_capital", positive=True)
    started_moment = _utc_now()
    start_date = started_moment.astimezone(SHANGHAI_TZ).date()
    calendar = _calendar_snapshot(start_date)
    dates = list(calendar["dates"])
    started_at = started_moment.isoformat()
    _assert_forward_temporal(db, release.id, holdout_eval, start_date, started_at)
    try:
        ensure_savepoint_transaction(db)
        with db.begin_nested():
            _lock_registry(db)
            db.expire_all()
            fresh_release = db.get(StrategyRelease, release.id)
            if fresh_release is None or fresh_release.status != "holdout_passed" or fresh_release.release_hash != release.release_hash:
                raise ManualPilotError("pilot_release_changed_during_admission")
            if _utc_now().astimezone(SHANGHAI_TZ).date() != start_date:
                raise ManualPilotError("pilot_admission_crossed_shanghai_day")
            if holdout_commit_snapshot(db, release.id) != snapshot:
                raise ManualPilotError("pilot_holdout_snapshot_changed")
            if db.scalars(select(ManualPilotBinding).where(ManualPilotBinding.release_id == release.id)).first() is not None:
                raise ManualPilotError("pilot_release_already_bound")
            pilot_id, binding_id, paper_id = uuid4_str(), uuid4_str(), uuid4_str()
            protocol = {
                "binding_version": "manual-pilot-binding-v1",
                "pilot_id": pilot_id,
                "binding_id": binding_id,
                "data_mode": "real_forward",
                "paper_observing_evaluation_id": paper_id,
                "release": {"id": release.id, "release_hash": release.release_hash, "bundle_hash": release.bundle_hash, "strategy_core_hash": release.strategy_fingerprint},
                "holdout": {"evaluation_id": holdout_eval.id, "evaluation_hash": holdout_eval.evaluation_hash, "artifact_id": artifact.id, "artifact_hash": artifact.evidence_hash},
                "capital": str(capital_decimal),
                "actor": actor,
                "started_at": started_at,
                "start_date": start_date.isoformat(),
                "expected_dates": dates,
                "calendar": {"source": calendar["source"], "content_hash": calendar["content_hash"], "coverage_start": calendar["coverage_start"], "coverage_end": calendar["coverage_end"], "report": calendar["report"]},
                "universe": universe,
                "close_capture_not_before": CLOSE_CAPTURE_NOT_BEFORE,
                "risk_policy": policy,
                "code_hashes": _code_map(),
            }
            protocol_hash = _hash(protocol)
            binding_payload = {
                "binding_id": binding_id, "pilot_id": pilot_id, "release_id": release.id,
                "holdout_evaluation_id": holdout_eval.id, "holdout_evaluation_hash": holdout_eval.evaluation_hash,
                "holdout_artifact_id": artifact.id, "holdout_artifact_hash": artifact.evidence_hash,
                "protocol_hash": protocol_hash, "request_key": request_key, "request_hash": request_hash,
                "created_by": actor, "created_at": started_at,
            }
            binding_hash = _hash(binding_payload)
            policy_hash = _hash(_obj(release.promotion_policy, "promotion_policy"))
            evidence_refs = {"pilot_binding_id": binding_id, "pilot_binding_hash": binding_hash, "holdout_evaluation_id": holdout_eval.id, "holdout_evaluation_hash": holdout_eval.evaluation_hash, "holdout_artifact_id": artifact.id, "holdout_artifact_hash": artifact.evidence_hash, "protocol_hash": protocol_hash}
            resolved_hash = _hash({"release_id": release.id, "release_hash": release.release_hash, "target_status": "paper_observing", "binding_hash": binding_hash, "protocol_hash": protocol_hash, "holdout_evaluation_id": holdout_eval.id, "holdout_evaluation_hash": holdout_eval.evaluation_hash, "holdout_artifact_hash": artifact.evidence_hash})
            checks = {"release_identity_valid": True, "holdout_chain_valid": True, "holdout_artifact_verified": True, "binding_frozen": True, "real_forward": True, "calendar_verified": True, "capital_inherited": True, "risk_policy_frozen": True}
            evaluation = StrategyPromotionEvaluation(
                id=paper_id, release_id=release.id, release_hash=release.release_hash,
                from_status="holdout_passed", target_status="paper_observing", policy_hash=policy_hash,
                evidence_refs_json=_canonical(evidence_refs), resolved_evidence_hash=resolved_hash,
                checks_json=_canonical(checks), decision="passed", resolver_version=RESOLVER_VERSION,
                resolver_code_hash=_hash(_code_map()), actor=actor, idempotency_key=request_key,
                request_hash=_hash({"release_id": release.id, "release_hash": release.release_hash, "target_status": "paper_observing", "evidence_refs": evidence_refs, "actor": actor}),
                previous_evaluation_id=holdout_eval.id, previous_evaluation_hash=holdout_eval.evaluation_hash,
            )
            evaluation.evaluation_hash = _promotion_evaluation_hash(evaluation, checks, evidence_refs)
            protocol_json = _canonical(protocol)
            binding = ManualPilotBinding(
                id=binding_id, pilot_id=pilot_id, release_id=release.id,
                holdout_evaluation_id=holdout_eval.id, holdout_evaluation_hash=holdout_eval.evaluation_hash,
                holdout_artifact_id=artifact.id, holdout_artifact_hash=artifact.evidence_hash,
                protocol_json=protocol_json, protocol_hash=_hash(protocol), binding_hash=binding_hash,
                request_key=request_key, request_hash=request_hash, created_by=actor, created_at=started_at,
            )
            pilot = ManualProspectivePilot(
                id=pilot_id, pilot_key=f"{release.id}:{request_key}", release_id=release.id,
                strategy_fingerprint=release.strategy_fingerprint, start_date=start_date.isoformat(),
                target_days=TARGET_DAYS, data_mode="real_forward", status="observing",
                required_evidence=_canonical({"policy": policy, "capital": str(capital_decimal), "binding_hash": binding_hash}), created_at=started_at,
            )
            db.add(pilot)
            db.add(binding)
            db.add(evaluation)
            db.flush()
            changed = db.execute(update(StrategyRelease).where(
                StrategyRelease.id == release.id,
                StrategyRelease.release_hash == release.release_hash,
                StrategyRelease.status == "holdout_passed",
            ).values(status="paper_observing", updated_at=started_at))
            if changed.rowcount != 1:
                raise ManualPilotError("pilot_release_changed_during_admission")
        db.commit()
        db.refresh(pilot)
        return pilot
    except IntegrityError as exc:
        db.rollback()
        raise ManualPilotError("pilot_binding_conflict") from exc
    except Exception:
        db.rollback()
        raise


__all__ = ["ManualPilotError", "start_paper_observation", "verify_pilot_binding", "_utc_now", "_calendar_snapshot"]
