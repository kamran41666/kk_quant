"""Database-backed evidence resolution for manual-daily release promotion.

Clients provide stable resource IDs only.  This module re-reads database rows,
re-hashes worker artifacts and appends one immutable promotion evaluation.  It
does not accept copied metrics or caller-supplied pass/fail booleans.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from quant_engine.factor.expression import FactorExpressionSpec
from server.config import settings
from server.models.schema import (
    FactorCandidate,
    FactorExperiment,
    ResearchEvidenceArtifact,
    StrategyPromotionEvaluation,
    StrategyRelease,
    manual_now_str,
    uuid4_str,
)
from server.services.strategy_promotion import create_strategy_release


RESOLVER_VERSION = "manual-release-evidence-v1"
_FACTOR_FILES = (
    "factor_values.parquet",
    "ic_series.parquet",
    "quantile_returns.parquet",
    "report.json",
)
_MANUAL_LABEL = {
    "label_id": "manual-daily-label-v1",
    "signal_phase": "close",
    "entry_offset": 1,
    "entry_phase": "open",
    "exit_offset": 2,
    "exit_phase": "close",
    "adjusted_prices": True,
}


class ManualEvidenceError(ValueError):
    """Evidence is absent, inconsistent or outside its frozen boundary."""


def create_evidence_bound_release(
    db: Session,
    *,
    strategy_key: str,
    version: str,
    bundle_hash: str,
    strategy_fingerprint: str,
    training_artifact_id: str,
    validation_artifact_id: str,
    execution_policy: Mapping[str, Any],
    risk_policy: Mapping[str, Any],
    promotion_policy: Mapping[str, Any] | None = None,
) -> StrategyRelease:
    if set(execution_policy) == set() or execution_policy.get("auto_submit") is not False:
        raise ManualEvidenceError("manual_release_execution_policy_must_disable_auto_submit")
    if not risk_policy:
        raise ManualEvidenceError("manual_release_risk_policy_required")
    refs = {
        "training_artifact_id": str(training_artifact_id),
        "validation_artifact_id": str(validation_artifact_id),
    }
    for expected_kind, artifact_id in (
        ("factor_training", refs["training_artifact_id"]),
        ("factor_validation", refs["validation_artifact_id"]),
    ):
        artifact = db.get(ResearchEvidenceArtifact, artifact_id)
        if artifact is None or artifact.kind != expected_kind or artifact.status != "verified":
            raise ManualEvidenceError(f"manual_release_{expected_kind}_artifact_invalid")
    return create_strategy_release(
        db, strategy_key=strategy_key, version=version, bundle_hash=bundle_hash,
        strategy_fingerprint=strategy_fingerprint, research_evidence=refs,
        execution_policy=execution_policy, risk_policy=risk_policy,
        promotion_policy=promotion_policy,
    )


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolver_code_hash() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _factor_artifact_manifest(artifact_dir: Path, allowed_root: Path) -> dict[str, Any]:
    root = allowed_root.resolve(strict=True)
    directory = artifact_dir.resolve(strict=True)
    if directory != root and root not in directory.parents:
        raise ManualEvidenceError("factor_artifact_outside_controlled_root")
    files = []
    for name in _FACTOR_FILES:
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ManualEvidenceError(f"factor_artifact_missing_or_symlinked:{name}")
        resolved = path.resolve(strict=True)
        if directory not in resolved.parents:
            raise ManualEvidenceError(f"factor_artifact_path_escape:{name}")
        files.append({
            "role": name.removesuffix(".parquet").removesuffix(".json"),
            "path": str(resolved),
            "size": resolved.stat().st_size,
            "sha256": _file_hash(resolved),
        })
    return {"artifact_dir": str(directory), "files": files}


def register_factor_experiment_artifact(
    db: Session,
    experiment_id: str,
    *,
    allowed_root: str | Path | None = None,
) -> ResearchEvidenceArtifact:
    experiment = db.get(FactorExperiment, experiment_id)
    if experiment is None:
        raise ManualEvidenceError("factor_experiment_not_found")
    if experiment.status != "completed" or not experiment.result_json or not experiment.artifact_dir:
        raise ManualEvidenceError("factor_experiment_not_completed")
    if experiment.stage not in {"training", "validation"}:
        raise ManualEvidenceError("factor_experiment_stage_not_promotable")
    candidate = db.get(FactorCandidate, experiment.candidate_id)
    if candidate is None:
        raise ManualEvidenceError("factor_candidate_not_found")
    try:
        expression = FactorExpressionSpec.from_dict(json.loads(candidate.expression_spec))
        result = json.loads(experiment.result_json)
        label_spec = json.loads(experiment.label_spec or "{}")
        evaluation_policy = json.loads(experiment.evaluation_policy or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ManualEvidenceError("factor_experiment_json_invalid") from exc
    if expression.expression_hash != candidate.expression_hash:
        raise ManualEvidenceError("factor_candidate_expression_hash_mismatch")
    if label_spec != _MANUAL_LABEL or experiment.forward_horizon != 1:
        raise ManualEvidenceError("factor_experiment_is_not_manual_daily_label_v1")
    expected_decision = f"{experiment.stage}_passed"
    if result.get("decision") != expected_decision:
        raise ManualEvidenceError(f"factor_{experiment.stage}_gate_not_passed")
    if result.get("data_content_hash") != experiment.data_content_hash or result.get("label_spec") != label_spec:
        raise ManualEvidenceError("factor_experiment_result_identity_mismatch")
    root = Path(allowed_root) if allowed_root is not None else Path(settings.result_dir) / "factor-experiments"
    manifest = _factor_artifact_manifest(Path(experiment.artifact_dir), root)
    report_path = next(Path(item["path"]) for item in manifest["files"] if item["role"] == "report")
    try:
        file_report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ManualEvidenceError("factor_report_json_invalid") from exc
    if _canonical(file_report) != _canonical(result):
        raise ManualEvidenceError("factor_report_database_mismatch")
    identity = {
        "producer_protocol": "factor-experiment-v2",
        "experiment_id": experiment.id,
        "attempt": experiment.attempt,
        "stage": experiment.stage,
        "candidate_id": candidate.id,
        "expression_hash": candidate.expression_hash,
        "direction": candidate.direction,
        "role": candidate.role,
        "dataset_id": experiment.dataset_id,
        "data_content_hash": experiment.data_content_hash,
        "start_date": experiment.start_date,
        "end_date": experiment.end_date,
        "forward_horizon": experiment.forward_horizon,
        "label_spec": label_spec,
        "evaluation_policy": evaluation_policy,
        "decision": expected_decision,
    }
    identity_hash = _hash(identity)
    manifest_hash = _hash(manifest)
    evidence_hash = _hash({"identity_hash": identity_hash, "manifest_hash": manifest_hash})
    kind = f"factor_{experiment.stage}"
    existing = db.scalars(select(ResearchEvidenceArtifact).where(
        ResearchEvidenceArtifact.kind == kind,
        ResearchEvidenceArtifact.producer_entity_id == experiment.id,
        ResearchEvidenceArtifact.producer_attempt == experiment.attempt,
    )).first()
    if existing:
        if existing.evidence_hash != evidence_hash or existing.status != "verified":
            raise ManualEvidenceError("factor_artifact_changed_after_registration")
        return existing
    artifact = ResearchEvidenceArtifact(
        id=uuid4_str(), kind=kind, producer_protocol="factor-experiment-v2",
        producer_entity_type="factor_experiment", producer_entity_id=experiment.id,
        producer_attempt=experiment.attempt, status="verified",
        identity_json=_canonical(identity), identity_hash=identity_hash,
        manifest_json=_canonical(manifest), manifest_hash=manifest_hash,
        evidence_hash=evidence_hash,
    )
    db.add(artifact)
    db.commit()
    db.refresh(artifact)
    return artifact


def verify_research_artifact(artifact: ResearchEvidenceArtifact) -> bool:
    if artifact.status != "verified":
        return False
    try:
        identity = json.loads(artifact.identity_json)
        manifest = json.loads(artifact.manifest_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if _hash(identity) != artifact.identity_hash or _hash(manifest) != artifact.manifest_hash:
        return False
    for item in manifest.get("files", []):
        try:
            path = Path(item["path"])
            if path.is_symlink() or not path.is_file() or path.stat().st_size != int(item["size"]):
                return False
            if _file_hash(path) != item["sha256"]:
                return False
        except (KeyError, OSError, TypeError, ValueError):
            return False
    return artifact.evidence_hash == _hash({"identity_hash": artifact.identity_hash, "manifest_hash": artifact.manifest_hash})


def _reverify_factor_artifact(db: Session, artifact: ResearchEvidenceArtifact) -> bool:
    try:
        manifest = json.loads(artifact.manifest_json)
        artifact_dir = Path(manifest["artifact_dir"])
        refreshed = register_factor_experiment_artifact(
            db, artifact.producer_entity_id, allowed_root=artifact_dir.parent,
        )
    except (ManualEvidenceError, KeyError, OSError, TypeError, ValueError):
        return False
    return refreshed.id == artifact.id and verify_research_artifact(refreshed)


def _release_identity_valid(release: StrategyRelease) -> bool:
    try:
        identity = {
            "strategy_key": release.strategy_key,
            "version": release.version,
            "bundle_hash": release.bundle_hash,
            "strategy_fingerprint": release.strategy_fingerprint,
            "market": release.market,
            "research_evidence": json.loads(release.research_evidence or "{}"),
            "execution_policy": json.loads(release.execution_policy or "{}"),
            "risk_policy": json.loads(release.risk_policy or "{}"),
            "promotion_policy": json.loads(release.promotion_policy or "{}"),
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return _hash(identity) == release.release_hash


@dataclass(frozen=True)
class ResolvedReleaseEvidence:
    target_status: str
    checks: Mapping[str, bool]
    evidence_refs: Mapping[str, str]
    resolved_evidence_hash: str

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(self.checks.values())


def _resolve_research_passed(
    db: Session,
    release: StrategyRelease,
    evidence_refs: Mapping[str, str],
) -> ResolvedReleaseEvidence:
    if set(evidence_refs) != {"training_artifact_id", "validation_artifact_id"}:
        raise ManualEvidenceError("research_promotion_requires_training_and_validation_artifact_ids")
    training = db.get(ResearchEvidenceArtifact, evidence_refs["training_artifact_id"])
    validation = db.get(ResearchEvidenceArtifact, evidence_refs["validation_artifact_id"])
    artifacts_present = training is not None and validation is not None
    training_identity = json.loads(training.identity_json) if training else {}
    validation_identity = json.loads(validation.identity_json) if validation else {}
    frozen_refs = json.loads(release.research_evidence or "{}")
    checks = {
        "release_identity_valid": _release_identity_valid(release),
        "artifacts_present": artifacts_present,
        "artifact_kinds": bool(training and validation and training.kind == "factor_training" and validation.kind == "factor_validation"),
        "artifacts_verified": bool(
            training and validation
            and _reverify_factor_artifact(db, training)
            and _reverify_factor_artifact(db, validation)
        ),
        "release_refs_match": frozen_refs == dict(evidence_refs),
        "same_candidate": bool(training_identity and training_identity.get("candidate_id") == validation_identity.get("candidate_id")),
        "same_dataset": bool(training_identity and training_identity.get("data_content_hash") == validation_identity.get("data_content_hash")),
        "same_label": bool(training_identity and training_identity.get("label_spec") == validation_identity.get("label_spec") == _MANUAL_LABEL),
        "same_policy": bool(training_identity and training_identity.get("evaluation_policy") == validation_identity.get("evaluation_policy")),
        "non_overlapping_periods": bool(
            training_identity and date.fromisoformat(training_identity["end_date"]) < date.fromisoformat(validation_identity["start_date"])
        ),
        "gate_decisions": bool(
            training_identity.get("decision") == "training_passed"
            and validation_identity.get("decision") == "validation_passed"
        ),
    }
    resolved = {
        "release_id": release.id,
        "release_hash": release.release_hash,
        "target_status": "research_passed",
        "training_evidence_hash": training.evidence_hash if training else None,
        "validation_evidence_hash": validation.evidence_hash if validation else None,
        "checks": checks,
    }
    return ResolvedReleaseEvidence("research_passed", checks, dict(evidence_refs), _hash(resolved))


def resolve_release_evidence(
    db: Session,
    *,
    release_id: str,
    target_status: str,
    evidence_refs: Mapping[str, str],
) -> ResolvedReleaseEvidence:
    release = db.get(StrategyRelease, release_id)
    if release is None:
        raise ManualEvidenceError("strategy_release_not_found")
    if target_status == "research_passed":
        return _resolve_research_passed(db, release, evidence_refs)
    reason = f"{target_status}_evidence_resolver_not_supported"
    return ResolvedReleaseEvidence(target_status, {reason: False}, dict(evidence_refs), _hash({
        "release_id": release.id, "release_hash": release.release_hash,
        "target_status": target_status, "reason": reason,
    }))


def advance_release_from_evidence(
    db: Session,
    *,
    release_id: str,
    target_status: str,
    evidence_refs: Mapping[str, str],
    actor: str,
    idempotency_key: str,
) -> StrategyPromotionEvaluation:
    release = db.get(StrategyRelease, release_id)
    if release is None:
        raise ManualEvidenceError("strategy_release_not_found")
    if not str(actor).strip() or not str(idempotency_key).strip():
        raise ManualEvidenceError("promotion_actor_and_idempotency_key_required")
    request = {
        "release_id": release_id, "release_hash": release.release_hash,
        "target_status": target_status,
        "evidence_refs": dict(evidence_refs), "actor": str(actor).strip(),
    }
    request_hash = _hash(request)
    existing = db.scalars(select(StrategyPromotionEvaluation).where(
        StrategyPromotionEvaluation.idempotency_key == idempotency_key,
    )).first()
    if existing:
        if existing.request_hash != request_hash:
            raise ManualEvidenceError("promotion_idempotency_conflict")
        return existing
    allowed = {
        "draft": "research_passed",
        "research_passed": "portfolio_passed",
        "portfolio_passed": "holdout_passed",
        "holdout_passed": "paper_observing",
        "paper_observing": "paper_passed",
        "paper_passed": "manual_ready",
    }
    if allowed.get(release.status) != target_status:
        raise ManualEvidenceError("release_status_transition_not_allowed")
    try:
        resolved = resolve_release_evidence(
            db, release_id=release_id, target_status=target_status, evidence_refs=evidence_refs,
        )
    except (ManualEvidenceError, KeyError, TypeError, ValueError) as exc:
        reason = str(exc) or type(exc).__name__
        resolved = ResolvedReleaseEvidence(
            target_status, {reason: False}, dict(evidence_refs),
            _hash({"release_id": release.id, "release_hash": release.release_hash, "reason": reason}),
        )
    policy_hash = _hash(json.loads(release.promotion_policy or "{}"))
    previous = db.scalars(select(StrategyPromotionEvaluation).where(
        StrategyPromotionEvaluation.release_id == release.id,
        StrategyPromotionEvaluation.decision == "passed",
    ).order_by(StrategyPromotionEvaluation.created_at.desc(), StrategyPromotionEvaluation.id.desc())).first()
    evaluation_payload = {
        "release_id": release.id, "release_hash": release.release_hash,
        "from_status": release.status, "target_status": target_status,
        "policy_hash": policy_hash, "evidence_refs": dict(evidence_refs),
        "resolved_evidence_hash": resolved.resolved_evidence_hash,
        "checks": dict(resolved.checks),
        "decision": "passed" if resolved.passed else "blocked",
        "resolver_version": RESOLVER_VERSION, "resolver_code_hash": _resolver_code_hash(),
        "previous_evaluation_hash": previous.evaluation_hash if previous else "",
        "actor": str(actor).strip(), "request_hash": request_hash,
    }
    evaluation = StrategyPromotionEvaluation(
        id=uuid4_str(), release_id=release.id, release_hash=release.release_hash,
        from_status=release.status, target_status=target_status, policy_hash=policy_hash,
        evidence_refs_json=_canonical(dict(evidence_refs)),
        resolved_evidence_hash=resolved.resolved_evidence_hash,
        checks_json=_canonical(dict(resolved.checks)),
        decision="passed" if resolved.passed else "blocked",
        resolver_version=RESOLVER_VERSION, resolver_code_hash=evaluation_payload["resolver_code_hash"],
        actor=str(actor).strip(), idempotency_key=str(idempotency_key).strip(),
        request_hash=request_hash,
        previous_evaluation_id=previous.id if previous else None,
        previous_evaluation_hash=previous.evaluation_hash if previous else "",
        evaluation_hash=_hash(evaluation_payload),
    )
    db.add(evaluation)
    if resolved.passed:
        updated = db.query(StrategyRelease).filter(
            StrategyRelease.id == release.id,
            StrategyRelease.release_hash == release.release_hash,
            StrategyRelease.status == release.status,
        ).update({"status": target_status, "updated_at": manual_now_str()}, synchronize_session=False)
        if updated != 1:
            db.rollback()
            raise ManualEvidenceError("release_changed_during_promotion")
    db.commit()
    db.refresh(evaluation)
    return evaluation


def invalidate_research_artifact(
    db: Session,
    artifact_id: str,
    *,
    reason: str,
) -> ResearchEvidenceArtifact:
    artifact = db.get(ResearchEvidenceArtifact, artifact_id)
    if artifact is None:
        raise ManualEvidenceError("research_artifact_not_found")
    if artifact.status == "invalidated":
        return artifact
    if not str(reason).strip():
        raise ManualEvidenceError("artifact_invalidation_reason_required")
    artifact.status = "invalidated"
    artifact.invalidated_at = manual_now_str()
    artifact.invalidation_reason = str(reason).strip()
    db.commit()
    db.refresh(artifact)
    return artifact


__all__ = [
    "ManualEvidenceError", "RESOLVER_VERSION", "ResolvedReleaseEvidence",
    "create_evidence_bound_release",
    "register_factor_experiment_artifact", "verify_research_artifact",
    "resolve_release_evidence", "advance_release_from_evidence",
    "invalidate_research_artifact",
]
