"""Evidence-bound manual holdout registration and access authorization."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
import hashlib
import json
import re
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.models.database import ensure_savepoint_transaction
from server.models.schema import (
    FactorExperiment,
    ManualHoldoutBinding,
    ManualHoldoutEvaluation,
    ResearchHoldoutAccess,
    ResearchHoldoutWindow,
    ResearchEvidenceArtifact,
    StrategyPromotionEvaluation,
    StrategyRelease,
    manual_now_str,
    uuid4_str,
)
from server.services.research_holdout import HoldoutError, _lock_registry, create_holdout_window


class ManualHoldoutError(HoldoutError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _sha(value: str, field: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise ManualHoldoutError(f"{field}_must_be_sha256")


def _date(value: date | str, field: str) -> date:
    try:
        if isinstance(value, datetime):
            return value.date()
        return value if isinstance(value, date) else date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ManualHoldoutError(f"{field}_must_be_iso") from exc


def _object(value: str, field: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ManualHoldoutError(f"{field}_invalid") from exc
    if not isinstance(parsed, dict):
        raise ManualHoldoutError(f"{field}_invalid")
    return parsed


def _raise(exc: Exception) -> ManualHoldoutError:
    if isinstance(exc, ManualHoldoutError):
        return exc
    return ManualHoldoutError(str(exc) or type(exc).__name__)


def _promotion_request_hash(release: StrategyRelease, evaluation: StrategyPromotionEvaluation, refs: Mapping[str, str]) -> str:
    request = {
        "release_id": release.id,
        "release_hash": release.release_hash,
        "target_status": "portfolio_passed",
        "evidence_refs": dict(refs),
        "actor": evaluation.actor,
    }
    return _hash(request)


def _verify_portfolio_parent(db: Session, release: StrategyRelease, *, require_portfolio_status: bool = True) -> StrategyPromotionEvaluation:
    """Re-resolve the sole passed portfolio evaluation and its research parent."""
    from server.services.manual_evidence import _release_identity_valid
    from server.services.manual_portfolio_promotion import _research_evaluation_valid, resolve_portfolio_passed

    if require_portfolio_status and release.status != "portfolio_passed":
        raise ManualHoldoutError("release_must_be_portfolio_passed")
    if not _release_identity_valid(release):
        raise ManualHoldoutError("release_identity_invalid")
    evaluations = db.scalars(select(StrategyPromotionEvaluation).where(
        StrategyPromotionEvaluation.release_id == release.id,
        StrategyPromotionEvaluation.target_status == "portfolio_passed",
        StrategyPromotionEvaluation.decision == "passed",
    )).all()
    if len(evaluations) != 1:
        raise ManualHoldoutError("portfolio_passed_evaluation_count_invalid")
    evaluation = evaluations[0]
    if evaluation.from_status != "research_passed" or evaluation.release_hash != release.release_hash:
        raise ManualHoldoutError("portfolio_passed_evaluation_chain_invalid")
    current_research = db.scalars(select(StrategyPromotionEvaluation).where(
        StrategyPromotionEvaluation.release_id == release.id,
        StrategyPromotionEvaluation.target_status == "research_passed",
        StrategyPromotionEvaluation.decision == "passed",
    )).all()
    if len(current_research) != 1 or evaluation.previous_evaluation_id != current_research[0].id:
        raise ManualHoldoutError("portfolio_passed_previous_research_invalid")
    refs = _object(evaluation.evidence_refs_json, "portfolio_evaluation_refs")
    if set(refs) != {"baseline_artifact_id", "stress_artifact_id"}:
        raise ManualHoldoutError("portfolio_evaluation_refs_invalid")
    policy_hash = _hash(_object(release.promotion_policy, "promotion_policy"))
    if evaluation.policy_hash != policy_hash:
        raise ManualHoldoutError("portfolio_evaluation_policy_hash_mismatch")
    checks = _object(evaluation.checks_json, "portfolio_evaluation_checks")
    if not checks or any(type(value) is not bool or not value for value in checks.values()):
        raise ManualHoldoutError("portfolio_evaluation_checks_invalid")
    try:
        previous_hash = _research_evaluation_valid(db, release, _object(release.research_evidence, "research_evidence"), policy_hash)[1]
        resolved = resolve_portfolio_passed(db, release, refs)
    except (ManualHoldoutError, HoldoutError):
        raise
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise _raise(exc) from exc
    if not resolved.passed or evaluation.resolved_evidence_hash != resolved.resolved_evidence_hash:
        raise ManualHoldoutError("portfolio_evaluation_evidence_hash_mismatch")
    if evaluation.previous_evaluation_id is None or evaluation.previous_evaluation_hash != previous_hash:
        raise ManualHoldoutError("portfolio_evaluation_previous_chain_invalid")
    if evaluation.request_hash != _promotion_request_hash(release, evaluation, refs):
        raise ManualHoldoutError("portfolio_evaluation_request_hash_mismatch")
    evaluation_payload = {
        "release_id": release.id,
        "release_hash": release.release_hash,
        "from_status": evaluation.from_status,
        "target_status": evaluation.target_status,
        "policy_hash": evaluation.policy_hash,
        "evidence_refs": refs,
        "resolved_evidence_hash": evaluation.resolved_evidence_hash,
        "checks": checks,
        "decision": evaluation.decision,
        "resolver_version": evaluation.resolver_version,
        "resolver_code_hash": evaluation.resolver_code_hash,
        "previous_evaluation_hash": evaluation.previous_evaluation_hash,
        "actor": evaluation.actor,
        "request_hash": evaluation.request_hash,
    }
    if evaluation.evaluation_hash != _hash(evaluation_payload):
        raise ManualHoldoutError("portfolio_evaluation_hash_mismatch")
    return evaluation


def _intervals_after_validation(db: Session, evaluation: StrategyPromotionEvaluation, start: date) -> None:
    refs = _object(evaluation.evidence_refs_json, "portfolio_evaluation_refs")
    from server.models.schema import ResearchEvidenceArtifact

    ends: list[date] = []
    research_refs = None
    for artifact_id in refs.values():
        artifact = db.get(ResearchEvidenceArtifact, artifact_id)
        if artifact is None:
            raise ManualHoldoutError("portfolio_artifact_registration_missing")
        manifest = _object(artifact.identity_json, "portfolio_artifact_identity")
        input_manifest = _object(artifact.manifest_json, "portfolio_artifact_manifest")
        for item in (manifest, input_manifest):
            for key in ("training_artifact_id", "validation_artifact_id"):
                if item.get(key):
                    research_refs = research_refs or {}
                    research_refs[key] = item[key]
    if not research_refs:
        raise ManualHoldoutError("portfolio_research_interval_missing")
    for artifact_id in research_refs.values():
        artifact = db.get(ResearchEvidenceArtifact, artifact_id)
        if artifact is None:
            raise ManualHoldoutError("research_artifact_missing")
        identity = _object(artifact.identity_json, "research_artifact_identity")
        try:
            ends.append(_date(identity["end_date"], "research_end_date"))
        except KeyError as exc:
            raise ManualHoldoutError("research_interval_missing") from exc
    if not ends or start <= max(ends):
        raise ManualHoldoutError("holdout_must_follow_training_validation")


def _factor_experiment_overlap(db: Session, start: date, end: date) -> None:
    rows = db.scalars(select(FactorExperiment).where(
        FactorExperiment.status.in_({"queued", "running", "completed", "failed"})
        | (FactorExperiment.attempt > 0)
    )).all()
    for row in rows:
        row_end = _date(row.end_date, "experiment_end_date")
        # Factor research currently reads the historical panel from the
        # dataset beginning through ``end_date``.  The table has no persisted
        # effective start, so a new holdout must begin after every queued or
        # previously attempted experiment's end date.
        if start <= row_end:
            raise ManualHoldoutError("holdout_range_overlaps_factor_experiment")


def _evaluation_scope_overlap(db: Session, start: date, end: date) -> None:
    """A frozen evaluation reserves the complete source read scope."""
    rows = db.scalars(select(ManualHoldoutEvaluation).where(
        ManualHoldoutEvaluation.read_scope_start.is_not(None),
        ManualHoldoutEvaluation.read_scope_end.is_not(None),
    )).all()
    for row in rows:
        scope_start, scope_end = _date(row.read_scope_start, "evaluation_scope_start"), _date(row.read_scope_end, "evaluation_scope_end")
        if start <= scope_end and end >= scope_start:
            raise ManualHoldoutError("holdout_range_overlaps_evaluation_read_scope")


def _protocol(release: StrategyRelease, evaluation: StrategyPromotionEvaluation, dataset_id: str, data_hash: str, start: date, end: date) -> dict[str, Any]:
    return {
        "protocol_version": "manual-holdout-preregistration-v1",
        "release": {
            "id": release.id,
            "release_hash": release.release_hash,
            "bundle_hash": release.bundle_hash,
            "strategy_core_hash": release.strategy_fingerprint,
            "strategy_key": release.strategy_key,
            "version": release.version,
        },
        "portfolio_parent": {
            "id": evaluation.id,
            "evaluation_hash": evaluation.evaluation_hash,
            "previous_evaluation_id": evaluation.previous_evaluation_id,
            "previous_evaluation_hash": evaluation.previous_evaluation_hash,
        },
        "holdout": {
            "dataset_id": dataset_id,
            "data_content_hash": data_hash,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        },
        "promotion_policy": _object(release.promotion_policy, "promotion_policy"),
        "execution_policy": _object(release.execution_policy, "execution_policy"),
        "prohibitions": {
            "training_feedback": True,
            "parameter_changes": True,
        },
    }


def create_manual_holdout(
    db: Session,
    *,
    release_id: str,
    dataset_id: str,
    data_content_hash: str,
    start_date: date | str,
    end_date: date | str,
    actor: str,
    idempotency_key: str,
) -> ManualHoldoutBinding:
    actor = str(actor).strip()
    idempotency_key = str(idempotency_key).strip()
    if not actor:
        raise ManualHoldoutError("holdout_actor_required")
    if not idempotency_key:
        raise ManualHoldoutError("holdout_idempotency_key_required")
    start, end = _date(start_date, "start_date"), _date(end_date, "end_date")
    if start > end:
        raise ManualHoldoutError("holdout_start_must_not_exceed_end")
    _sha(data_content_hash, "data_content_hash")
    release = db.get(StrategyRelease, release_id)
    if release is None:
        raise ManualHoldoutError("strategy_release_not_found")
    request_hash = _hash({
        "release_id": release_id, "release_hash": release.release_hash, "dataset_id": dataset_id,
        "data_content_hash": data_content_hash, "start_date": start.isoformat(), "end_date": end.isoformat(),
        "actor": actor,
    })
    existing = db.scalars(select(ManualHoldoutBinding).where(ManualHoldoutBinding.idempotency_key == idempotency_key)).first()
    if existing is not None:
        if existing.request_hash != request_hash:
            raise ManualHoldoutError("holdout_idempotency_conflict")
        return existing
    try:
        evaluation = _verify_portfolio_parent(db, release)
        _intervals_after_validation(db, evaluation, start)
        _lock_registry(db)
        # The parent check above is intentionally outside the short registry
        # lock.  Refresh both rows under that lock and refuse a concurrent
        # release/evaluation change instead of binding stale evidence.
        db.refresh(release)
        fresh_evaluation = db.get(StrategyPromotionEvaluation, evaluation.id)
        if (
            fresh_evaluation is None
            or release.status != "portfolio_passed"
            or release.release_hash != evaluation.release_hash
            or fresh_evaluation.evaluation_hash != evaluation.evaluation_hash
            or fresh_evaluation.release_hash != release.release_hash
        ):
            raise ManualHoldoutError("holdout_parent_changed_during_create")
        evaluation = fresh_evaluation
        _factor_experiment_overlap(db, start, end)
        _evaluation_scope_overlap(db, start, end)
        protocol = _protocol(release, evaluation, dataset_id, data_content_hash, start, end)
        protocol_json = _canonical(protocol)
        protocol_hash = _hash(protocol)
        ensure_savepoint_transaction(db)
        with db.begin_nested():
            window = create_holdout_window(
                db, dataset_id=dataset_id, data_content_hash=data_content_hash,
                start_date=start, end_date=end, policy_hash=protocol_hash, commit=False,
            )
            binding_hash = _hash({
                "window_id": window.id,
                "release_hash": release.release_hash,
                "portfolio_parent_hash": evaluation.evaluation_hash,
                "protocol_hash": protocol_hash,
            })
            binding = ManualHoldoutBinding(
                id=uuid4_str(), window_id=window.id, release_id=release.id,
                release_hash=release.release_hash, strategy_core_hash=release.strategy_fingerprint,
                portfolio_evaluation_id=evaluation.id, portfolio_evaluation_hash=evaluation.evaluation_hash,
                protocol_json=protocol_json, protocol_hash=protocol_hash, binding_hash=binding_hash,
                idempotency_key=idempotency_key, request_hash=request_hash, created_by=actor,
            )
            db.add(binding)
            db.flush()
            binding.binding_hash = _hash({
                "window_id": window.id,
                "release_hash": release.release_hash,
                "portfolio_parent_hash": evaluation.evaluation_hash,
                "protocol_hash": protocol_hash,
                "created_by": actor,
                "created_at": binding.created_at,
            })
            db.flush()
        db.commit()
        db.refresh(binding)
        return binding
    except IntegrityError as exc:
        db.rollback()
        raise ManualHoldoutError("holdout_binding_conflict") from exc
    except (HoldoutError, ManualHoldoutError):
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


def _validate_binding(
    db: Session, binding: ManualHoldoutBinding, *, verify_parent: bool = True,
) -> tuple[ResearchHoldoutWindow, StrategyRelease, StrategyPromotionEvaluation | None, dict[str, Any]]:
    protocol = _object(binding.protocol_json, "holdout_protocol")
    if binding.protocol_hash != _hash(protocol) or protocol.get("protocol_version") != "manual-holdout-preregistration-v1":
        raise ManualHoldoutError("holdout_protocol_hash_mismatch")
    window = db.get(ResearchHoldoutWindow, binding.window_id)
    release = db.get(StrategyRelease, binding.release_id)
    if window is None or release is None:
        raise ManualHoldoutError("holdout_binding_identity_missing")
    if _hash({
        "window_id": window.id, "release_hash": binding.release_hash,
        "portfolio_parent_hash": binding.portfolio_evaluation_hash, "protocol_hash": binding.protocol_hash,
        "created_by": binding.created_by, "created_at": binding.created_at,
    }) != binding.binding_hash:
        raise ManualHoldoutError("holdout_binding_hash_mismatch")
    holdout = protocol.get("holdout", {})
    if any(window_value != holdout.get(key) for window_value, key in (
        (window.dataset_id, "dataset_id"), (window.data_content_hash, "data_content_hash"),
        (window.start_date, "start_date"), (window.end_date, "end_date"),
    )) or window.policy_hash != binding.protocol_hash:
        raise ManualHoldoutError("holdout_window_identity_mismatch")
    if release.release_hash != binding.release_hash or release.strategy_fingerprint != binding.strategy_core_hash:
        raise ManualHoldoutError("holdout_release_identity_mismatch")
    request_hash = _hash({
        "release_id": release.id, "release_hash": release.release_hash,
        "dataset_id": window.dataset_id, "data_content_hash": window.data_content_hash,
        "start_date": window.start_date, "end_date": window.end_date, "actor": binding.created_by,
    })
    if binding.request_hash != request_hash:
        raise ManualHoldoutError("holdout_request_hash_mismatch")
    release_obj = protocol.get("release", {})
    if release_obj.get("bundle_hash") != release.bundle_hash or release_obj.get("strategy_core_hash") != release.strategy_fingerprint:
        raise ManualHoldoutError("holdout_protocol_release_mismatch")
    evaluation = None
    if verify_parent:
        evaluation = _verify_portfolio_parent(db, release)
        if evaluation.id != binding.portfolio_evaluation_id or evaluation.evaluation_hash != binding.portfolio_evaluation_hash:
            raise ManualHoldoutError("holdout_parent_evidence_mismatch")
        expected_protocol = _protocol(
            release, evaluation, window.dataset_id, window.data_content_hash,
            _date(window.start_date, "start_date"), _date(window.end_date, "end_date"),
        )
        if protocol != expected_protocol:
            raise ManualHoldoutError("holdout_protocol_frozen_content_mismatch")
    return window, release, evaluation, protocol


def _open_snapshot(
    binding: ManualHoldoutBinding,
    window: ResearchHoldoutWindow,
    release: StrategyRelease,
    evaluation: StrategyPromotionEvaluation,
    artifacts: dict[str, tuple[Any, ...]],
) -> dict[str, Any]:
    """Capture the rows whose identities were checked outside the DB lock."""
    return {
        "binding": tuple(getattr(binding, name) for name in (
            "id", "window_id", "release_id", "release_hash", "strategy_core_hash",
            "portfolio_evaluation_id", "portfolio_evaluation_hash", "protocol_json", "protocol_hash",
            "binding_hash", "idempotency_key", "request_hash", "created_by", "created_at",
        )),
        "window": tuple(getattr(window, name) for name in (
            "id", "dataset_id", "data_content_hash", "start_date", "end_date", "policy_hash",
            "created_at", "invalidated_at", "invalidation_reason", "completed_at",
        )),
        "release": tuple(getattr(release, name) for name in (
            "id", "strategy_key", "version", "bundle_hash", "release_hash", "strategy_fingerprint",
            "market", "research_evidence", "execution_policy", "risk_policy", "promotion_policy", "status",
        )),
        "evaluation": tuple(getattr(evaluation, name) for name in (
            "id", "release_id", "release_hash", "from_status", "target_status", "policy_hash",
            "evidence_refs_json", "resolved_evidence_hash", "checks_json", "decision", "resolver_version",
            "resolver_code_hash", "actor", "idempotency_key", "request_hash", "previous_evaluation_id",
            "previous_evaluation_hash", "evaluation_hash", "created_at",
        )),
        "artifacts": artifacts,
    }


def _artifact_snapshot(db: Session, release: StrategyRelease, evaluation: StrategyPromotionEvaluation) -> dict[str, tuple[Any, ...]]:
    refs = list(_object(release.research_evidence, "research_evidence").values())
    refs.extend(_object(evaluation.evidence_refs_json, "portfolio_evaluation_refs").values())
    result: dict[str, tuple[Any, ...]] = {}
    for artifact_id in sorted(set(refs)):
        row = db.scalars(select(ResearchEvidenceArtifact).where(
            ResearchEvidenceArtifact.id == artifact_id,
        ).execution_options(populate_existing=True)).first()
        if row is None:
            raise ManualHoldoutError("holdout_parent_artifact_missing")
        result[artifact_id] = (row.id, row.status, row.evidence_hash, row.identity_hash, row.manifest_hash)
    return result


def _assert_open_snapshot(
    snapshot: dict[str, Any], binding: ManualHoldoutBinding, window: ResearchHoldoutWindow,
    release: StrategyRelease, evaluation: StrategyPromotionEvaluation, artifacts: dict[str, tuple[Any, ...]],
) -> None:
    current = _open_snapshot(binding, window, release, evaluation, artifacts)
    if current != snapshot:
        raise ManualHoldoutError("holdout_parent_changed_during_open")


def open_manual_holdout(db: Session, binding_id: str, *, actor: str, purpose: str) -> ResearchHoldoutAccess:
    actor, purpose = str(actor).strip(), str(purpose).strip()
    if not actor:
        raise ManualHoldoutError("holdout_actor_required")
    if not purpose:
        raise ManualHoldoutError("holdout_purpose_required")
    binding = db.get(ManualHoldoutBinding, binding_id)
    if binding is None:
        raise ManualHoldoutError("holdout_binding_not_found")
    try:
        # Recheck before taking the short allocation lock so stale evidence is
        # rejected without holding the database write lock during replay.
        checked_window, checked_release, checked_evaluation, protocol = _validate_binding(db, binding)
        if checked_evaluation is None:
            raise ManualHoldoutError("holdout_parent_missing")
        artifacts = _artifact_snapshot(db, checked_release, checked_evaluation)
        snapshot = _open_snapshot(binding, checked_window, checked_release, checked_evaluation, artifacts)
        ensure_savepoint_transaction(db)
        with db.begin_nested():
            _lock_registry(db)
            db.refresh(binding)
            window = db.get(ResearchHoldoutWindow, binding.window_id)
            release = db.get(StrategyRelease, binding.release_id)
            evaluation = db.get(StrategyPromotionEvaluation, binding.portfolio_evaluation_id)
            if window is None or release is None or evaluation is None:
                raise ManualHoldoutError("holdout_window_not_found")
            db.refresh(window)
            db.refresh(release)
            db.refresh(evaluation)
            artifact_rows = db.scalars(select(ResearchEvidenceArtifact).where(
                ResearchEvidenceArtifact.id.in_(tuple(artifacts)),
            ).execution_options(populate_existing=True)).all()
            current_artifacts = {
                row.id: (row.id, row.status, row.evidence_hash, row.identity_hash, row.manifest_hash)
                for row in artifact_rows
            }
            if set(current_artifacts) != set(artifacts):
                raise ManualHoldoutError("holdout_parent_artifact_missing")
            _validate_binding(db, binding, verify_parent=False)
            _assert_open_snapshot(snapshot, binding, window, release, evaluation, current_artifacts)
            if window.status in {"invalidated", "completed"}:
                raise ManualHoldoutError(f"holdout_{window.status}")
            now = manual_now_str()
            if any(datetime.fromisoformat(now) < datetime.fromisoformat(value) for value in (
                binding.created_at, window.created_at, evaluation.created_at if evaluation else binding.created_at,
            )):
                raise ManualHoldoutError("holdout_opened_at_before_binding")
            holdout = protocol["holdout"]
            changed = db.execute(update(ResearchHoldoutWindow).where(
                ResearchHoldoutWindow.id == window.id,
                ResearchHoldoutWindow.status.in_(("sealed", "opened")),
                ResearchHoldoutWindow.dataset_id == holdout["dataset_id"],
                ResearchHoldoutWindow.data_content_hash == holdout["data_content_hash"],
                ResearchHoldoutWindow.start_date == holdout["start_date"],
                ResearchHoldoutWindow.end_date == holdout["end_date"],
                ResearchHoldoutWindow.policy_hash == binding.protocol_hash,
            ).values(status="opened", opened_at=func.coalesce(ResearchHoldoutWindow.opened_at, now)))
            if changed.rowcount != 1:
                raise ManualHoldoutError("holdout_status_changed_during_open")
            db.refresh(window)
            access = ResearchHoldoutAccess(
                id=uuid4_str(), window_id=window.id, accessed_at=now,
                accessed_by=actor, purpose=purpose, result_exposed=True,
                binding_hash=binding.binding_hash,
                payload_hash=_hash({
                    "binding_hash": binding.binding_hash, "window_id": window.id,
                    "accessed_by": actor, "purpose": purpose, "accessed_at": now,
                    "result_exposed": True,
                }),
            )
            db.add(access)
            db.flush()
        db.commit()
        db.refresh(access)
        return access
    except (ManualHoldoutError, HoldoutError):
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


def invalidate_manual_holdout(db: Session, binding_id: str, *, reason: str) -> ResearchHoldoutWindow:
    reason = str(reason).strip()
    if not reason:
        raise ManualHoldoutError("holdout_invalidation_reason_required")
    binding = db.get(ManualHoldoutBinding, binding_id)
    if binding is None:
        raise ManualHoldoutError("holdout_binding_not_found")
    try:
        _validate_binding(db, binding, verify_parent=False)
    except (ManualHoldoutError, HoldoutError):
        raise
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise _raise(exc) from exc
    try:
        _validate_binding(db, binding, verify_parent=False)
        ensure_savepoint_transaction(db)
        result_window = None
        with db.begin_nested():
            _lock_registry(db)
            db.refresh(binding)
            window = db.get(ResearchHoldoutWindow, binding.window_id)
            if window is None:
                raise ManualHoldoutError("holdout_window_not_found")
            _validate_binding(db, binding, verify_parent=False)
            if window.status == "completed":
                raise ManualHoldoutError("completed_holdout_cannot_be_invalidated")
            if window.status == "invalidated":
                result_window = window
            else:
                now = manual_now_str()
                changed = db.execute(update(ResearchHoldoutWindow).where(
                    ResearchHoldoutWindow.id == window.id,
                    ResearchHoldoutWindow.status.in_(("sealed", "opened")),
                ).values(status="invalidated", invalidated_at=now, invalidation_reason=reason))
                if changed.rowcount != 1:
                    db.refresh(window)
                    if window.status == "invalidated":
                        result_window = window
                    else:
                        raise ManualHoldoutError("holdout_status_changed_during_invalidate")
                else:
                    result_window = window
            db.refresh(window)
        db.commit()
        db.refresh(result_window)
        return result_window
    except (ManualHoldoutError, HoldoutError):
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


__all__ = ["ManualHoldoutError", "create_manual_holdout", "invalidate_manual_holdout", "open_manual_holdout"]
