"""Economic H2c holdout resolver.

The resolver consumes only a database artifact id.  The execution service is
the authority for re-reading the frozen files and replaying the result;
promotion adds the release-chain and policy checks around that verified fact.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.models.schema import (
    ManualHoldoutBinding,
    ManualHoldoutEvaluation,
    ResearchEvidenceArtifact,
    ResearchHoldoutWindow,
    StrategyPromotionEvaluation,
    StrategyRelease,
)
from server.services.strategy_promotion import ManualDailyPromotionPolicyV1


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _obj(value: Any, field: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field}_invalid") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field}_invalid")
    return parsed


def _decimal(value: Any, field: str) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _scenario(metrics: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = metrics.get(name)
    if isinstance(value, dict):
        return value
    return {}


def _metric(scenario: Mapping[str, Any], name: str) -> Decimal | None:
    return _decimal(scenario.get(name), name)


def _parent_portfolio(
    db: Session,
    release: StrategyRelease,
    *,
    require_portfolio_status: bool = True,
) -> tuple[StrategyPromotionEvaluation, dict[str, bool]]:
    """Revalidate the historical portfolio evaluation and its research parent."""
    from server.services.manual_holdout import _verify_portfolio_parent

    row = _verify_portfolio_parent(db, release, require_portfolio_status=require_portfolio_status)
    return row, {
        "portfolio_parent_chain_valid": True,
        "portfolio_parent_replay_valid": True,
    }


def _economic_gate(metrics: Mapping[str, Any], policy: ManualDailyPromotionPolicyV1, verified: Mapping[str, Any]) -> dict[str, bool]:
    """Apply the same economic checks to every preregistered binding."""
    baseline, stress = _scenario(metrics, "baseline"), _scenario(metrics, "stress")
    b_return = _metric(baseline, "total_return")
    s_return = _metric(stress, "total_return")
    b_excess = _metric(baseline, "excess_return")
    s_excess = _metric(stress, "excess_return")
    s_sharpe = _metric(stress, "sharpe_252_rf0")
    s_dd = _metric(stress, "max_drawdown_magnitude")
    b_turn = _metric(baseline, "annual_turnover_double_sided")
    s_turn = _metric(stress, "annual_turnover_double_sided")
    b_fill = _metric(baseline, "capacity_fill_rate_amount_weighted")
    s_fill = _metric(stress, "capacity_fill_rate_amount_weighted")
    b_samples = _metric(baseline, "trade_count")
    s_samples = _metric(stress, "trade_count")
    quality_errors = verified.get("quality_errors")
    replays = verified.get("independent_replays")
    replay_ok = isinstance(replays, dict) and all(
        isinstance(replays.get(name), dict) and replays[name].get("passed") is True
        for name in ("baseline", "stress")
    )
    return {
        "baseline_net_return_positive": b_return is not None and b_return > 0,
        "stress_net_return_positive": s_return is not None and s_return > 0,
        "baseline_excess_return_positive": b_excess is not None and b_excess > 0,
        "stress_excess_return_positive": s_excess is not None and s_excess > 0,
        "stress_sharpe_gate": s_sharpe is not None and s_sharpe >= policy.minimum_stress_sharpe,
        "stress_drawdown_gate": s_dd is not None and Decimal(0) <= s_dd <= policy.maximum_stress_drawdown,
        "turnover_gate": b_turn is not None and s_turn is not None and Decimal(0) <= b_turn and Decimal(0) <= s_turn and max(b_turn, s_turn) <= policy.maximum_annual_turnover,
        "fill_rate_gate": b_fill is not None and s_fill is not None and Decimal(0) <= b_fill <= Decimal(1) and Decimal(0) <= s_fill <= Decimal(1) and min(b_fill, s_fill) >= policy.minimum_capacity_fill_rate,
        "sample_count_present": b_samples is not None and s_samples is not None and b_samples > 0 and s_samples > 0,
        "quality_errors_empty": isinstance(quality_errors, list) and not quality_errors,
        "independent_replays_passed": replay_ok,
    }


def resolve_holdout_passed(
    db: Session,
    release: StrategyRelease,
    evidence_refs: Mapping[str, str],
    *,
    require_portfolio_status: bool = True,
) -> Any:
    """Resolve H2c from a completed, independently verified holdout artifact."""
    from server.services.manual_evidence import ManualEvidenceError, ResolvedReleaseEvidence, _release_identity_valid, holdout_commit_snapshot
    from server.services.manual_holdout_evaluation import verify_completed_holdout
    starting_snapshot = holdout_commit_snapshot(db, release.id)

    try:
        if set(evidence_refs) != {"holdout_artifact_id"}:
            raise ValueError("holdout_promotion_requires_holdout_artifact_id")
        artifact_id = str(evidence_refs.get("holdout_artifact_id") or "")
        artifact = db.get(ResearchEvidenceArtifact, artifact_id)
        if artifact is None or artifact.kind != "holdout_result" or artifact.status != "verified":
            raise ValueError("holdout_result_artifact_invalid")
        verified = verify_completed_holdout(db, artifact_id)
        if not isinstance(verified, dict):
            raise ValueError("holdout_verified_result_invalid")

        evaluation = db.get(ManualHoldoutEvaluation, verified.get("evaluation_id"))
        if evaluation is None:
            raise ValueError("holdout_evaluation_not_found")
        binding = db.get(ManualHoldoutBinding, evaluation.binding_id)
        if binding is None:
            raise ValueError("holdout_binding_not_found")
        window = db.get(ResearchHoldoutWindow, binding.window_id)
        if window is None:
            raise ValueError("holdout_window_not_found")
        protocol = _obj(binding.protocol_json, "holdout_protocol")
        policy_payload = _obj(protocol.get("promotion_policy"), "holdout_promotion_policy")
        policy = ManualDailyPromotionPolicyV1(**policy_payload)
        release_policy = _obj(release.promotion_policy, "promotion_policy")

        # A release's preregistered holdout set is an all-or-nothing sample.
        # The caller's artifact is merely the lookup key; it cannot select a
        # favourable window while leaving another planned/failed window out.
        all_bindings = list(db.scalars(select(ManualHoldoutBinding).where(
            ManualHoldoutBinding.release_id == release.id,
        ).order_by(ManualHoldoutBinding.created_at.asc(), ManualHoldoutBinding.id.asc())).all())
        if not all_bindings or binding.id not in {row.id for row in all_bindings}:
            raise ValueError("holdout_artifact_binding_set_invalid")
        set_artifacts: list[tuple[ManualHoldoutBinding, ManualHoldoutEvaluation, ResearchEvidenceArtifact, dict[str, Any]]] = []
        for candidate_binding in all_bindings:
            candidate_window = db.get(ResearchHoldoutWindow, candidate_binding.window_id)
            candidate_eval = db.scalars(select(ManualHoldoutEvaluation).where(
                ManualHoldoutEvaluation.binding_id == candidate_binding.id,
            )).first()
            if candidate_window is None or candidate_eval is None or candidate_eval.status != "completed" or candidate_window.status != "completed":
                raise ValueError("holdout_preregistered_set_incomplete")
            candidate_artifact = db.get(ResearchEvidenceArtifact, candidate_eval.result_artifact_id)
            if candidate_artifact is None or candidate_artifact.kind != "holdout_result" or candidate_artifact.status != "verified":
                raise ValueError("holdout_preregistered_set_artifact_invalid")
            candidate_verified = verify_completed_holdout(db, candidate_artifact.id)
            if (
                candidate_verified.get("artifact_id") != candidate_artifact.id
                or candidate_verified.get("evaluation_id") != candidate_eval.id
                or candidate_verified.get("binding_id") != candidate_binding.id
            ):
                raise ValueError("holdout_preregistered_set_identity_invalid")
            candidate_metrics = candidate_verified.get("metrics") if isinstance(candidate_verified, dict) else None
            if not isinstance(candidate_metrics, dict):
                raise ValueError("holdout_preregistered_set_metrics_invalid")
            candidate_accesses = candidate_verified.get("accesses")
            if not isinstance(candidate_accesses, list) or len(candidate_accesses) < policy.minimum_holdout_accesses or any(
                not isinstance(row, dict)
                or set(row) != {"id", "window_id", "accessed_at", "accessed_by", "purpose", "result_exposed", "binding_hash", "payload_hash"}
                or row["window_id"] != candidate_binding.window_id
                or row["binding_hash"] != candidate_binding.binding_hash
                or row["result_exposed"] is not True
                or not isinstance(row["payload_hash"], str) or len(row["payload_hash"]) != 64
                for row in candidate_accesses
            ):
                raise ValueError("holdout_preregistered_set_access_invalid")
            candidate_gate = _economic_gate(candidate_metrics, policy, candidate_verified)
            if not all(candidate_gate.values()):
                raise ValueError("holdout_preregistered_set_gate_failed")
            set_artifacts.append((candidate_binding, candidate_eval, candidate_artifact, candidate_verified))
        all_artifact_ids = [item[2].id for item in set_artifacts]

        parent, parent_checks = _parent_portfolio(
            db, release, require_portfolio_status=require_portfolio_status,
        )
        wp = {"status": window.status}
        metrics = verified.get("metrics") if isinstance(verified.get("metrics"), dict) else {}
        checks: dict[str, bool] = {
            "release_identity_valid": _release_identity_valid(release),
            "release_status_portfolio_passed": (
                release.status == "portfolio_passed"
                or (
                    not require_portfolio_status
                    and release.status in {"holdout_passed", "paper_observing", "paper_passed", "manual_ready"}
                )
            ),
            "artifact_verified": artifact.status == "verified",
            "artifact_kind_holdout_result": artifact.kind == "holdout_result",
            "binding_release_match": verified.get("binding_id") == binding.id and binding.release_id == release.id,
            "binding_release_hash_match": binding.release_hash == release.release_hash,
            "binding_strategy_core_match": binding.strategy_core_hash == release.strategy_fingerprint,
            "binding_protocol_hash_match": binding.protocol_hash == _hash(protocol),
            "binding_portfolio_parent_match": (
                binding.portfolio_evaluation_id == parent.id
                and binding.portfolio_evaluation_hash == parent.evaluation_hash
                and verified.get("parent_portfolio_evaluation_id") == parent.id
                and verified.get("parent_portfolio_evaluation_hash") == parent.evaluation_hash
            ),
            "window_completed": wp.get("status") == "completed",
            "evaluation_completed": evaluation.status == "completed",
            "evaluation_artifact_match": evaluation.result_artifact_id == artifact_id,
            "policy_matches_release": policy.as_dict() == release_policy,
        }
        accesses = verified.get("accesses")
        if not isinstance(accesses, list):
            accesses = []
        checks["minimum_accesses"] = len(accesses) >= policy.minimum_holdout_accesses
        checks["accesses_bound_to_binding"] = bool(accesses) and all(
            isinstance(row, dict)
            and set(row) == {"id", "window_id", "accessed_at", "accessed_by", "purpose", "result_exposed", "binding_hash", "payload_hash"}
            and row["binding_hash"] == binding.binding_hash
            and row["window_id"] == binding.window_id
            and row["result_exposed"] is True
            and isinstance(row["payload_hash"], str) and len(row["payload_hash"]) == 64
            for row in accesses
        )

        baseline = _scenario(metrics, "baseline")
        stress = _scenario(metrics, "stress")
        checks.update(_economic_gate(metrics, policy, verified))
        quality_errors = verified.get("quality_errors")
        checks["quality_errors_empty"] = isinstance(quality_errors, list) and not quality_errors
        replay = verified.get("independent_replays")
        checks["independent_replays_passed"] = isinstance(replay, dict) and all(
            isinstance(replay.get(name), dict) and replay[name].get("passed") is True
            for name in ("baseline", "stress")
        )
        checks.update(parent_checks)

        resolved = {
            "release_id": release.id,
            "release_hash": release.release_hash,
            "target_status": "holdout_passed",
            "holdout_artifact_id": artifact_id,
            "holdout_artifact_ids": all_artifact_ids,
            "holdout_set_hashes": [
                {"binding_hash": item[0].binding_hash, "evaluation_hash": item[1].input_hash,
                 "artifact_id": item[2].id, "artifact_hash": item[2].evidence_hash}
                for item in set_artifacts
            ],
            "holdout_evidence_hash": artifact.evidence_hash,
            "binding_hash": binding.binding_hash,
            "previous_evaluation_id": parent.id,
            "previous_evaluation_hash": parent.evaluation_hash,
            "policy_hash": policy.policy_hash,
            "metrics": metrics,
            "checks": checks,
        }
        return ResolvedReleaseEvidence(
            "holdout_passed", checks, dict(evidence_refs), _hash(resolved),
            commit_snapshot=starting_snapshot,
        )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ManualEvidenceError):
            raise
        raise ManualEvidenceError(str(exc) or type(exc).__name__) from exc


__all__ = ["resolve_holdout_passed"]
